"""Persistent watchlist tab for EVE Market Scout.

Features:
- Add items by name (ESI search or local cache)
- Bulk add from pasted fitting/market format
- Set custom alert conditions per item (price under X, margin over Y, etc.)
- Persist watchlist to JSON
- Right-click to edit/remove items
- Green tab + row highlighting when alerts trigger
- Multi-select with copy/paste to NPC Orders tab
"""

import tkinter as tk
from tkinter import ttk
import os
import json
from typing import Callable, Optional
from dataclasses import asdict

from gui_watchlist_dialogs import (
    WatchlistItem,
    AddItemDialog,
    BulkAddDialog,
    EditItemDialog
)
from sound_manager import get_data_dir


# Watchlist persistence file - use centralized data directory
WATCHLIST_FILE = str(get_data_dir() / "watchlist.json")

# Magic header for TSV clipboard format (round-trip copy/paste between tabs)
WATCHLIST_TSV_HEADER = "EVE_MARKET_SCOUT_WATCHLIST_V1"


# Columns that should sort numerically
WATCHLIST_NUMERIC_COLUMNS = {"price_under", "price_over", "margin_over", "current_price", "qty"}


class WatchlistTabManager:
    """Manages the persistent watchlist tab."""

    def __init__(self, notebook: ttk.Notebook, get_client: Callable = None, set_status: Callable = None):
        self.notebook = notebook
        self.get_client = get_client  # Function to get ESIClient for searching
        self.set_status = set_status or (lambda x: None)
        self.get_skills = None  # Function to get TradingSkills for fee calculations
        self.region_id = None  # Current hub's region_id for max buy calculator
        
        # Watchlist data
        self.watchlist: dict[int, WatchlistItem] = {}  # type_id -> WatchlistItem
        self._load_watchlist()
        
        # Track alert state for tab coloring
        self.has_alerts = False
        self.tab_index = None  # Will be set after tab is created
        
        # Clipboard for copy/paste (shared via setter)
        self._clipboard_getter = None
        self._clipboard_setter = None
        
        # Sort state tracking
        self.sort_state: dict[str, bool] = {}  # column -> reverse
        
        # Stock market tab reference (set by gui_main after creation)
        self.stock_market_tab = None
        
        self._create_tab()

    def set_clipboard_functions(self, getter: Callable, setter: Callable):
        """Set clipboard getter/setter for cross-tab copy/paste."""
        self._clipboard_getter = getter
        self._clipboard_setter = setter

    def set_skills_getter(self, getter: Callable):
        """Set function to retrieve current TradingSkills for fee calculations."""
        self.get_skills = getter

    def set_region_id(self, region_id: int):
        """Update region_id when hub changes (for max buy calculator)."""
        self.region_id = region_id

    def _load_watchlist(self):
        """Load watchlist from JSON file."""
        try:
            if os.path.exists(WATCHLIST_FILE):
                with open(WATCHLIST_FILE, "r") as f:
                    data = json.load(f)
                    for item_data in data.get("items", []):
                        item = WatchlistItem(**item_data)
                        # Clear market data - should only come from fresh scans
                        old_price = item.current_price
                        item.current_price = None
                        item.current_qty = None
                        item.current_margin = None
                        print(f"[Watchlist] Loaded '{item.name}': cleared price {old_price} -> None")
                        self.watchlist[item.type_id] = item
        except Exception as e:
            print(f"Error loading watchlist: {e}")

    def _save_watchlist(self):
        """Save watchlist to JSON file."""
        try:
            items = [asdict(item) for item in self.watchlist.values()]
            with open(WATCHLIST_FILE, "w") as f:
                json.dump({"items": items}, f, indent=2)
        except Exception as e:
            print(f"Error saving watchlist: {e}")

    def _create_tab(self):
        """Create the watchlist tab."""
        self.frame = ttk.Frame(self.notebook)
        self.notebook.add(self.frame, text="Watchlist")
        
        # Store tab index for later color updates
        self.tab_index = self.notebook.index(self.frame)

        # Button bar
        btn_frame = ttk.Frame(self.frame, padding=5)
        btn_frame.pack(fill=tk.X)

        ttk.Button(btn_frame, text="+ Add Item", command=self._show_add_dialog).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="+ Bulk Add", command=self._show_bulk_add_dialog).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Edit", command=self._edit_selected).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Remove", command=self._remove_selected).pack(side=tk.LEFT, padx=5)
        
        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=2)
        
        ttk.Button(btn_frame, text="Copy", command=self._copy_selected).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Paste", command=self._paste_items).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(btn_frame, text="Ctrl+C/V or right-click", foreground="gray").pack(side=tk.RIGHT, padx=10)

        # Treeview with EXTENDED selection for multi-select
        columns = ("name", "price_under", "price_over", "margin_over", "current_price", "qty", "status", "notes")
        self.tree = ttk.Treeview(
            self.frame,
            columns=columns,
            show="headings",
            style="Deals.Treeview",
            selectmode="extended"  # Allow multi-select
        )

        # Column headings with sort commands
        self.tree.heading("name", text="Item Name", command=lambda: self._sort_tree("name"))
        self.tree.heading("price_under", text="Price Under", command=lambda: self._sort_tree("price_under"))
        self.tree.heading("price_over", text="Price Over", command=lambda: self._sort_tree("price_over"))
        self.tree.heading("margin_over", text="Margin Over %", command=lambda: self._sort_tree("margin_over"))
        self.tree.heading("current_price", text="Current Price", command=lambda: self._sort_tree("current_price"))
        self.tree.heading("qty", text="Qty", command=lambda: self._sort_tree("qty"))
        self.tree.heading("status", text="Status", command=lambda: self._sort_tree("status"))
        self.tree.heading("notes", text="Notes", command=lambda: self._sort_tree("notes"))

        # Column widths
        self.tree.column("name", width=200, minwidth=150)
        self.tree.column("price_under", width=100, anchor=tk.E)
        self.tree.column("price_over", width=100, anchor=tk.E)
        self.tree.column("margin_over", width=100, anchor=tk.E)
        self.tree.column("current_price", width=100, anchor=tk.E)
        self.tree.column("qty", width=70, anchor=tk.E)
        self.tree.column("status", width=100, anchor=tk.CENTER)
        self.tree.column("notes", width=200)

        # Scrollbars
        vsb = ttk.Scrollbar(self.frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(self.frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Tags for status colors - GREEN for alerts
        self.tree.tag_configure("alert", foreground="white", background="#228B22")  # Forest green
        self.tree.tag_configure("normal", foreground="black")
        self.tree.tag_configure("no_data", foreground="gray")

        # Context menu
        self.context_menu = tk.Menu(self.frame, tearoff=0)
        self.context_menu.add_command(label="Edit Item", command=self._edit_selected)
        self.context_menu.add_command(label="Remove Item", command=self._remove_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy (Ctrl+C)", command=self._copy_selected)
        self.context_menu.add_command(label="Paste (Ctrl+V)", command=self._paste_items)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Show Price History", command=self._show_price_history)
        self.context_menu.add_command(label="Add to Stock Market", command=self._add_to_stock_market)

        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Double-1>", lambda e: self._edit_selected())
        
        # Keyboard shortcuts
        self.tree.bind("<Control-c>", lambda e: self._copy_selected())
        self.tree.bind("<Control-v>", lambda e: self._paste_items())

        # Initial display
        self._refresh_display()

    def _sort_tree(self, col: str):
        """Sort treeview by column when header is clicked."""
        # Toggle sort direction
        reverse = self.sort_state.get(col, False)
        self.sort_state[col] = not reverse

        # Get all items with their values
        items = [(self.tree.set(item, col), item) for item in self.tree.get_children("")]

        if col in WATCHLIST_NUMERIC_COLUMNS:
            def parse_num(val):
                if val == "-" or val == "No data":
                    return float("-inf") if reverse else float("inf")
                val = val.replace(",", "").replace("%", "")
                try:
                    return float(val)
                except ValueError:
                    return float("-inf") if reverse else float("inf")
            items.sort(key=lambda x: parse_num(x[0]), reverse=reverse)
        else:
            items.sort(key=lambda x: x[0].lower(), reverse=reverse)

        # Rearrange items
        for idx, (_, item) in enumerate(items):
            self.tree.move(item, "", idx)

        # Update header to show sort direction
        for c in self.tree["columns"]:
            text = self._get_column_title(c)
            self.tree.heading(c, text=text)
        
        arrow = " v" if reverse else " ^"
        self.tree.heading(col, text=self._get_column_title(col) + arrow)

    def _get_column_title(self, col: str) -> str:
        """Get display title for a column."""
        titles = {
            "name": "Item Name",
            "price_under": "Price Under",
            "price_over": "Price Over",
            "margin_over": "Margin Over %",
            "current_price": "Current Price",
            "qty": "Qty",
            "status": "Status",
            "notes": "Notes"
        }
        return titles.get(col, col)

    def _show_context_menu(self, event):
        """Show right-click context menu."""
        item = self.tree.identify_row(event.y)
        if item:
            # Add to selection if not already selected
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def _copy_selected(self):
        """
        Unified copy to OS clipboard.
        - 1 item: just the name (paste-friendly for EVE).
        - Multiple items: TSV with magic header for round-trip into the app.
        """
        selection = self.tree.selection()
        if not selection:
            self.set_status("No items selected to copy")
            return

        items = []
        for iid in selection:
            try:
                type_id = int(iid)
            except ValueError:
                continue
            if type_id in self.watchlist:
                items.append(self.watchlist[type_id])

        if not items:
            self.set_status("No items selected to copy")
            return

        if len(items) == 1:
            text = items[0].name
            status_msg = f"Copied name: {items[0].name}"
        else:
            lines = [WATCHLIST_TSV_HEADER]
            lines.append("type_id\tname\tprice_under\tprice_over\tmargin_over\tnotes")
            for it in items:
                lines.append("\t".join([
                    str(it.type_id),
                    it.name,
                    str(it.price_under) if it.price_under else "",
                    str(it.price_over) if it.price_over else "",
                    str(it.margin_over) if it.margin_over else "",
                    (it.notes or "").replace("\t", " ").replace("\n", " "),
                ]))
            text = "\n".join(lines)
            status_msg = f"Copied {len(items)} item(s) to clipboard"

        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(text)
            self.frame.update()  # Force flush so clipboard persists when focus changes
            self.set_status(status_msg)
        except tk.TclError as e:
            self.set_status(f"Clipboard error: {e}")

    def _paste_items(self):
        """
        Unified paste from OS clipboard.
        - If our TSV header is detected: restore items with full data.
        - Otherwise: treat clipboard as a list of names and open BulkAddDialog
          pre-filled, which auto-resolves type_ids via ESI.
        """
        try:
            text = self.frame.clipboard_get()
        except tk.TclError:
            self.set_status("Clipboard is empty or not text")
            return

        if not text or not text.strip():
            self.set_status("Clipboard is empty")
            return

        lines = text.split("\n")
        if lines and lines[0].strip() == WATCHLIST_TSV_HEADER:
            self._paste_from_tsv(lines)
        else:
            # Treat as plain list of names - delegate to BulkAddDialog
            BulkAddDialog(self.frame, self._on_bulk_add, self.get_client, prefill_text=text)

    def _paste_from_tsv(self, lines):
        """Parse our internal TSV format and restore items with full data."""
        if len(lines) < 3:
            self.set_status("Clipboard format invalid")
            return

        added = 0
        skipped = 0
        # lines[0] = magic header, lines[1] = column header, lines[2:] = data
        for line in lines[2:]:
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            try:
                type_id = int(parts[0])
            except ValueError:
                continue

            if type_id in self.watchlist:
                skipped += 1
                continue

            name = parts[1]
            price_under = self._parse_optional_float(parts[2] if len(parts) > 2 else "")
            price_over = self._parse_optional_float(parts[3] if len(parts) > 3 else "")
            margin_over = self._parse_optional_float(parts[4] if len(parts) > 4 else "")
            notes = parts[5] if len(parts) > 5 else ""

            self.watchlist[type_id] = WatchlistItem(
                type_id=type_id,
                name=name,
                price_under=price_under,
                price_over=price_over,
                margin_over=margin_over,
                notes=notes,
            )
            added += 1

        if added > 0:
            self._save_watchlist()
            self._refresh_display()
            self.set_status(f"Pasted {added} item(s)" + (f" ({skipped} already in list)" if skipped else ""))
        else:
            self.set_status("All items already in watchlist")

    @staticmethod
    def _parse_optional_float(s):
        """Parse a possibly-empty string into float or None."""
        s = (s or "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    def _show_price_history(self):
        """Show price history graph for the selected watchlist item."""
        type_id = self._get_selected_type_id()
        if type_id is None or type_id not in self.watchlist:
            return

        item = self.watchlist[type_id]

        region_id = self.region_id
        if not region_id:
            from config import get_hub_config, DEFAULT_HUB
            region_id = get_hub_config(DEFAULT_HUB)["region_id"]

        from graphing import show_price_graph
        show_price_graph(
            parent=self.frame,
            type_id=type_id,
            type_name=item.name,
            region_id=region_id,
            profiles=None,
        )

    def _add_to_stock_market(self):
        """Add selected item to stock market portfolio."""
        selection = self.tree.selection()
        if not selection:
            return
        
        # Get the first selected item
        item_id = selection[0]
        try:
            type_id = int(item_id)
        except ValueError:
            return
        
        watchlist_item = self.watchlist.get(type_id)
        if not watchlist_item:
            return
        
        if self.stock_market_tab:
            from config import get_hub_config, DEFAULT_HUB
            hub_config = get_hub_config(DEFAULT_HUB)
            
            self.stock_market_tab.add_item_from_external(
                type_id=watchlist_item.type_id,
                region_id=self.region_id or hub_config["region_id"],
                station_id=hub_config["station_id"],
                type_name=watchlist_item.name
            )

    def _update_tab_color(self):
        """Update tab appearance based on alert status."""
        alert_items = self.get_alert_items()
        alert_count = len(alert_items)
        
        if alert_count > 0:
            self.has_alerts = True
            # Update tab text with alert count and use green styling
            self.notebook.tab(self.tab_index, text=f"Watchlist ({alert_count}!)")
        else:
            self.has_alerts = False
            item_count = len(self.watchlist)
            if item_count > 0:
                self.notebook.tab(self.tab_index, text=f"Watchlist ({item_count})")
            else:
                self.notebook.tab(self.tab_index, text="Watchlist")

    def _refresh_display(self):
        """Refresh the treeview with current watchlist data."""
        # Clear tree
        for item in self.tree.get_children():
            self.tree.delete(item)

        # Populate
        for wl_item in self.watchlist.values():
            self._insert_item(wl_item)
        
        # Update tab color based on alerts
        self._update_tab_color()

    def _insert_item(self, item: WatchlistItem):
        """Insert a watchlist item into the tree."""
        # Format values
        price_under = f"{item.price_under:,.0f}" if item.price_under else "-"
        price_over = f"{item.price_over:,.0f}" if item.price_over else "-"
        margin_over = f"{item.margin_over:.1f}%" if item.margin_over else "-"
        
        # Distinguish "no listings" (qty=0) from "never scanned" (qty=None)
        if item.current_price:
            current = f"{item.current_price:,.0f}"
            qty = f"{item.current_qty:,}" if item.current_qty else "-"
        elif item.current_qty == 0:
            current = "No listings"
            qty = "0"
        else:
            current = "No data"
            qty = "-"
        
        # Determine status and tag
        status = "Watching"
        tag = "normal"
        
        if item.current_price:
            alerts = []
            if item.price_under and item.current_price <= item.price_under:
                alerts.append("UNDER")
            if item.price_over and item.current_price >= item.price_over:
                alerts.append("OVER")
            if item.margin_over and item.current_margin and item.current_margin >= item.margin_over:
                alerts.append("MARGIN")
            
            if alerts:
                status = " | ".join(alerts)
                tag = "alert"  # Green row
        else:
            tag = "no_data"

        self.tree.insert("", tk.END, iid=str(item.type_id), values=(
            item.name,
            price_under,
            price_over,
            margin_over,
            current,
            qty,
            status,
            item.notes[:30] + "..." if len(item.notes) > 30 else item.notes
        ), tags=(tag,))

    def _get_selected_type_id(self) -> Optional[int]:
        """Get type_id of selected item."""
        selection = self.tree.selection()
        if selection:
            return int(selection[0])
        return None

    def _show_add_dialog(self):
        """Show dialog to add new item to watchlist."""
        AddItemDialog(self.frame, self._on_add_item, self.get_client, get_skills=self.get_skills, region_id=self.region_id)

    def _show_bulk_add_dialog(self):
        """Show dialog to bulk add items from pasted text."""
        BulkAddDialog(self.frame, self._on_bulk_add, self.get_client)

    def _on_add_item(self, type_id: int, name: str, conditions: dict):
        """Callback when item is added from dialog."""
        if type_id in self.watchlist:
            # Update existing
            item = self.watchlist[type_id]
            item.price_under = conditions.get("price_under")
            item.price_over = conditions.get("price_over")
            item.margin_over = conditions.get("margin_over")
            item.notes = conditions.get("notes", "")
        else:
            # Create new
            self.watchlist[type_id] = WatchlistItem(
                type_id=type_id,
                name=name,
                price_under=conditions.get("price_under"),
                price_over=conditions.get("price_over"),
                margin_over=conditions.get("margin_over"),
                notes=conditions.get("notes", "")
            )
        
        self._save_watchlist()
        self._refresh_display()
        self.set_status(f"Added to watchlist: {name}")

    def _on_bulk_add(self, items: list[dict]):
        """Callback when items are bulk added."""
        added = 0
        for item_data in items:
            type_id = item_data["type_id"]
            name = item_data["name"]
            
            if type_id not in self.watchlist:
                self.watchlist[type_id] = WatchlistItem(
                    type_id=type_id,
                    name=name
                )
                added += 1
        
        self._save_watchlist()
        self._refresh_display()
        self.set_status(f"Added {added} items to watchlist")

    def _edit_selected(self):
        """Edit the selected watchlist item."""
        type_id = self._get_selected_type_id()
        if type_id and type_id in self.watchlist:
            item = self.watchlist[type_id]
            EditItemDialog(self.frame, item, self._on_edit_item)

    def _on_edit_item(self, type_id: int, conditions: dict):
        """Callback when item is edited."""
        if type_id in self.watchlist:
            item = self.watchlist[type_id]
            item.price_under = conditions.get("price_under")
            item.price_over = conditions.get("price_over")
            item.margin_over = conditions.get("margin_over")
            item.notes = conditions.get("notes", "")
            
            self._save_watchlist()
            self._refresh_display()

    def _remove_selected(self):
        """Remove selected items from watchlist."""
        selection = self.tree.selection()
        if not selection:
            return
        
        removed = 0
        for iid in selection:
            type_id = int(iid)
            if type_id in self.watchlist:
                del self.watchlist[type_id]
                removed += 1
        
        if removed > 0:
            self._save_watchlist()
            self._refresh_display()
            self.set_status(f"Removed {removed} item(s) from watchlist")

    def add_from_deal(self, type_id: int, name: str, current_price: float = None):
        """Add item to watchlist from deals context menu (pre-populated)."""
        # If already in watchlist, just edit it
        if type_id in self.watchlist:
            item = self.watchlist[type_id]
            EditItemDialog(self.frame, item, self._on_edit_item)
        else:
            # Create with pre-filled data, open edit dialog
            temp_item = WatchlistItem(
                type_id=type_id,
                name=name,
                current_price=current_price
            )
            AddItemDialog(self.frame, self._on_add_item, self.get_client, prefill=temp_item, get_skills=self.get_skills, region_id=self.region_id)

    def update_from_local_orders(self, orders: list[dict]):
        """
        Update watchlist prices from local hub market orders.
        Called after each scan with the raw order data.
        """
        if not self.watchlist:
            return
        
        # Build lookup: type_id -> (lowest sell price, quantity at that price)
        sell_data = {}  # type_id -> {"price": float, "qty": int}
        for order in orders:
            if order.get("is_buy_order"):
                continue  # Skip buy orders
            
            type_id = order["type_id"]
            price = order["price"]
            volume = order.get("volume_remain", 0)
            
            if type_id not in sell_data or price < sell_data[type_id]["price"]:
                # New lowest price - reset quantity
                sell_data[type_id] = {"price": price, "qty": volume}
            elif price == sell_data[type_id]["price"]:
                # Same price - accumulate quantity
                sell_data[type_id]["qty"] += volume
        
        # Update watchlist items
        updated = 0
        for type_id, item in self.watchlist.items():
            if type_id in sell_data:
                item.current_price = sell_data[type_id]["price"]
                item.current_qty = sell_data[type_id]["qty"]
                updated += 1
            else:
                # No sell orders for this item - clear stale data
                # Use qty=0 to distinguish "no listings" from "never scanned"
                item.current_price = None
                item.current_qty = 0
        
        # Refresh display to show new prices and trigger alerts
        self._refresh_display()
        
        # Check for alerts
        alerts = self.get_alert_items()
        if alerts:
            self.set_status(f"Watchlist: {len(alerts)} alert(s)!")

    def get_alert_items(self) -> list[WatchlistItem]:
        """Get items that have triggered alerts."""
        alerts = []
        for item in self.watchlist.values():
            if item.current_price:
                if item.price_under and item.current_price <= item.price_under:
                    alerts.append(item)
                elif item.price_over and item.current_price >= item.price_over:
                    alerts.append(item)
                elif item.margin_over and item.current_margin and item.current_margin >= item.margin_over:
                    alerts.append(item)
        return alerts
