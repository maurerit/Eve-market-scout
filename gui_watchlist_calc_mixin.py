"""Mixin providing the Max Buy Price calculator UI + logic for watchlist dialogs.

Used by AddItemDialog (gui_watchlist_add.py) and EditItemDialog (gui_watchlist_search.py).
The calc section is only built when self.show_max_buy_calc is True; calc methods
early-return when the section was not built, so they are safe no-ops in that case.

Host class contract (attributes the mixin reads):
    self.show_max_buy_calc    : bool
    self.get_client           : Callable
    self.get_skills           : Optional[Callable]
    self.region_id            : int
    self.selected_item        : dict with keys {"type_id", "name"}
    self.price_under_var      : tk.StringVar

  Optional, only consulted when nearest_station_mode is True (NPC Orders flow):
    self.nearest_station_mode : bool  -- enables jump-filter + buyer-station rep
    self.get_origin_system    : () -> int system_id (origin for jump filter)
    self.get_esi_standings    : () -> ESIStandings (rep against any corp/faction)
    self.max_jumps            : int  -- defaults to 6
"""

import tkinter as tk
from tkinter import ttk
import asyncio
import threading

from calculate import (
    get_broker_fee_rate, get_sales_tax_rate, TradingSkills, DEFAULT_SKILLS,
)
from config import REQUEST_TIMEOUT
from tk_queue import submit


class MaxBuyCalcMixin:
    """Shared Max Buy Price calculator section + handlers."""

    def _init_calc_state(self):
        """Initialize calc state. Call from host __init__."""
        self.best_buy_price = None
        self.calculated_max_buy = None
        # Nearest-station mode (NPC Orders flow). Host overrides AFTER
        # _init_calc_state and BEFORE _build_max_buy_calc_section. Defaults
        # keep the original watchlist behavior unchanged.
        self.nearest_station_mode = False
        self.get_origin_system = None
        self.get_esi_standings = None
        self.max_jumps = 6
        # UI handles set in _build_max_buy_calc_section; left as None so
        # _calc_ui_ready() can detect "section not built" cleanly.
        self.calc_btn = None
        self.calc_status_label = None
        self.best_buy_label = None
        self.max_buy_label = None
        self.use_price_btn = None
        self.nearest_station_label = None
        self.nearest_distance_label = None
        self.nearest_standings_label = None
        self.nearest_tax_label = None

    def _calc_ui_ready(self) -> bool:
        """True only when the calc UI has actually been built."""
        return getattr(self, "show_max_buy_calc", False) and self.calc_btn is not None

    def _build_max_buy_calc_section(self, parent=None):
        """Build the Max Buy Price Calculator UI inside `parent` (defaults to self).
        
        No-op when self.show_max_buy_calc is False.
        """
        if not getattr(self, "show_max_buy_calc", False):
            return

        if parent is None:
            parent = self

        calc_frame = ttk.LabelFrame(parent, text="Max Buy Price Calculator", padding=10)
        calc_frame.pack(fill=tk.X, padx=10, pady=5)

        calc_btn_row = ttk.Frame(calc_frame)
        calc_btn_row.pack(fill=tk.X)

        self.calc_btn = ttk.Button(calc_btn_row, text="Calculate Max Buy", command=self._calculate_max_buy)
        self.calc_btn.pack(side=tk.LEFT)

        self.calc_status_label = ttk.Label(calc_btn_row, text="", font=("Segoe UI", 8), foreground="gray")
        self.calc_status_label.pack(side=tk.LEFT, padx=10)

        calc_results = ttk.Frame(calc_frame)
        calc_results.pack(fill=tk.X, pady=(5, 0))

        ttk.Label(calc_results, text="Best Regional Buy:").pack(anchor=tk.W)
        self.best_buy_label = ttk.Label(calc_results, text="--", font=("Segoe UI", 9))
        self.best_buy_label.pack(anchor=tk.W, padx=(15, 0))

        if self.nearest_station_mode:
            # Nearest-station surfacing: show which buyer the calc picked,
            # where they are, and how that station's rep changes the tax.
            ttk.Label(calc_results, text="Best buy station:").pack(anchor=tk.W, pady=(5, 0))
            self.nearest_station_label = ttk.Label(
                calc_results, text="—", font=("Segoe UI", 9)
            )
            self.nearest_station_label.pack(anchor=tk.W, padx=(15, 0))

            ttk.Label(calc_results, text="Distance:").pack(anchor=tk.W, pady=(3, 0))
            self.nearest_distance_label = ttk.Label(
                calc_results, text="—", font=("Segoe UI", 9)
            )
            self.nearest_distance_label.pack(anchor=tk.W, padx=(15, 0))

            ttk.Label(calc_results, text="Standings @ sell station:").pack(anchor=tk.W, pady=(3, 0))
            self.nearest_standings_label = ttk.Label(
                calc_results, text="—", font=("Segoe UI", 9)
            )
            self.nearest_standings_label.pack(anchor=tk.W, padx=(15, 0))

            ttk.Label(calc_results, text="Tax @ that rep:").pack(anchor=tk.W, pady=(3, 0))
            self.nearest_tax_label = ttk.Label(
                calc_results, text="—", font=("Segoe UI", 9)
            )
            self.nearest_tax_label.pack(anchor=tk.W, padx=(15, 0))

        ttk.Label(calc_results, text="Max Buy Price (1% profit):").pack(anchor=tk.W, pady=(3, 0))
        self.max_buy_label = ttk.Label(calc_results, text="--", font=("Segoe UI", 10, "bold"), foreground="green")
        self.max_buy_label.pack(anchor=tk.W, padx=(15, 0))

        self.use_price_btn = ttk.Button(
            calc_frame, text="Use as Alert Price",
            command=self._use_calculated_price, state=tk.DISABLED
        )
        self.use_price_btn.pack(anchor=tk.W, pady=(5, 0))

    def _calculate_max_buy(self):
        """Fetch regional buy orders and calculate max profitable buy price."""
        # Safe no-op when the calc section wasn't built (e.g. personal watchlist)
        if not self._calc_ui_ready():
            return

        selected = getattr(self, "selected_item", None)
        if not selected:
            self.calc_status_label.configure(text="Select an item first")
            return

        type_id = selected["type_id"]
        self.calc_status_label.configure(text="Fetching buy orders...")
        self.calc_btn.configure(state=tk.DISABLED)
        self.best_buy_label.configure(text="...")
        self.max_buy_label.configure(text="...")
        self.use_price_btn.configure(state=tk.DISABLED)

        def fetch_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            payload = None
            error_msg = None

            try:
                client = self.get_client() if self.get_client else None
                if not client:
                    error_msg = "No client available"
                else:
                    payload, error_msg = loop.run_until_complete(
                        self._async_resolve_best_buy(client, type_id)
                    )
            except Exception as e:
                error_msg = str(e)
                print(f"Max buy calc error: {e}")
            finally:
                loop.close()

            if error_msg:
                submit(lambda: self._update_calc_error(error_msg))
            elif self.nearest_station_mode:
                submit(lambda: self._update_calc_display_nearest(payload))
            else:
                submit(lambda: self._update_calc_display(payload["best_buy"]))

        threading.Thread(target=fetch_thread, daemon=True).start()

    async def _async_resolve_best_buy(self, client, type_id: int):
        """Fetch buy orders and (in nearest mode) resolve station + jumps.

        Returns (payload, error_msg). Exactly one of payload/error_msg is
        non-None.
          - non-nearest: payload = {"best_buy": float}
          - nearest:     payload = {"best_buy", "location_id", "system_id",
                                    "jumps", "station_info"}
        """
        import aiohttp
        from gui_jump_cache import JumpCache
        from gui_station_lookup import StationLookup, PLAYER_STRUCTURE_ID_THRESHOLD

        client.reset_for_new_loop()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        ) as session:
            client.session = session

            # Paginate buy orders for this type in the configured region.
            url = f"https://esi.evetech.net/latest/markets/{self.region_id}/orders/"
            all_orders = []
            page = 1
            while True:
                async with session.get(url, params={
                    "type_id": type_id,
                    "order_type": "buy",
                    "page": page,
                }) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                    if not data:
                        break
                    all_orders.extend(data)
                    total_pages = int(resp.headers.get("X-Pages", 1))
                    if page >= total_pages:
                        break
                    page += 1

            if not all_orders:
                return (None, "No buy orders found")

            if not self.nearest_station_mode:
                return ({"best_buy": max(o["price"] for o in all_orders)}, None)

            # --- Nearest-station mode -------------------------------------
            # NPC sales tax doesn't apply the same way at player structures
            # (structure owner sets a market tax outside the rep system).
            candidates = [
                o for o in all_orders
                if o.get("location_id", 0) < PLAYER_STRUCTURE_ID_THRESHOLD
            ]
            if not candidates:
                return (None, "Only player-structure buy orders -- skipping")

            origin = self.get_origin_system() if self.get_origin_system else None
            if origin is None:
                return (None, "Origin system not configured")

            # Resolve jumps for each unique system once (cached across calls).
            unique_systems = {o["system_id"] for o in candidates if o.get("system_id")}
            jc = JumpCache.singleton()
            jumps_map: dict[int, int] = {}
            for sys_id in unique_systems:
                j = await jc.fetch(session, origin, sys_id)
                if j is not None:
                    jumps_map[sys_id] = j

            in_range = [
                o for o in candidates
                if jumps_map.get(o.get("system_id"), 999) <= self.max_jumps
            ]
            if not in_range:
                return (None,
                        f"No buy orders within {self.max_jumps} jumps of origin")

            best = max(in_range, key=lambda o: o["price"])

            sl = StationLookup.singleton()
            station_info = await sl.fetch(session, best["location_id"])

            return ({
                "best_buy": best["price"],
                "location_id": best["location_id"],
                "system_id": best.get("system_id"),
                "jumps": jumps_map.get(best.get("system_id")),
                "station_info": station_info,
            }, None)

    def _update_calc_error(self, msg: str):
        """Display calculation error."""
        if not self._calc_ui_ready():
            return
        self.calc_status_label.configure(text=f"Error: {msg[:40]}")
        self.calc_btn.configure(state=tk.NORMAL)
        self.best_buy_label.configure(text="--")
        self.max_buy_label.configure(text="--")

    def _update_calc_display(self, best_buy: float):
        """Calculate and display max buy price from best regional buy order."""
        if not self._calc_ui_ready():
            return

        self.best_buy_price = best_buy
        self.best_buy_label.configure(text=f"{best_buy:,.2f} ISK")

        # Get current skills (with standings/manual overrides) for fee calc
        skills = None
        if getattr(self, "get_skills", None):
            skills = self.get_skills()

        broker_rate = get_broker_fee_rate(skills) / 100.0
        tax_rate = get_sales_tax_rate(skills) / 100.0
        target_margin = 0.01  # 1% profit target

        # Instant-buy workflow: we buy from existing sell orders (no buy-side
        # broker fee), then sell our own sell order (broker + sales tax).
        # net_revenue = best_buy * (1 - broker_rate - tax_rate)   # what we pocket
        # For 1% profit: net_revenue / max_buy >= 1.01
        # => max_buy = net_revenue / (1 + target_margin)
        net_revenue = best_buy * (1.0 - broker_rate - tax_rate)
        max_buy = net_revenue / (1.0 + target_margin)

        self.calculated_max_buy = max_buy
        self.max_buy_label.configure(text=f"{max_buy:,.2f} ISK")

        fee_info = f"Broker: {broker_rate*100:.2f}% | Tax: {tax_rate*100:.2f}%"
        if skills:
            fee_info += " (from skills)"
        else:
            fee_info += " (default - no skills)"
        self.calc_status_label.configure(text=fee_info)

        self.calc_btn.configure(state=tk.NORMAL)
        self.use_price_btn.configure(state=tk.NORMAL)

    def _update_calc_display_nearest(self, payload: dict):
        """Display nearest-station calc results: surfaces station, distance,
        the user's rep at that station, and the tax that rep produces -- so
        the user can verify which buyer the 1%-profit max-buy is based on.
        """
        if not self._calc_ui_ready():
            return

        best_buy = payload["best_buy"]
        station_info = payload.get("station_info") or {}
        jumps = payload.get("jumps")
        location_id = payload.get("location_id")

        self.best_buy_price = best_buy
        self.best_buy_label.configure(text=f"{best_buy:,.2f} ISK")

        station_name = station_info.get("name") or f"Station {location_id}"
        self.nearest_station_label.configure(text=station_name)
        if jumps is not None:
            self.nearest_distance_label.configure(text=f"{jumps} jump(s)")
        else:
            self.nearest_distance_label.configure(text="?")

        # Look up user's standings against the station's corp + faction.
        corp_standing = 0.0
        faction_standing = 0.0
        standings_source = "no standings"
        standings_obj = self.get_esi_standings() if self.get_esi_standings else None
        corp_id = station_info.get("corp_id")
        faction_id = station_info.get("faction_id")
        if standings_obj:
            if corp_id:
                corp_standing = standings_obj.get_corp_standing(corp_id, slot="seller")
            if faction_id:
                faction_standing = standings_obj.get_faction_standing(faction_id, slot="seller")
            standings_source = "from ESI"

        self.nearest_standings_label.configure(
            text=f"Corp {corp_standing:.2f}  ·  Faction {faction_standing:.2f}  "
                 f"({standings_source})"
        )

        # Build an adjusted skills object: keep broker_relations / accounting /
        # advanced_broker_relations / manual overrides from the user's current
        # skills, but substitute the buyer-station standings for fee math.
        base = self.get_skills() if self.get_skills else DEFAULT_SKILLS
        if base is None:
            base = DEFAULT_SKILLS
        adjusted = TradingSkills(
            broker_relations=base.broker_relations,
            accounting=base.accounting,
            advanced_broker_relations=base.advanced_broker_relations,
            station_standing=corp_standing,
            faction_standing=faction_standing,
            manual_broker_fee=base.manual_broker_fee,
            manual_sales_tax=base.manual_sales_tax,
        )
        broker_rate = get_broker_fee_rate(adjusted) / 100.0
        tax_rate = get_sales_tax_rate(adjusted) / 100.0
        self.nearest_tax_label.configure(text=f"{tax_rate*100:.2f}%")

        # Same instant-buy formula as the original calc, but with the
        # station-specific tax_rate.
        target_margin = 0.01
        net_revenue = best_buy * (1.0 - broker_rate - tax_rate)
        max_buy = net_revenue / (1.0 + target_margin)

        self.calculated_max_buy = max_buy
        self.max_buy_label.configure(text=f"{max_buy:,.2f} ISK")

        self.calc_status_label.configure(
            text=f"Broker: {broker_rate*100:.2f}%  ·  "
                 f"Tax: {tax_rate*100:.2f}% @ this station's rep"
        )
        self.calc_btn.configure(state=tk.NORMAL)
        self.use_price_btn.configure(state=tk.NORMAL)

    def _use_calculated_price(self):
        """Copy calculated max buy price into the Alert if price UNDER field."""
        if self.calculated_max_buy is not None and hasattr(self, "price_under_var"):
            self.price_under_var.set(str(int(self.calculated_max_buy)))
