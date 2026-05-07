"""Dialog for searching and adding a custom NPC station."""

import tkinter as tk
from tkinter import ttk, messagebox
import asyncio
import threading
from typing import Callable, Optional

from tk_queue import submit
from config import TRADE_HUBS
from custom_stations import add_custom_station, get_custom_hub_key, is_custom_hub


class AddStationDialog(tk.Toplevel):
    """Modal dialog for looking up and adding a custom NPC station."""

    def __init__(
        self,
        parent,
        get_client: Callable,
        on_station_added: Callable[[str], None],
        on_add_to_stockmarket: Callable[[str], None],
    ):
        super().__init__(parent)
        self.get_client = get_client
        self.on_station_added = on_station_added
        self.on_add_to_stockmarket = on_add_to_stockmarket

        self._results: list[dict] = []
        self._selected: Optional[dict] = None

        self.title("Add Custom Station")
        self.geometry("520x380")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._create_widgets()
        self.after(100, lambda: self.search_entry.focus_set())

    # -------------------------------------------------------------------------

    def _create_widgets(self):
        # Search row
        search_frame = ttk.Frame(self, padding=(10, 10, 10, 5))
        search_frame.pack(fill=tk.X)

        ttk.Label(search_frame, text="Station name:").pack(side=tk.LEFT)

        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=32)
        self.search_entry.pack(side=tk.LEFT, padx=(6, 6))
        self.search_entry.bind("<Return>", lambda _: self._do_search())

        self.search_btn = ttk.Button(search_frame, text="Search", command=self._do_search)
        self.search_btn.pack(side=tk.LEFT)

        # Status / hint
        self.hint_var = tk.StringVar(value="Type a station name and press Search.")
        ttk.Label(self, textvariable=self.hint_var, font=("Segoe UI", 8),
                  foreground="gray").pack(anchor=tk.W, padx=12)

        # Results listbox
        list_frame = ttk.Frame(self, padding=(10, 4, 10, 4))
        list_frame.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self.listbox = tk.Listbox(
            list_frame,
            yscrollcommand=scrollbar.set,
            selectmode=tk.SINGLE,
            font=("Segoe UI", 9),
            activestyle="none",
        )
        scrollbar.config(command=self.listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.pack(fill=tk.BOTH, expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<Double-Button-1>", lambda _: self._on_add())

        # Options row
        opt_frame = ttk.Frame(self, padding=(10, 4, 10, 4))
        opt_frame.pack(fill=tk.X)

        self.stock_market_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_frame,
            text="Also add to Stock Market",
            variable=self.stock_market_var,
        ).pack(side=tk.LEFT)

        # Buttons
        btn_frame = ttk.Frame(self, padding=(10, 6, 10, 10))
        btn_frame.pack(fill=tk.X)

        self.add_btn = ttk.Button(
            btn_frame, text="Add Station", command=self._on_add, state=tk.DISABLED
        )
        self.add_btn.pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)

    # -------------------------------------------------------------------------

    def _do_search(self):
        term = self.search_var.get().strip()
        if not term:
            return

        self.search_btn.configure(state=tk.DISABLED)
        self.listbox.delete(0, tk.END)
        self.listbox.insert(tk.END, "Searching…")
        self.hint_var.set("")
        self._results = []
        self._selected = None
        self.add_btn.configure(state=tk.DISABLED)

        def worker():
            client = self.get_client() if self.get_client else None
            results = []
            if client:
                try:
                    import aiohttp
                    from config import REQUEST_TIMEOUT
                    from ssl_context import make_connector

                    async def _run():
                        client.reset_for_new_loop()
                        async with aiohttp.ClientSession(
                            connector=make_connector(),
                            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                        ) as session:
                            client.session = session
                            return await client.search_station_by_name(term)

                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        results = loop.run_until_complete(_run())
                    finally:
                        loop.close()
                except Exception as e:
                    print(f"[AddStation] search error: {e}")

            submit(lambda r=results: self._display_results(r))

        threading.Thread(target=worker, daemon=True).start()

    def _display_results(self, results: list[dict]):
        self.search_btn.configure(state=tk.NORMAL)
        self.listbox.delete(0, tk.END)
        self._results = results

        if not results:
            self.hint_var.set("No stations found. Try a different search term.")
            return

        self.hint_var.set(f"{len(results)} result(s) — select one, then click Add Station.")
        for r in results:
            label = f"{r['name']}  —  {r['system_name']}, {r['region_name']}"
            self.listbox.insert(tk.END, label)

    def _on_select(self, _event=None):
        sel = self.listbox.curselection()
        if sel and self._results and sel[0] < len(self._results):
            self._selected = self._results[sel[0]]
            self.add_btn.configure(state=tk.NORMAL)
        else:
            self._selected = None
            self.add_btn.configure(state=tk.DISABLED)

    # -------------------------------------------------------------------------

    def _on_add(self):
        if not self._selected:
            return

        station_id = self._selected["station_id"]
        hub_key = get_custom_hub_key(station_id)

        # Block duplicates of the 5 hardcoded hubs
        for key, cfg in TRADE_HUBS.items():
            if not is_custom_hub(key) and cfg.get("station_id") == station_id:
                messagebox.showinfo(
                    "Already Listed",
                    f"{self._selected['name']} is already in the hub list as '{cfg['name']}'.",
                    parent=self,
                )
                return

        # Block re-adding an existing custom station
        if hub_key in TRADE_HUBS:
            messagebox.showinfo(
                "Already Added",
                f"{self._selected['name']} is already in your custom stations.",
                parent=self,
            )
            return

        in_sm = self.stock_market_var.get()

        station_dict = {
            "name": self._selected["name"],
            "station_id": station_id,
            "region_id": self._selected.get("region_id"),
            "system_id": self._selected.get("system_id"),
            "corp_id": self._selected.get("corp_id"),
            "faction_id": None,
        }

        added_key = add_custom_station(station_dict, in_stock_market=in_sm)

        self.on_station_added(added_key)
        if in_sm:
            self.on_add_to_stockmarket(added_key)

        self.destroy()
