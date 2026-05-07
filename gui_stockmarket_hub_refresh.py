"""Refresh-cycle mixin for StockMarketHubPanel.

Extracted to keep gui_stockmarket_hub.py under 700 lines.
Provides: refresh_display_async, _get_trend_for_data, _li_lookup_for_data,
          _get_trend_tag_for_data, _apply_refresh_data, _get_floor_trend.
"""

import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable

from tk_queue import submit


def _fmt_ago(dt_utc, now_utc=None) -> str:
    if dt_utc is None:
        return "--"
    now = now_utc or datetime.now(timezone.utc)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    secs = (now - dt_utc).total_seconds()
    if secs < 60:
        return "just now"
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins}m ago"
    h = mins // 60
    m = mins % 60
    return f"{h}h {m}m ago" if m else f"{h}h ago"


def _fmt_until(dt_utc, now_utc=None) -> str:
    if dt_utc is None:
        return "--"
    now = now_utc or datetime.now(timezone.utc)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    secs = (dt_utc - now).total_seconds()
    if secs <= 0:
        return "due"
    if secs < 60:
        return "< 1m"
    mins = int(secs // 60)
    if mins < 60:
        return f"in {mins}m"
    h = mins // 60
    m = mins % 60
    return f"in {h}h {m}m" if m else f"in {h}h"


class HubPanelRefreshMixin:
    """Display-refresh cycle for a single hub panel.

    Expects the host class to provide: hub_key, region_id, profiles,
    risk_panels, holdings_panel, live_prices, _update_ticker().
    """

    def render_from_cache(self, order_cache) -> bool:
        """Update live prices from the region's cached orders. No ESI call.

        Returns True if the cache had sell orders to apply.
        """
        entry = order_cache._order_cache.get(self.region_id, {})
        orders = entry.get("orders", [])
        if not orders:
            return False
        prices: dict = {}
        for order in orders:
            if order.get("is_buy_order"):
                continue
            type_id = order["type_id"]
            price = order["price"]
            if type_id not in prices or price < prices[type_id]:
                prices[type_id] = price
        if prices:
            self.update_live_prices(prices)
        self.update_refresh_labels(order_cache)
        return bool(prices)

    def update_refresh_labels(self, order_cache):
        """Update last_refreshed / next_refresh labels from the order cache."""
        now = datetime.now(timezone.utc)
        entry = order_cache._order_cache.get(self.region_id, {})
        ts = entry.get("timestamp")
        expires = entry.get("expires")
        self._last_refreshed_var.set(f"Updated: {_fmt_ago(ts, now)}")
        nxt = expires or (ts + timedelta(minutes=5) if ts else None)
        self._next_refresh_var.set(f"Next: {_fmt_until(nxt, now)}")

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
            try:
                sde = get_sde_manager()

                self._li_cache_for_routing = None

                risk_data = {"low": [], "medium": [], "high": []}
                all_profiles = self.profiles.get_all_profiles()
                region_profiles = [
                    p for p in all_profiles if p.region_id == self.region_id
                ]

                print(
                    f"[StockMarket-{self.hub_key}] Found {len(all_profiles)} total "
                    f"profiles, {len(region_profiles)} for region {self.region_id}"
                )

                all_stats = self.profiles.get_all_yearly_stats_for_region(
                    self.region_id, context_label=self.hub_key
                )

                for profile in region_profiles:
                    yearly_stats = all_stats.get(profile.type_id, {})
                    trend = self._get_trend_for_data(yearly_stats, profile)
                    if trend not in risk_data:
                        continue

                    type_name = (
                        sde.get_type_name(profile.type_id) or f"Type {profile.type_id}"
                    )
                    current_price = self.live_prices.get(profile.type_id, 0)
                    trend_tag = self._get_trend_tag_for_data(yearly_stats)

                    risk_data[trend].append(
                        {
                            "type_id": profile.type_id,
                            "type_name": type_name,
                            "profile": profile,
                            "current_price": current_price,
                            "trend_tag": trend_tag,
                        }
                    )

                for risk_level, items in risk_data.items():
                    print(
                        f"[StockMarket-{self.hub_key}] {risk_level}: {len(items)} items"
                    )

                submit(lambda: self._apply_refresh_data(risk_data, after=after))

            except Exception as e:
                print(
                    f"[StockMarket-{self.hub_key}] refresh_display_async ERROR: {e}"
                )
                import traceback
                traceback.print_exc()
                if after is not None:
                    submit(after)

        threading.Thread(target=gather_data, daemon=True).start()

    def _get_trend_for_data(self, yearly_stats: dict, profile) -> str:
        """Determine trend from yearly stats (thread-safe, no UI calls).

        Reads from the pre-populated material risk cache but never
        triggers fresh analysis.  apply_material_filter() is the only
        entry-point that clears and re-populates the cache.

        Leading indicators promotion: UNDERCUT SPIRAL or LIQUIDITY DRAIN
        bumps the item one tier up (low->medium, medium->high).
        """
        if len(yearly_stats) < 2:
            return "none"

        years = sorted(yearly_stats.keys(), reverse=True)
        floors = [yearly_stats[y].p_low for y in years[:3]]

        if len(floors) < 2:
            return "none"

        declining = all(floors[i] < floors[i + 1] for i in range(len(floors) - 1))
        if declining:
            return "high"

        base_tier = None
        rising = all(floors[i] > floors[i + 1] for i in range(len(floors) - 1))
        if rising:
            base_tier = "medium"
        else:
            if len(floors) >= 2:
                avg_floor = sum(floors) / len(floors)
                if avg_floor > 0:
                    max_deviation = max(
                        abs(f - avg_floor) / avg_floor * 100 for f in floors
                    )
                    if max_deviation <= 15:
                        if profile:
                            from stockmarket_filters import get_cached_material_risk
                            cached = get_cached_material_risk(
                                profile.type_id, self.region_id
                            )
                            base_tier = "medium" if cached == "medium" else "low"
                        else:
                            base_tier = "low"

        if base_tier is None:
            return "none"

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
                print(
                    f"[StockMarket-{self.hub_key}] LI routing cache load error: {e}"
                )
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
                max_deviation = max(
                    abs(f - avg_floor) / avg_floor * 100 for f in floors
                )
                if max_deviation <= 15:
                    return "trend_stable"

        return "trend_none"

    def _apply_refresh_data(
        self, risk_data: dict, after: Optional[Callable[[], None]] = None
    ):
        """Apply gathered data to UI (main thread only).

        Args:
            risk_data: Pre-classified items per risk level.
            after: Optional callback invoked after the UI has been
                updated.  Used to hide the lock overlay.
        """
        print(f"[StockMarket-{self.hub_key}] _apply_refresh_data called")

        for risk_level, items in risk_data.items():
            panel = self.risk_panels.get(risk_level)
            if panel:
                print(
                    f"[StockMarket-{self.hub_key}] Applying {len(items)} items "
                    f"to {risk_level} panel"
                )
                panel.refresh_from_data(items)

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

        declining = all(floors[i] < floors[i + 1] for i in range(len(floors) - 1))
        if declining:
            return "high"

        rising = all(floors[i] > floors[i + 1] for i in range(len(floors) - 1))
        if rising:
            return "medium"

        if len(floors) >= 2:
            avg_floor = sum(floors) / len(floors)
            if avg_floor > 0:
                max_dev = max(
                    abs(f - avg_floor) / avg_floor * 100 for f in floors
                )
                if max_dev <= 15:
                    return "low"

        return "none"
