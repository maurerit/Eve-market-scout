"""Discover Player Structures dialog.

Lets the user enumerate every player structure they have active orders at
(per character slot), see system + name + ID, and copy the ID to clipboard.
Persistence and scanner wiring is intentionally out of scope here — this is
the lookup-only step so users can find structure IDs without leaving the app.
"""

import tkinter as tk
from tkinter import ttk
import threading

from tk_queue import submit
from esi_auth import ESIAuth
from esi_structures import discover_accessible_structures, StructureAccessError


class DiscoverStructuresDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.auth = ESIAuth()

        self.title("Find Player Structures")
        self.geometry("640x420")
        self.minsize(520, 320)
        self.transient(parent)

        self._create_widgets()

    def _create_widgets(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Character:").pack(side=tk.LEFT, padx=(0, 6))

        self.slot_var = tk.StringVar(value="seller")
        seller_name = self.auth.seller.character_name if self.auth.seller else "(not logged in)"
        buyer_name = self.auth.buyer.character_name if self.auth.buyer else "(not logged in)"
        ttk.Radiobutton(
            top, text=f"Seller — {seller_name}",
            variable=self.slot_var, value="seller",
            state=tk.NORMAL if self.auth.seller else tk.DISABLED,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(
            top, text=f"Buyer — {buyer_name}",
            variable=self.slot_var, value="buyer",
            state=tk.NORMAL if self.auth.buyer else tk.DISABLED,
        ).pack(side=tk.LEFT, padx=4)

        self.discover_btn = ttk.Button(top, text="Discover", command=self._on_discover)
        self.discover_btn.pack(side=tk.RIGHT)

        # Status line
        self.status_var = tk.StringVar(value="Pick a character and click Discover.")
        ttk.Label(self, textvariable=self.status_var,
                  font=("Segoe UI", 8), foreground="gray").pack(
            fill=tk.X, padx=10, pady=(0, 4)
        )

        # Results tree
        tree_frame = ttk.Frame(self, padding=(10, 0, 10, 6))
        tree_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("system_id", "name", "structure_id")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        self.tree.heading("system_id", text="System ID")
        self.tree.heading("name", text="Structure")
        self.tree.heading("structure_id", text="Structure ID")
        self.tree.column("system_id", width=90, anchor=tk.CENTER)
        self.tree.column("name", width=320, anchor=tk.W)
        self.tree.column("structure_id", width=160, anchor=tk.W)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # Bottom action bar
        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill=tk.X)
        self.copy_btn = ttk.Button(
            bottom, text="Copy ID", command=self._on_copy, state=tk.DISABLED
        )
        self.copy_btn.pack(side=tk.LEFT)
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side=tk.RIGHT)

    def _on_discover(self):
        slot = self.slot_var.get()
        self.discover_btn.configure(state=tk.DISABLED)
        self.status_var.set(f"Fetching {slot}'s active orders and resolving structures…")
        for row in self.tree.get_children():
            self.tree.delete(row)

        def worker():
            try:
                structures = discover_accessible_structures(self.auth, slot=slot)
                err = None
            except StructureAccessError as e:
                structures, err = None, e
            except Exception as e:
                structures, err = None, e
            submit(lambda s=structures, e=err: self._on_results(s, e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_results(self, structures, err):
        self.discover_btn.configure(state=tk.NORMAL)
        if err is not None:
            self.status_var.set(f"Failed: {err}")
            return
        if not structures:
            self.status_var.set(
                "No player structures found in this character's active orders."
            )
            return
        for info in structures:
            self.tree.insert(
                "", tk.END,
                values=(info.solar_system_id, info.name, info.structure_id),
            )
        self.status_var.set(f"Found {len(structures)} structure(s). Click a row, then Copy ID.")

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        self.copy_btn.configure(state=tk.NORMAL if sel else tk.DISABLED)

    def _on_copy(self):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        if len(values) < 3:
            return
        structure_id = str(values[2])
        self.clipboard_clear()
        self.clipboard_append(structure_id)
        self.update()  # ensures clipboard is committed before dialog might close
        self.status_var.set(f"Copied {structure_id} to clipboard.")
