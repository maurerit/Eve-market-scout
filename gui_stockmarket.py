"""Stock Market tab for EVE Market Scout - long-term value investing tracker.

Main coordinator that creates sub-tabs for each trading hub.
Each hub has Discovery (bulk scanning) and Holdings (position tracking) sub-tabs.
"""

import tkinter as tk
from tkinter import ttk
import asyncio
import threading
from typing import Optional, Callable, List, Dict, TYPE_CHECKING

from tk_queue import submit
from config import TRADE_HUBS, get_hub_config
from historical_profiles import ProfileManager
from gui_stockmarket_settings import load_settings
from gui_stockmarket_hub import StockMarketHubPanel
from gui_stockmarket_actions import StockMarketActionsMixin
from gui_stockmarket_overlay import StockMarketOverlayMixin

if TYPE_CHECKING:
    from api import ESIClient
    from archive_downloader import ArchiveDownloader
    from esi_wallet import ESIWallet


class StockMarketTab(StockMarketActionsMixin, StockMarketOverlayMixin):
    """Main Stock Market tab with sub-tabs per trading hub."""
    
    def __init__(
        self,
        notebook: ttk.Notebook,
        get_client: Optional[Callable[[], "ESIClient"]] = None,
        set_status: Optional[Callable[[str], None]] = None,
    ):
        self.notebook = notebook
        self.get_client = get_client
        self.set_status = set_status or (lambda s: None)
        
        # Load settings
        self.settings = load_settings()
        
        # Profile manager (shared across all hubs)
        self.profiles = ProfileManager(
            buy_percentile=self.settings.buy_percentile,
            sell_percentile=self.settings.sell_percentile,
            archive_path=self.settings.get_archive_path()
        )
        
        # Archive downloader
        from archive_downloader import ArchiveDownloader
        self.downloader = ArchiveDownloader(archive_path=self.settings.get_archive_path())
        self.profiles.archive_path = self.downloader.archive_path
        
        # Hub panels
        self.hub_panels: Dict[str, StockMarketHubPanel] = {}
        
        # Create main frame
        self.frame = ttk.Frame(notebook)
        notebook.add(self.frame, text="Stock Market")
        
        self._create_widgets()
        # Defer status update to avoid blocking startup with slow DB queries
        self.frame.after(100, self._update_archive_status_safe)
        # Defer initial data load + material filter to background thread
        self.frame.after(500, self._startup_refresh)
        
        # Register callback so background import can trigger refresh
        # + material filter after profile building completes
        from background_import import set_profiles_ready_callback
        set_profiles_ready_callback(self._on_profiles_ready)
    
    def _create_widgets(self):
        """Create all widgets."""
        self._create_toolbar()
        self._create_hub_notebook()
        self._create_locked_overlay()
        
        # Start polling for lock state
        self._poll_lock_state()
    
    def _create_toolbar(self):
        """Create toolbar with action buttons."""
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill=tk.X, padx=5, pady=5)
        
        ttk.Button(toolbar, text="Add Item", command=self._on_add_item).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Download SDE", command=self._on_download_sde).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Refresh Prices", command=self._on_refresh_prices).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Reset Profiles", command=self._on_reset_profiles).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Settings", command=self._on_settings).pack(side=tk.LEFT, padx=2)
        
        # Spacer
        ttk.Frame(toolbar).pack(side=tk.LEFT, expand=True)
        
        # SDE status
        self.sde_label = ttk.Label(toolbar, text="SDE: --", font=("Segoe UI", 8))
        self.sde_label.pack(side=tk.RIGHT, padx=5)
        
        # Percentile display
        self.percentile_label = ttk.Label(
            toolbar,
            text=f"P{self.settings.buy_percentile}/P{self.settings.sell_percentile}",
            font=("Segoe UI", 8),
            foreground="gray"
        )
        self.percentile_label.pack(side=tk.RIGHT, padx=5)
    
    def _create_hub_notebook(self):
        """Create notebook with sub-tabs for each hub."""
        self.hub_notebook = ttk.Notebook(self.frame)
        self.hub_notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))
        
        # Create a panel for each enabled hub
        for hub_key, config in TRADE_HUBS.items():
            if not config.get("enabled", True):
                continue
            
            # Frame for this hub's tab
            hub_frame = ttk.Frame(self.hub_notebook)
            self.hub_notebook.add(hub_frame, text=config["name"])
            
            # Create the hub panel
            panel = StockMarketHubPanel(
                parent=hub_frame,
                hub_key=hub_key,
                settings=self.settings,
                profiles=self.profiles,
                get_client=self.get_client,
                set_status=self.set_status,
            )
            self.hub_panels[hub_key] = panel
    
    def _get_current_hub_panel(self) -> Optional[StockMarketHubPanel]:
        """Get the currently selected hub panel."""
        try:
            current_tab = self.hub_notebook.index(self.hub_notebook.select())
            hub_keys = [k for k, c in TRADE_HUBS.items() if c.get("enabled", True)]
            if current_tab < len(hub_keys):
                return self.hub_panels.get(hub_keys[current_tab])
        except Exception:
            pass
        return None
    
    def _get_current_hub_key(self) -> Optional[str]:
        """Get the currently selected hub key."""
        try:
            current_tab = self.hub_notebook.index(self.hub_notebook.select())
            hub_keys = [k for k, c in TRADE_HUBS.items() if c.get("enabled", True)]
            if current_tab < len(hub_keys):
                return hub_keys[current_tab]
        except Exception:
            pass
        return None
    
    # =========================================================================
    # Status Updates
    # =========================================================================
    
    def _update_archive_status_safe(self):
        """Deferred SDE status update - runs after GUI loads."""
        from sde_manager import get_sde_manager
        
        def update_in_background():
            # Get SDE info
            sde = get_sde_manager()
            if sde.is_available():
                info = sde.get_version_info()
                count = info.get("record_count", 0)
                sde_text = f"SDE: {count:,}"
            else:
                sde_text = "SDE: Not installed"
            
            # Update GUI from main thread via task queue
            submit(lambda: self.sde_label.configure(text=sde_text))
        
        # Show loading state immediately
        self.sde_label.configure(text="SDE: ...")
        
        # Run in background
        threading.Thread(target=update_in_background, daemon=True).start()
    
    def _update_sde_status(self):
        """Update SDE status label."""
        from sde_manager import get_sde_manager
        sde = get_sde_manager()
        if sde.is_available():
            info = sde.get_version_info()
            count = info.get("record_count", 0)
            self.sde_label.configure(text=f"SDE: {count:,}")
        else:
            self.sde_label.configure(text="SDE: Not installed")
    
    # =========================================================================
    # External API
    # =========================================================================
    
    def refresh_current_hub_prices(self):
        """Public wrapper for external refresh triggers (scan complete, ESI sync)."""
        self._on_refresh_prices()
    
    def add_item_from_external(self, type_id: int, region_id: int, station_id: int, type_name: str = ""):
        """Add item from external source to appropriate hub."""
        # Find the hub for this region
        for hub_key, config in TRADE_HUBS.items():
            if config["region_id"] == region_id:
                panel = self.hub_panels.get(hub_key)
                if panel:
                    panel.add_item(type_id, type_name)
                    # Switch to that hub's tab
                    for i, (hk, _) in enumerate([(k, c) for k, c in TRADE_HUBS.items() if c.get("enabled", True)]):
                        if hk == hub_key:
                            self.hub_notebook.select(i)
                            break
                return
    
    def update_from_local_orders(self, orders: List[dict], region_id: Optional[int] = None):
        """Update prices from scan results.
        
        Args:
            orders: List of market orders from scan
            region_id: Region the orders are from (if known)
        """
        if not orders:
            print(f"[StockMarket] update_from_local_orders: No orders provided")
            return
        
        # Build price dict: type_id -> lowest sell price
        prices: Dict[int, float] = {}
        
        for order in orders:
            if order.get("is_buy_order"):
                continue
            
            type_id = order["type_id"]
            price = order["price"]
            
            if type_id not in prices or price < prices[type_id]:
                prices[type_id] = price
        
        if not prices:
            print(f"[StockMarket] update_from_local_orders: No sell orders found in {len(orders)} orders")
            return
        
        print(f"[StockMarket] update_from_local_orders: {len(prices)} live prices from region {region_id}")
        
        # If region specified, update that hub only
        if region_id:
            for hub_key, panel in self.hub_panels.items():
                config = get_hub_config(hub_key)
                if config["region_id"] == region_id:
                    print(f"[StockMarket] -> Updating {hub_key} hub")
                    panel.update_live_prices(prices)
                    return
        
        # Otherwise update all hubs (legacy behavior)
        print(f"[StockMarket] -> Updating all hubs (no region specified)")
        for panel in self.hub_panels.values():
            panel.update_live_prices(prices)
    
    def sync_orders_to_holdings(self, orders: List[dict], region_id: int):
        """Sync ESI orders to holdings for appropriate hub."""
        for hub_key, panel in self.hub_panels.items():
            config = get_hub_config(hub_key)
            if config["region_id"] == region_id:
                panel.sync_from_orders(orders)
                return
    
    def sync_wallet_to_holdings(self, wallet: "ESIWallet"):
        """Sync ESI wallet transactions to holdings for all hubs.
        
        Called on each ESI refresh cycle. Each hub panel checks transactions
        for items in its holdings and updates buy/sell records.
        
        Args:
            wallet: ESIWallet instance with fetched transactions
        """
        if not wallet or not wallet.transactions:
            return
        
        total_buys = 0
        total_sales = 0
        
        for hub_key, panel in self.hub_panels.items():
            try:
                results = panel.sync_from_esi_wallet(wallet)
                total_buys += results.get("buys_synced", 0)
                total_sales += results.get("sales_synced", 0)
            except Exception as e:
                print(f"[StockMarket] Holdings sync error for {hub_key}: {e}")
        
        if total_buys > 0 or total_sales > 0:
            print(f"[StockMarket] Holdings synced: {total_buys} buys, {total_sales} sales")
    
    def sync_orders_to_pnl(self, char_orders: List[dict], wallet: "ESIWallet"):
        """Sync character orders to P&L tracking for fee calculation.
        
        Called on each ESI refresh cycle. Tracks:
        - New buy/sell order placements (broker fees)
        - Order price modifications (relist fees)
        - Completed sales (sales tax)
        
        Args:
            char_orders: List of character's active orders from ESI
            wallet: ESIWallet instance with transactions and journal
        """
        if not char_orders and not wallet:
            return
        
        from sde_manager import get_sde_manager
        sde = get_sde_manager()
        
        for hub_key, panel in self.hub_panels.items():
            if not panel.pnl_panel:
                continue
            
            try:
                pnl = panel.pnl_panel.get_pnl_manager()
                config = get_hub_config(hub_key)
                station_id = config["station_id"]
                
                # Get holdings type_ids for this hub (only track items in holdings)
                holdings_type_ids = set()
                if panel.holdings_panel:
                    holdings_type_ids = set(panel.holdings_panel.holdings.get_type_ids())
                
                if not holdings_type_ids:
                    continue
                
                # Filter orders to this hub's station and holdings items
                hub_orders = [
                    o for o in char_orders
                    if o.get("type_id") in holdings_type_ids
                ]
                
                # Check for order modifications (price changes)
                if hub_orders:
                    mod_fees = pnl.check_order_modifications(hub_orders, holdings_type_ids)
                    if mod_fees:
                        print(f"[PnL-{hub_key}] Detected {len(mod_fees)} order modification(s)")
                
                # Record new orders from wallet.orders (with location filtering)
                if wallet and wallet.orders:
                    for order in wallet.orders:
                        if order.type_id not in holdings_type_ids:
                            continue
                        # Filter by station if available
                        if hasattr(order, 'location_id') and order.location_id != station_id:
                            continue
                        
                        type_name = sde.get_type_name(order.type_id) or f"Type {order.type_id}"
                        
                        if order.is_buy_order:
                            pnl.record_buy_order(
                                order.order_id, order.type_id, type_name,
                                order.price, order.volume_remain
                            )
                        else:
                            pnl.record_sell_order(
                                order.order_id, order.type_id, type_name,
                                order.price, order.volume_remain
                            )
                
                # Record transactions (buys and sales)
                if wallet and wallet.transactions:
                    for tx in wallet.transactions:
                        if tx.type_id not in holdings_type_ids:
                            continue
                        # Filter by station
                        if hasattr(tx, 'location_id') and tx.location_id != station_id:
                            continue
                        
                        type_name = sde.get_type_name(tx.type_id) or f"Type {tx.type_id}"
                        
                        if tx.is_buy:
                            pnl.record_buy_fill(
                                tx.transaction_id, tx.type_id, type_name,
                                tx.quantity, tx.unit_price
                            )
                        else:
                            pnl.record_sale(
                                tx.transaction_id, tx.type_id, type_name,
                                tx.quantity, tx.unit_price
                            )
                
                # Refresh P&L display
                panel.pnl_panel.refresh_display()
                
            except Exception as e:
                print(f"[StockMarket] P&L sync error for {hub_key}: {e}")
                import traceback
                traceback.print_exc()
    
    def fetch_history_for_region(self, region_id: int):
        """Fetch market history for all profiled items in a region.
        
        Uses the same data flow as the scanner:
        bulk_history -> ESI supplement -> ESI API fallback
        
        This ensures Stock Market has trend data for all profiled items,
        not just items that were candidates in the scan.
        """
        if not self.get_client:
            print("[StockMarket] No client available for history fetch")
            return
        
        client = self.get_client()
        if not client:
            print("[StockMarket] Client is None")
            return
        
        # Get all profiled type_ids for this region
        all_profiles = self.profiles.get_all_profiles()
        region_profiles = [p for p in all_profiles if p.region_id == region_id]
        
        if not region_profiles:
            print(f"[StockMarket] No profiles for region {region_id}")
            return
        
        type_ids = [p.type_id for p in region_profiles]
        print(f"[StockMarket] Fetching history for {len(type_ids)} profiled items in region {region_id}")
        
        # Run async fetch in thread
        def run_fetch():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def do_fetch():
                    client.ensure_session()
                    client.reset_for_new_loop()
                    return await client.get_market_history_bulk(region_id, type_ids, use_cache=True)
                
                result = loop.run_until_complete(do_fetch())
                
                # Count results
                has_data = sum(1 for h in result.values() if h)
                empty = sum(1 for h in result.values() if not h)
                print(f"[StockMarket] History fetch complete: {has_data} with data, {empty} empty")
                
                # Refresh display on main thread
                if hasattr(self, 'frame'):
                    submit(self.refresh_display)
                    
            except Exception as e:
                print(f"[StockMarket] History fetch error: {e}")
                import traceback
                traceback.print_exc()
            finally:
                loop.close()
        
        thread = threading.Thread(target=run_fetch, daemon=True)
        thread.start()
    
    def reload_filters_from_cache(self):
        """Reload fee rates from cached skills JSON for all hub panels.
        
        Called after tracking tab completes ESI refresh.
        """
        for panel in self.hub_panels.values():
            panel.reload_filters_from_cache()
    
    def refresh_display(self):
        """Refresh all hub panels."""
        for panel in self.hub_panels.values():
            panel.refresh_display()
    
    def _refresh_display_async(self):
        """Refresh all hub panels asynchronously (non-blocking).
        
        Used for startup and deferred refreshes.
        """
        for panel in self.hub_panels.values():
            panel.refresh_display_async()
    
    def _startup_refresh(self):
        """Initial load: show existing data then run material filter.
        
        Called once at startup via frame.after().  Each hub panel's
        apply_material_filter() checks the once-per-day tracker, so
        this is safe to call every startup.
        """
        self._apply_material_filter_all()
    
    def _on_profiles_ready(self):
        """Called when background import finishes building profiles.
        
        Runs material filter on all hubs which clears stale cache
        entries and refreshes the display with correct risk
        classifications.
        """
        print("[StockMarket] Profiles ready - running material filter")
        self._apply_material_filter_all()
    
    def _apply_material_filter_all(self):
        """Run material filter on every hub panel.
        
        Each panel checks its own once-per-day tracker.  If the filter
        already ran today for a hub, that hub falls back to a normal
        async refresh (reading existing cached results).
        """
        for panel in self.hub_panels.values():
            panel.apply_material_filter()
