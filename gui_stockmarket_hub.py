"""Stock Market hub panel for EVE Market Scout.

Each trading hub gets its own panel with:
- Low/Medium/High Risk tabs: Curated by trend analysis
- Holdings sub-tab: Items being actively tracked/traded
- P&L sub-tab: Profit and loss tracking
- Scrolling ticker at bottom
"""

import tkinter as tk
from tkinter import ttk, messagebox
import asyncio
import threading
from typing import Optional, Callable, List, Dict, TYPE_CHECKING

from tk_queue import submit

from config import TRADE_HUBS, get_hub_config
from historical_profiles import ProfileManager, YearlyStats
from gui_stockmarket_ticker import ScrollingTicker
from gui_stockmarket_risk import RiskCategoryPanel, format_isk
from gui_stockmarket_hub_filters import HubFilterPhaseMixin

if TYPE_CHECKING:
    from api import ESIClient
    from gui_stockmarket_settings import StockMarketSettings
    from stockmarket_filters import StockMarketFilters


def _check_thread(context: str):
    """Debug helper - warn if not on main thread."""
    current = threading.current_thread()
    if current is not threading.main_thread():
        print(f"[THREAD WARNING] {context} called from {current.name}")
        import traceback
        traceback.print_stack(limit=8)


class StockMarketHubPanel(HubFilterPhaseMixin):
    """Panel for a single trading hub's stock market functionality.
    
    Contains sub-tabs:
    - Low Risk: Stable trend items (green)
    - Medium Risk: Rising trend items (yellow)
    - High Risk: Falling trend items (red)
    - Holdings: Track active positions
    - P&L: Profit and loss tracking
    """
    
    def __init__(
        self,
        parent: ttk.Frame,
        hub_key: str,
        settings: "StockMarketSettings",
        profiles: ProfileManager,
        get_client: Optional[Callable[[], "ESIClient"]] = None,
        set_status: Optional[Callable[[str], None]] = None,
    ):
        self.parent = parent
        self.hub_key = hub_key
        self.hub_config = get_hub_config(hub_key)
        self.settings = settings
        self.profiles = profiles
        self.get_client = get_client
        self.set_status = set_status or (lambda s: None)
        
        # Filters with hub-specific fee calculation (from cached JSON)
        from stockmarket_filters import load_filters
        self.filters = load_filters()
        self.filters.load_from_cached_skills(hub_key)
        
        self.region_id = self.hub_config["region_id"]
        self.station_id = self.hub_config["station_id"]
        
        # Live prices cache (shared across sub-tabs)
        self.live_prices: Dict[int, float] = {}
        
        # Sub-panels
        self.holdings_panel = None
        self.pnl_panel = None
        self.risk_panels = {}  # "low", "medium", "high"
        
        # Ticker
        self.ticker = None
        
        # Create UI
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill=tk.BOTH, expand=True)
        
        self._create_widgets()
    
    def _create_widgets(self):
        """Create panel widgets."""
        # Main container
        main_container = ttk.Frame(self.frame)
        main_container.pack(fill=tk.BOTH, expand=True)
        
        # Create notebook for sub-tabs
        self.sub_notebook = ttk.Notebook(main_container)
        self.sub_notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Risk category tabs
        for risk_level, tab_name in [("low", "Low Risk"), ("medium", "Med Risk"), ("high", "High Risk")]:
            risk_frame = ttk.Frame(self.sub_notebook)
            self.sub_notebook.add(risk_frame, text=tab_name)
            self._create_risk_tab(risk_frame, risk_level)
        
        # Holdings tab
        holdings_frame = ttk.Frame(self.sub_notebook)
        self.sub_notebook.add(holdings_frame, text="Holdings")
        self._create_holdings_tab(holdings_frame)
        
        # P&L tab
        pnl_frame = ttk.Frame(self.sub_notebook)
        self.sub_notebook.add(pnl_frame, text="P&L")
        self._create_pnl_tab(pnl_frame)
        
        # Scrolling ticker at bottom
        self.ticker = ScrollingTicker(self.frame)
        self.ticker.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)
        
        # Bind tab change to update ticker
        self.sub_notebook.bind("<<NotebookTabChanged>>", lambda e: self._update_ticker())
        
        # Lock overlay (hidden by default).  Shown during material filter +
        # refresh so the user can't interact while the panel is computing.
        # See _show_filter_overlay / _hide_filter_overlay.
        self._overlay_frame = None
        self._overlay_status_var = None
        self._overlay_progress = None
    
    def _create_risk_tab(self, parent: ttk.Frame, risk_level: str):
        """Create a risk category tab."""
        panel = RiskCategoryPanel(
            parent,
            hub_key=self.hub_key,
            risk_level=risk_level,
            profiles=self.profiles,
            filters=self.filters,
            get_client=self.get_client,
            set_status=self.set_status,
            on_item_selected=self._on_item_added_to_holdings,
            on_double_click=self._on_item_double_click,
        )
        self.risk_panels[risk_level] = panel
    
    def _create_holdings_tab(self, parent: ttk.Frame):
        """Create the holdings sub-tab."""
        from gui_stockmarket_holdings import HoldingsPanel
        
        self.holdings_panel = HoldingsPanel(
            parent,
            hub_key=self.hub_key,
            profiles=self.profiles,
            get_client=self.get_client,
            set_status=self.set_status,
        )
    
    def _create_pnl_tab(self, parent: ttk.Frame):
        """Create the P&L sub-tab."""
        from gui_stockmarket_pnl import PnLPanel
        
        self.pnl_panel = PnLPanel(
            parent,
            hub_key=self.hub_key,
            set_status=self.set_status,
        )
    
    def _on_item_added_to_holdings(self, type_id: int, type_name: str):
        """Called when user selects an item from a risk panel to watch."""
        if self.holdings_panel:
            self.holdings_panel.add_watched_item(type_id, type_name)
            # Switch to holdings tab (index 3: Low=0, Med=1, High=2, Holdings=3, P&L=4)
            self.sub_notebook.select(3)
    
    def reload_filters_from_cache(self):
        """Reload fee rates from cached skills JSON (called after ESI refresh)."""
        _check_thread(f"HubPanel.reload_filters_from_cache({self.hub_key})")
        self.filters.load_from_cached_skills(self.hub_key)
    
    def _on_item_double_click(self, type_id: int, type_name: str):
        """Handle double-click to open price history graph."""
        from graphing import show_price_graph
        
        show_price_graph(
            self.frame,
            type_id=type_id,
            type_name=type_name,
            region_id=self.region_id,
            profiles=self.profiles,
        )
    
    def _update_ticker(self):
        """Update ticker in background thread to avoid blocking UI."""
        if not self.ticker:
            return
        
        # Capture current state for thread
        try:
            current_tab = self.sub_notebook.index(self.sub_notebook.select())
        except Exception:
            current_tab = 0
        
        # Get holdings type_ids if needed (safe to read from main thread)
        holdings_type_ids = []
        if current_tab in (3, 4) and self.holdings_panel:
            holdings_type_ids = [e.type_id for e in self.holdings_panel.holdings.get_all()]
        
        # Copy live prices for thread safety
        live_prices_copy = dict(self.live_prices)
        
        def compute_ticker():
            """Background thread: compute ticker items (DB queries here)."""
            from sde_manager import get_sde_manager
            sde = get_sde_manager()
            
            # Tab indices: 0=Low Risk, 1=Med Risk, 2=High Risk, 3=Holdings, 4=P&L
            if current_tab in (3, 4):
                # Holdings or P&L - show only holdings
                profiles_to_show = [
                    self.profiles.get_computed_profile(tid, self.region_id)
                    for tid in holdings_type_ids
                ]
                profiles_to_show = [p for p in profiles_to_show if p]
            else:
                # Risk tabs - filter by risk level
                risk_map = {0: "low", 1: "medium", 2: "high"}
                risk_level = risk_map.get(current_tab, "low")
                
                all_profiles = self.profiles.get_all_profiles()
                profiles_to_show = []
                
                for profile in all_profiles:
                    if profile.region_id != self.region_id:
                        continue
                    
                    yearly_stats = self.profiles.get_yearly_stats(profile.type_id, self.region_id)
                    trend = self._get_trend_for_ticker(yearly_stats)
                    
                    if trend == risk_level:
                        profiles_to_show.append(profile)
            
            # Calculate % change for all items
            items_with_change = []
            for profile in profiles_to_show:
                if not profile:
                    continue
                
                current = live_prices_copy.get(profile.type_id, 0)
                if current <= 0 or profile.weighted_p_low <= 0:
                    continue
                
                pct_change = ((current - profile.weighted_p_low) / profile.weighted_p_low) * 100
                type_name = sde.get_type_name(profile.type_id) or f"Type {profile.type_id}"
                items_with_change.append((type_name, pct_change))
            
            # Sort by absolute % change (biggest movers), take top 20
            items_with_change.sort(key=lambda x: abs(x[1]), reverse=True)
            ticker_items = items_with_change[:20]
            
            # Update UI on main thread
            submit(lambda: self._apply_ticker_items(ticker_items))
        
        threading.Thread(target=compute_ticker, daemon=True).start()
    
    def _apply_ticker_items(self, ticker_items: list):
        """Apply computed ticker items to UI (main thread only)."""
        if self.ticker:
            self.ticker.update_items(ticker_items)
    
    def _get_trend_for_ticker(self, yearly_stats: dict) -> str:
        """Determine trend from yearly stats for ticker filtering."""
        if len(yearly_stats) < 2:
            return "none"
        
        years = sorted(yearly_stats.keys(), reverse=True)
        floors = [yearly_stats[y].p_low for y in years[:3]]
        
        if len(floors) < 2:
            return "none"
        
        # Declining = high risk
        declining = all(floors[i] < floors[i + 1] for i in range(len(floors) - 1))
        if declining:
            return "high"
        
        # Rising = medium risk
        rising = all(floors[i] > floors[i + 1] for i in range(len(floors) - 1))
        if rising:
            return "medium"
        
        # Check stability = low risk
        if len(floors) >= 2:
            avg_floor = sum(floors) / len(floors)
            if avg_floor > 0:
                max_deviation = max(abs(f - avg_floor) / avg_floor * 100 for f in floors)
                if max_deviation <= 15:
                    return "low"
        
        return "none"
    
    # =========================================================================
    # External API
    # =========================================================================
    
    def update_settings(self, settings: "StockMarketSettings"):
        """Update settings reference."""
        self.settings = settings
        self.refresh_display()
    
    def update_live_prices(self, prices: Dict[int, float]):
        """Update live prices in all sub-tabs.
        
        Only updates price-dependent columns, NOT full refresh.
        Full refresh (with DB queries) only happens on startup or daily material filter.
        """
        old_count = len(self.live_prices)
        self.live_prices.update(prices)
        new_count = len(self.live_prices)
        print(f"[StockMarket-{self.hub_key}] update_live_prices: received {len(prices)}, total now {new_count} (was {old_count})")
        
        # Update price data in panels
        for panel in self.risk_panels.values():
            panel.live_prices.update(prices)
        
        if self.holdings_panel:
            self.holdings_panel.live_prices.update(prices)
        
        # Update only price columns (fast, no DB queries)
        self._update_prices_only()
        
        # Update ticker with new prices
        self._update_ticker()
    
    def _update_prices_only(self):
        """Update only price-dependent columns in all panels.
        
        Called on every scan. Much faster than refresh_display().
        """
        for panel in self.risk_panels.values():
            if hasattr(panel, 'update_prices_only'):
                panel.update_prices_only()
        
        if self.holdings_panel and hasattr(self.holdings_panel, 'update_prices_only'):
            self.holdings_panel.update_prices_only()
    
    def get_type_ids(self) -> List[int]:
        """Get all type IDs being tracked (from holdings)."""
        if self.holdings_panel:
            return [e.type_id for e in self.holdings_panel.holdings.get_all()]
        return []
    
    def refresh_display(self):
        """Full refresh of all sub-panels with DB queries.
        
        Use sparingly - only on startup, manual refresh, or after
        apply_material_filter().  For price updates use
        update_live_prices() instead.
        
        Note: This runs on main thread. For startup, use
        refresh_display_async().
        
        Material filter is NOT gated here.  _get_trend() in the risk
        panels reads from the pre-populated material risk cache.
        apply_material_filter() is the single entry-point that clears
        the cache, re-analyzes, marks the tracker complete, and then
        calls this method.
        """
        for panel in self.risk_panels.values():
            panel.refresh_display()
        
        if self.holdings_panel:
            self.holdings_panel.refresh_display()
        
        self._update_ticker()
    
    def refresh_display_async(self, after: Optional[Callable[[], None]] = None):
        """Refresh display without blocking UI.
        
        Gathers all data in background thread, then updates UI on main
        thread.  Uses cached material risk results (read-only) for
        classification — never triggers fresh analysis.
        
        Args:
            after: Optional callback invoked on the main thread once the
                refresh has applied (or errored).  Used by
                apply_material_filter() to hide the lock overlay.
        """
        from sde_manager import get_sde_manager
        
        print(f"[StockMarket-{self.hub_key}] refresh_display_async starting...")
        
        def gather_data():
            """Background thread: gather all profile data."""
            try:
                sde = get_sde_manager()
                
                # Invalidate the LI routing cache so this refresh picks up
                # any new leading-indicator data computed by the LI phase.
                self._li_cache_for_routing = None
                
                # Gather risk panel data
                risk_data = {"low": [], "medium": [], "high": []}
                all_profiles = self.profiles.get_all_profiles()
                region_profiles = [p for p in all_profiles if p.region_id == self.region_id]
                
                print(f"[StockMarket-{self.hub_key}] Found {len(all_profiles)} total profiles, {len(region_profiles)} for region {self.region_id}")
                
                # Batched fetch — one SQL query for the whole region instead
                # of N short-lived connections per profile.  Previously this
                # was the dominant cost when 5 hubs refresh concurrently.
                all_stats = self.profiles.get_all_yearly_stats_for_region(
                    self.region_id, context_label=self.hub_key
                )
                
                # Single pass over profiles — classify once, bucket
                # accordingly.  Previous code looped 3 times and ran
                # get_yearly_stats N*3 times.
                for profile in region_profiles:
                    yearly_stats = all_stats.get(profile.type_id, {})
                    trend = self._get_trend_for_data(yearly_stats, profile)
                    if trend not in risk_data:
                        continue
                    
                    type_name = sde.get_type_name(profile.type_id) or f"Type {profile.type_id}"
                    current_price = self.live_prices.get(profile.type_id, 0)
                    trend_tag = self._get_trend_tag_for_data(yearly_stats)
                    
                    risk_data[trend].append({
                        "type_id": profile.type_id,
                        "type_name": type_name,
                        "profile": profile,
                        "current_price": current_price,
                        "trend_tag": trend_tag,
                    })
                
                for risk_level, items in risk_data.items():
                    print(f"[StockMarket-{self.hub_key}] {risk_level}: {len(items)} items")
                
                # Update UI on main thread
                submit(lambda: self._apply_refresh_data(risk_data, after=after))
                
            except Exception as e:
                print(f"[StockMarket-{self.hub_key}] refresh_display_async ERROR: {e}")
                import traceback
                traceback.print_exc()
                # Make sure the overlay still gets hidden on error
                if after is not None:
                    submit(after)
        
        threading.Thread(target=gather_data, daemon=True).start()
    
    def _get_trend_for_data(self, yearly_stats: dict, profile) -> str:
        """Determine trend from yearly stats (thread-safe, no UI calls).
        
        Reads from the pre-populated material risk cache but never
        triggers fresh analysis.  apply_material_filter() is the only
        entry-point that clears and re-populates the cache.
        
        Leading indicators promotion (after material filter):
        UNDERCUT SPIRAL or LIQUIDITY DRAIN bumps the item one tier up
        (low -> medium, medium -> high). High Risk stays High Risk.
        """
        if len(yearly_stats) < 2:
            return "none"
        
        years = sorted(yearly_stats.keys(), reverse=True)
        floors = [yearly_stats[y].p_low for y in years[:3]]
        
        if len(floors) < 2:
            return "none"
        
        # Declining floors = high risk
        declining = all(floors[i] < floors[i + 1] for i in range(len(floors) - 1))
        if declining:
            return "high"
        
        # Determine base tier from floor pattern (+ material filter)
        base_tier = None
        rising = all(floors[i] > floors[i + 1] for i in range(len(floors) - 1))
        if rising:
            base_tier = "medium"
        else:
            # Check stability = low risk candidate
            if len(floors) >= 2:
                avg_floor = sum(floors) / len(floors)
                if avg_floor > 0:
                    max_deviation = max(abs(f - avg_floor) / avg_floor * 100 for f in floors)
                    if max_deviation <= 15:
                        # Stable floors - check cached material risk (read-only)
                        if profile:
                            from stockmarket_filters import get_cached_material_risk
                            cached = get_cached_material_risk(
                                profile.type_id, self.region_id
                            )
                            if cached == 'medium':
                                base_tier = "medium"
                            else:
                                base_tier = "low"
                        else:
                            base_tier = "low"
        
        if base_tier is None:
            return "none"
        
        # Leading indicators promotion: UNDERCUT SPIRAL or LIQUIDITY DRAIN
        # bumps one tier up. High already returned above. High Risk caps out.
        if profile and base_tier in ("low", "medium"):
            li_result = self._li_lookup_for_data(profile.type_id)
            if li_result and li_result.is_promotion:
                if base_tier == "low":
                    return "medium"
                if base_tier == "medium":
                    return "high"
        
        return base_tier
    
    def _li_lookup_for_data(self, type_id: int):
        """Lookup cached leading indicator result for a single item.
        
        Loads the per-region cache lazily and stores it on self for the
        duration of one refresh pass. Cleared by refresh_display_async
        before each background routing pass.
        """
        if not hasattr(self, "_li_cache_for_routing") or self._li_cache_for_routing is None:
            try:
                import leading_indicators_storage
                self._li_cache_for_routing = (
                    leading_indicators_storage.load_for_region(self.region_id)
                )
            except Exception as e:
                print(f"[StockMarket-{self.hub_key}] LI routing cache "
                      f"load error: {e}")
                self._li_cache_for_routing = {}
        return self._li_cache_for_routing.get(type_id)
    
    def _get_trend_tag_for_data(self, yearly_stats: dict) -> str:
        """Get trend tag for row coloring (thread-safe)."""
        if len(yearly_stats) < 2:
            return "trend_none"
        
        years = sorted(yearly_stats.keys(), reverse=True)
        floors = [yearly_stats[y].p_low for y in years[:3]]
        
        if len(floors) < 2:
            return "trend_none"
        
        declining = all(floors[i] < floors[i + 1] for i in range(len(floors) - 1))
        if declining:
            return "trend_down"
        
        rising = all(floors[i] > floors[i + 1] for i in range(len(floors) - 1))
        if rising:
            return "trend_up"
        
        if len(floors) >= 2:
            avg_floor = sum(floors) / len(floors)
            if avg_floor > 0:
                max_deviation = max(abs(f - avg_floor) / avg_floor * 100 for f in floors)
                if max_deviation <= 15:
                    return "trend_stable"
        
        return "trend_none"
    
    def _apply_refresh_data(self, risk_data: dict, after: Optional[Callable[[], None]] = None):
        """Apply gathered data to UI (main thread only).
        
        Args:
            risk_data: Pre-classified items per risk level.
            after: Optional callback invoked after the UI has been
                updated.  Used to hide the lock overlay.
        """
        print(f"[StockMarket-{self.hub_key}] _apply_refresh_data called")
        
        # Update risk panels with pre-computed data
        for risk_level, items in risk_data.items():
            panel = self.risk_panels.get(risk_level)
            if panel:
                print(f"[StockMarket-{self.hub_key}] Applying {len(items)} items to {risk_level} panel")
                panel.refresh_from_data(items)
        
        # Ticker update is already async
        self._update_ticker()
        
        print(f"[StockMarket-{self.hub_key}] _apply_refresh_data complete")
        
        if after is not None:
            try:
                after()
            except Exception as e:
                print(f"[StockMarket-{self.hub_key}] after callback error: {e}")
    
    
    def _get_floor_trend(self, yearly_stats: dict) -> str:
        """Pure floor-trend classification (no material filter lookup).
        
        Used internally by apply_material_filter() to identify stable-
        floor items that are candidates for material analysis.
        """
        if len(yearly_stats) < 2:
            return "none"
        
        years = sorted(yearly_stats.keys(), reverse=True)
        floors = [yearly_stats[y].p_low for y in years[:3]]
        
        if len(floors) < 2:
            return "none"
        
        declining = all(
            floors[i] < floors[i + 1] for i in range(len(floors) - 1)
        )
        if declining:
            return "high"
        
        rising = all(
            floors[i] > floors[i + 1] for i in range(len(floors) - 1)
        )
        if rising:
            return "medium"
        
        if len(floors) >= 2:
            avg_floor = sum(floors) / len(floors)
            if avg_floor > 0:
                max_dev = max(
                    abs(f - avg_floor) / avg_floor * 100
                    for f in floors
                )
                if max_dev <= 15:
                    return "low"
        
        return "none"
    
    def add_item(self, type_id: int, type_name: str, auto_build_profile: bool = True):
        """Add item to holdings."""
        if self.holdings_panel:
            self.holdings_panel.add_watched_item(type_id, type_name)
        
        if auto_build_profile and not self.profiles.has_profile(type_id, self.region_id):
            self._build_profile_async(type_id, self.region_id, type_name)
    
    def sync_from_orders(self, orders: List[dict]):
        """Sync holdings from ESI order data."""
        if self.holdings_panel:
            self.holdings_panel.sync_from_orders(orders)
    
    def sync_from_esi_wallet(self, wallet) -> dict:
        """Sync holdings from ESI wallet transactions."""
        if self.holdings_panel:
            return self.holdings_panel.sync_from_esi_wallet(wallet)
        return {"buys_synced": 0, "sales_synced": 0}
    
    def _build_profile_async(self, type_id: int, region_id: int, type_name: str):
        """Build profile in background."""
        self.set_status(f"Building profile for {type_name}...")
        
        def build():
            success = self.profiles.extract_item(type_id, region_id)
            submit(lambda: self._on_profile_built(type_name, success))
        
        threading.Thread(target=build, daemon=True).start()
    
    def _build_profiles_batch(self, items: List[tuple]):
        """Build profiles for multiple items."""
        self.set_status(f"Building profiles for {len(items)} items...")
        
        def build():
            built = 0
            for type_id, type_name in items:
                if self.profiles.extract_item(type_id, self.region_id):
                    built += 1
            submit(lambda: self._on_batch_complete(built, len(items)))
        
        threading.Thread(target=build, daemon=True).start()
    
    def _on_profile_built(self, type_name: str, success: bool):
        """Called when single profile build completes."""
        if success:
            self.set_status(f"Profile built for {type_name}")
        else:
            self.set_status(f"Failed to build profile for {type_name}")
        self.refresh_display()
    
    def _on_batch_complete(self, built: int, total: int):
        """Called when batch profile build completes."""
        self.set_status(f"Built {built}/{total} profiles")
        self.refresh_display()
    
    def destroy(self):
        """Clean up."""
        pass
