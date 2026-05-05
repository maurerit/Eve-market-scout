"""Scan logic mixin for MarketScoutGUI.

Extracts scan execution and results display from gui_main.py.

Expected parent attributes:
    root: tk.Tk - main window
    scan_callback: Callable - async scan function
    is_scanning: bool - scan in progress flag
    auto_refresh_enabled: bool - auto-refresh state
    sound_enabled: bool - sound alert state
    buy_station: str - current buy station key
    sell_station: str - current sell station key
    force_jita_refresh: bool - flag to refresh Jita cache
    next_refresh_seconds: int - ESI cache timing
    previous_deal_ids: set[int] - deal IDs from last scan
    deals: list[Deal] - current deals
    
    Widgets expected:
    scan_btn, jita_btn, buy_station_dropdown, sell_station_dropdown
    progress, status_label, count_label
    
    Managers expected:
    filter_manager, deals_manager, crosshub_display_manager
    watchlist_manager, npc_orders_manager, stock_market_tab
    tracking_manager
    
    Methods expected:
    is_crosshub_mode() -> bool
    _schedule_auto_refresh()
    _update_jita_status()
    _play_alert()
"""

import asyncio
import threading
import tkinter as tk
from tkinter import messagebox

from tk_queue import submit
from scanner_common import Deal, ScanResult
from scanner import CrossHubScanResult
from config import AUTO_REFRESH_INTERVAL, get_hub_config
from sound_manager import play_alert


def _check_thread(context: str):
    """Debug helper - warn if not on main thread."""
    current = threading.current_thread()
    if current is not threading.main_thread():
        print(f"[THREAD WARNING] {context} called from {current.name}")
        import traceback
        traceback.print_stack(limit=8)


class MainScanMixin:
    """Mixin providing scan execution and results display."""

    def _check_first_time_setup(self) -> bool:
        """Check if scanner has minimum data. Returns True if ready to scan.
        
        Scanner only needs 30 days of recent data. This is a quick download
        (~60MB, 1-2 minutes) compared to full 3-year archive.
        
        Stock Market features will prompt for full history separately.
        """
        from market_history import get_market_history_db
        from gui_migration import check_has_recent_data, ensure_scanner_data
        
        try:
            db = get_market_history_db()
            
            # Check if we have enough recent data for scanner
            if check_has_recent_data(db):
                return True
                
        except Exception as e:
            print(f"[Setup] Error checking database: {e}")
        
        # Need to download scanner data
        result = messagebox.askyesno(
            "Scanner Setup Required",
            "EVE Market Scout needs to download recent market data.\n\n"
            "This will download 30 days of price history (~60 MB).\n"
            "Takes about 1-2 minutes.\n\n"
            "Continue?"
        )
        
        if not result:
            return False
        
        # Download scanner minimum data
        from market_history import get_market_history_db
        db = get_market_history_db()
        
        success = ensure_scanner_data(self.root, db)
        
        if success:
            self.status_label.configure(text="Scanner ready!")
            return True
        else:
            messagebox.showwarning(
                "Setup Incomplete",
                "Scanner data download failed or was cancelled.\n"
                "Some features may not work correctly."
            )
            return False
    
    def _run_first_time_setup(self):
        """Legacy method - now handled by _check_first_time_setup."""
        pass

    def _auto_refresh(self):
        """Triggered by auto-refresh timer."""
        if self.auto_refresh_enabled and not self.is_scanning:
            self._start_scan(is_auto=True)

    def _start_scan(self, is_auto=False):
        """Start the market scan in a background thread."""
        if self.is_scanning:
            return
        
        # First-time setup check (skip for auto-refresh)
        if not is_auto and not self._check_first_time_setup():
            return

        self.is_scanning = True
        self.scan_btn.configure(state=tk.DISABLED)
        self.jita_btn.configure(state=tk.DISABLED)
        self.buy_station_dropdown.configure(state=tk.DISABLED)
        self.sell_station_dropdown.configure(state=tk.DISABLED)
        self.progress["value"] = 0

        if is_auto:
            self.status_label.configure(text="Auto-refreshing...")

        # Get filter values from FilterManager
        filter_values = self.filter_manager.get_filter_values()
        
        # Check if Jita refresh was requested
        refresh_jita = self.force_jita_refresh
        self.force_jita_refresh = False

        thread = threading.Thread(
            target=self._run_scan_thread,
            args=(is_auto, filter_values, refresh_jita),
            daemon=True
        )
        thread.start()

    def _run_scan_thread(self, is_auto, filter_values, refresh_jita):
        """Thread target that runs the async scan."""
        print(f"[THREAD DEBUG] _run_scan_thread starting on {threading.current_thread().name}")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        error_msg = None
        scan_result = None
        refresh_seconds = 0

        min_profit, min_total, max_cost, min_margin, min_volume = filter_values
        
        # Get skills from tracking manager
        seller_skills = None
        buyer_skills = None
        if self.tracking_manager:
            _check_thread("_run_scan_thread -> tracking_manager.get_skills()")
            seller_skills = self.tracking_manager.get_skills()
            # For cross-hub, we'd need buyer skills too
            # TODO: Get buyer skills when second character is implemented
            buyer_skills = seller_skills  # For now, use same skills

        try:
            # Determine scan mode
            if self.is_crosshub_mode():
                # Cross-hub arbitrage scan
                result = loop.run_until_complete(
                    self.scan_callback(
                        self._update_progress,
                        min_profit_per_unit=min_profit,
                        min_total_profit=min_total,
                        max_cost=max_cost,
                        min_margin_percent=min_margin,
                        min_daily_volume=min_volume,
                        refresh_jita=refresh_jita,
                        skills=seller_skills,
                        hub=self.sell_station,  # Legacy - sell station
                        # Cross-hub specific
                        crosshub_mode=True,
                        buy_station=self.buy_station,
                        sell_station=self.sell_station,
                        buyer_skills=buyer_skills,
                        seller_skills=seller_skills,
                    )
                )
            else:
                # Same-station scan (original behavior)
                result = loop.run_until_complete(
                    self.scan_callback(
                        self._update_progress,
                        min_profit_per_unit=min_profit,
                        min_total_profit=min_total,
                        max_cost=max_cost,
                        min_margin_percent=min_margin,
                        min_daily_volume=min_volume,
                        refresh_jita=refresh_jita,
                        skills=seller_skills,
                        hub=self.sell_station
                    )
                )
            
            scan_result, refresh_seconds = result
        except Exception as e:
            error_msg = str(e)
            import traceback
            traceback.print_exc()
        finally:
            loop.close()

        # Schedule UI updates on main thread
        if error_msg:
            submit(lambda msg=error_msg: self._show_error(msg))
        else:
            submit(lambda r=refresh_seconds: self._set_refresh_timing(r))
            submit(lambda sr=scan_result, a=is_auto: self._display_deals(sr, a))

        submit(self._scan_complete)

    def _set_refresh_timing(self, seconds: float):
        """Store ESI cache expiry timing for next refresh."""
        self.next_refresh_seconds = int(seconds) if seconds > 0 else AUTO_REFRESH_INTERVAL

    def _scan_complete(self):
        """Called when scan finishes."""
        self.is_scanning = False
        self.scan_btn.configure(state=tk.NORMAL)
        self.jita_btn.configure(state=tk.NORMAL)
        self.buy_station_dropdown.configure(state="readonly")
        self.sell_station_dropdown.configure(state="readonly")
        try:
            self._update_jita_status()
        except Exception as e:
            print(f"Error updating jita status: {e}")
        if self.auto_refresh_enabled:
            self._schedule_auto_refresh()

    def _update_progress(self, status: str, percent: int):
        """Update progress bar and status."""
        submit(lambda: self._do_update_progress(status, percent))

    def _do_update_progress(self, status: str, percent: int):
        """Actually update the UI."""
        self.status_label.configure(text=status)
        self.progress["value"] = percent

    def _display_deals(self, scan_result, is_auto=False):
        """Display deals and update watchlist with local hub orders."""
        # Handle CrossHubScanResult (different display format)
        if isinstance(scan_result, CrossHubScanResult):
            self._display_crosshub_deals(scan_result, is_auto)
            return
        
        # Handle normal ScanResult format
        if isinstance(scan_result, ScanResult):
            steals = scan_result.steals
            low_risk = scan_result.low_risk
            high_risk = scan_result.high_risk
            local_orders = scan_result.local_orders
            local_orders_filtered = scan_result.local_orders_filtered
        else:
            # Fallback for old format (shouldn't happen)
            steals = []
            low_risk = scan_result if scan_result else []
            high_risk = []
            local_orders = []
            local_orders_filtered = []

        # Combine all deals for tracking
        all_deals = steals + low_risk + high_risk
        self.deals = all_deals
        
        # Determine which orders to use for watchlists based on hub_only filter
        # Hub Only ON: only hub station orders
        # Hub Only OFF: all high-sec orders (local_orders_filtered)
        if self.filter_manager.hub_only_var and self.filter_manager.hub_only_var.get():
            # Filter to hub station only
            hub_config = get_hub_config(self.sell_station)
            hub_station_id = hub_config["station_id"]
            watchlist_orders = [o for o in local_orders if o.get("location_id") == hub_station_id]
        else:
            # Use high-sec filtered orders
            watchlist_orders = local_orders_filtered if local_orders_filtered else local_orders
        
        # Update watchlist with current local hub prices
        # Always call even if empty - clears stale prices for items with no listings
        if self.watchlist_manager:
            self.watchlist_manager.update_from_local_orders(watchlist_orders)
        
        # Update NPC orders with current local hub prices
        if self.npc_orders_manager:
            self.npc_orders_manager.update_from_local_orders(watchlist_orders)
        
        # Update Stock Market tab with current local hub prices
        if self.stock_market_tab:
            sell_config = get_hub_config(self.sell_station)
            self.stock_market_tab.update_from_local_orders(watchlist_orders, sell_config["region_id"])
            # Material filter tracking now handled by HubPanel.refresh_display()
        
        # Display categorized deals (returns count of new alert-worthy deals: steals + low_risk)
        new_alert_count = self.deals_manager.display_categorized_deals(
            steals, low_risk, high_risk, 
            self.previous_deal_ids, is_auto
        )
        
        # Also check watchlist for alerts
        watchlist_alerts = self.watchlist_manager.get_alert_items() if self.watchlist_manager else []
        npc_alerts = self.npc_orders_manager.get_alert_items() if self.npc_orders_manager else []
        
        if new_alert_count > 0 and is_auto and self.sound_enabled:
            self._play_alert()
            self.status_label.configure(text=f"Found {new_alert_count} new deal(s)!")
        elif (watchlist_alerts or npc_alerts) and is_auto and self.sound_enabled:
            self._play_alert()
            total_alerts = len(watchlist_alerts) + len(npc_alerts)
            self.status_label.configure(text=f"Alerts: {total_alerts} item(s)!")
        
        # Update previous deal IDs for next comparison
        self.previous_deal_ids = self.deals_manager.get_current_deal_ids()
        
        # Update count label with breakdown
        total = len(steals) + len(low_risk) + len(high_risk)
        self.count_label.configure(text=f"Deals: {total} (S:{len(steals)} L:{len(low_risk)} H:{len(high_risk)})")

    def _display_crosshub_deals(self, scan_result: CrossHubScanResult, is_auto=False):
        """Display cross-hub deals with dual-row format."""
        low_risk = scan_result.low_risk
        high_risk = scan_result.high_risk
        
        # Store deals
        self.deals = low_risk + high_risk
        
        # Update Stock Market tab with sell station prices
        if scan_result.sell_station_orders and self.stock_market_tab:
            sell_config = get_hub_config(self.sell_station)
            self.stock_market_tab.update_from_local_orders(scan_result.sell_station_orders, sell_config["region_id"])
            # Material filter tracking now handled by HubPanel.refresh_display()
        
        # Configure trees for crosshub display if not already done
        if not hasattr(self, '_crosshub_trees_configured'):
            self.crosshub_display_manager.configure_tree_for_crosshub(
                self.deals_manager.low_risk_tree
            )
            self.crosshub_display_manager.configure_tree_for_crosshub(
                self.deals_manager.high_risk_tree
            )
            self._crosshub_trees_configured = True
        
        # Display using crosshub manager's dual-row format
        new_alert_count = self.crosshub_display_manager.display_crosshub_deals(
            low_risk, high_risk,
            self.deals_manager.low_risk_tree,
            self.deals_manager.high_risk_tree,
            self.previous_deal_ids, is_auto
        )
        
        # Update Steals tab to show empty (cross-hub doesn't have steals)
        for item in self.deals_manager.steals_tree.get_children():
            self.deals_manager.steals_tree.delete(item)
        self.deals_manager.notebook.tab(2, text="Steals (0)")
        
        # Handle alerts
        if new_alert_count > 0 and is_auto and self.sound_enabled:
            self._play_alert()
            self.status_label.configure(text=f"Found {new_alert_count} new deal(s)!")
        
        # Update previous deal IDs
        self.previous_deal_ids = self.crosshub_display_manager.get_current_deal_ids()
        
        # Update count label
        total = len(low_risk) + len(high_risk)
        self.count_label.configure(text=f"Cross-Hub Deals: {total} (L:{len(low_risk)} H:{len(high_risk)})")

    def _show_error(self, message: str):
        """Show error dialog."""
        self.status_label.configure(text="Error occurred")
        messagebox.showerror("Scan Error", f"Failed to scan market:\n{message}")

    def _play_alert(self):
        """Play custom alert sound in a cross-platform way."""
        play_alert()
