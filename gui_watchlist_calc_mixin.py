"""Mixin providing the Max Buy Price calculator UI + logic for watchlist dialogs.

Used by AddItemDialog (gui_watchlist_add.py) and EditItemDialog (gui_watchlist_search.py).
The calc section is only built when self.show_max_buy_calc is True; calc methods
early-return when the section was not built, so they are safe no-ops in that case.

Host class contract (attributes the mixin reads):
    self.show_max_buy_calc : bool
    self.get_client        : Callable
    self.get_skills        : Optional[Callable]
    self.region_id         : int
    self.selected_item     : dict with keys {"type_id", "name"}
    self.price_under_var   : tk.StringVar
"""

import tkinter as tk
from tkinter import ttk
import asyncio
import threading

from calculate import get_broker_fee_rate, get_sales_tax_rate
from config import REQUEST_TIMEOUT
from tk_queue import submit


class MaxBuyCalcMixin:
    """Shared Max Buy Price calculator section + handlers."""

    def _init_calc_state(self):
        """Initialize calc state. Call from host __init__."""
        self.best_buy_price = None
        self.calculated_max_buy = None
        # UI handles set in _build_max_buy_calc_section; left as None so
        # _calc_ui_ready() can detect "section not built" cleanly.
        self.calc_btn = None
        self.calc_status_label = None
        self.best_buy_label = None
        self.max_buy_label = None
        self.use_price_btn = None

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
            best_buy = None
            error_msg = None

            try:
                client = self.get_client() if self.get_client else None
                if client:
                    import aiohttp

                    async def do_fetch():
                        client.reset_for_new_loop()
                        async with aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                        ) as session:
                            client.session = session
                            url = f"https://esi.evetech.net/latest/markets/{self.region_id}/orders/"
                            all_buy_orders = []
                            page = 1
                            while True:
                                async with session.get(url, params={
                                    "type_id": type_id,
                                    "order_type": "buy",
                                    "page": page
                                }) as resp:
                                    if resp.status != 200:
                                        break
                                    data = await resp.json()
                                    if not data:
                                        break
                                    all_buy_orders.extend(data)
                                    total_pages = int(resp.headers.get("X-Pages", 1))
                                    if page >= total_pages:
                                        break
                                    page += 1
                            return all_buy_orders

                    buy_orders = loop.run_until_complete(do_fetch())

                    if buy_orders:
                        best_buy = max(order["price"] for order in buy_orders)
                    else:
                        error_msg = "No buy orders found"
                else:
                    error_msg = "No client available"
            except Exception as e:
                error_msg = str(e)
                print(f"Max buy calc error: {e}")
            finally:
                loop.close()

            if error_msg:
                submit(lambda: self._update_calc_error(error_msg))
            else:
                submit(lambda: self._update_calc_display(best_buy))

        threading.Thread(target=fetch_thread, daemon=True).start()

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

    def _use_calculated_price(self):
        """Copy calculated max buy price into the Alert if price UNDER field."""
        if self.calculated_max_buy is not None and hasattr(self, "price_under_var"):
            self.price_under_var.set(str(int(self.calculated_max_buy)))
