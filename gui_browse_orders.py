"""Raw market viewer for a single player structure (dev/debug tool).

Bypasses every scanner filter — fetches `/markets/structures/{id}/` and dumps
the entire order book in a treeview so the user can confirm whether there's
actually anything trading there. Authoritative for "is the fetch working?"
and "does this structure have a market?" questions.
"""

import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime

from tk_queue import submit
from esi_auth import ESIAuth
from esi_structures import fetch_structure_orders, StructureAccessError


class BrowseStructureOrdersDialog(tk.Toplevel):
    def __init__(self, parent, structure_id: int, structure_name: str,
                 slot: str = "seller"):
        super().__init__(parent)
        self.structure_id = structure_id
        self.structure_name = structure_name
        self.slot = slot
        self.auth = ESIAuth()

        self.title(f"Browse Orders — {structure_name}")
        self.geometry("900x560")
        self.minsize(720, 400)
        self.transient(parent)

        self._build()
        self._kick_fetch()

    def _build(self):
        header = ttk.Frame(self, padding=10)
        header.pack(fill=tk.X)
        ttk.Label(header, text=self.structure_name,
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text=f"  (id {self.structure_id}, slot: {self.slot})",
                  foreground="gray").pack(side=tk.LEFT)
        ttk.Button(header, text="Refresh", command=self._kick_fetch).pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value="Fetching orders…")
        ttk.Label(self, textvariable=self.status_var,
                  font=("Segoe UI", 8), foreground="gray").pack(
            fill=tk.X, padx=10, pady=(0, 4)
        )

        tree_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        tree_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("side", "name", "type_id", "price", "qty", "issued")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        self.tree.heading("side", text="Side")
        self.tree.heading("name", text="Item")
        self.tree.heading("type_id", text="Type ID")
        self.tree.heading("price", text="Price")
        self.tree.heading("qty", text="Qty")
        self.tree.heading("issued", text="Issued (UTC)")
        self.tree.column("side", width=50, anchor=tk.CENTER)
        self.tree.column("name", width=300, anchor=tk.W)
        self.tree.column("type_id", width=80, anchor=tk.E)
        self.tree.column("price", width=130, anchor=tk.E)
        self.tree.column("qty", width=90, anchor=tk.E)
        self.tree.column("issued", width=140, anchor=tk.W)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.tag_configure("sell", foreground="#aa4444")
        self.tree.tag_configure("buy", foreground="#3a7a3a")

    def _kick_fetch(self):
        self.status_var.set("Fetching orders…")
        for row in self.tree.get_children():
            self.tree.delete(row)

        def worker():
            try:
                orders, _expires = fetch_structure_orders(
                    self.structure_id, self.auth, slot=self.slot
                )
                err = None
            except StructureAccessError as e:
                orders, err = None, e
            except Exception as e:
                orders, err = None, e
            submit(lambda o=orders, e=err: self._on_result(o, e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_result(self, orders, err):
        if err is not None:
            self.status_var.set(f"Fetch failed: {err}")
            return
        if not orders:
            self.status_var.set(
                "Fetch succeeded but returned zero orders — structure market is empty."
            )
            return

        # Resolve names in one shot via SDE (sync, fast).
        from sde_manager import get_sde_manager
        sde = get_sde_manager()
        type_ids = sorted({o["type_id"] for o in orders})
        names = sde.get_type_names_bulk(type_ids) if sde else {}

        # Sort: sell-side first by ascending price, then buy-side by descending
        # price. Mirrors how a player reads a market window.
        sells = sorted(
            (o for o in orders if not o.get("is_buy_order")),
            key=lambda o: (o.get("type_id", 0), o.get("price", 0.0)),
        )
        buys = sorted(
            (o for o in orders if o.get("is_buy_order")),
            key=lambda o: (o.get("type_id", 0), -o.get("price", 0.0)),
        )

        for o in sells + buys:
            tid = o["type_id"]
            side = "Buy" if o.get("is_buy_order") else "Sell"
            tag = "buy" if o.get("is_buy_order") else "sell"
            self.tree.insert(
                "", tk.END,
                values=(
                    side,
                    names.get(tid, f"(type {tid})"),
                    tid,
                    f"{o.get('price', 0.0):,.2f}",
                    f"{o.get('volume_remain', 0):,}",
                    self._fmt_issued(o.get("issued")),
                ),
                tags=(tag,),
            )

        n_sell, n_buy = len(sells), len(buys)
        n_types = len(type_ids)
        self.status_var.set(
            f"{len(orders)} orders — {n_sell} sell, {n_buy} buy, "
            f"across {n_types} item types."
        )

    @staticmethod
    def _fmt_issued(raw):
        if not raw:
            return ""
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d %H:%M"
            )
        except Exception:
            return str(raw)[:16]
