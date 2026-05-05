"""Profit & Loss tracking for Stock Market holdings.

Tracks all fees and revenue for accurate P&L calculation:
- Broker fees on buy/sell order placement
- Modification fees when order prices change
- Sales tax on completed sales

Integrates with ESI refresh cycle to detect order modifications.
"""

import json
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List
from pathlib import Path

from sound_manager import get_data_dir
from calculate import (
    TradingSkills, load_cached_skills,
    calculate_broker_fee, calculate_sales_tax, calculate_relist_fee,
    get_broker_fee_rate, get_sales_tax_rate
)


PNL_FILE_PREFIX = "stock_pnl_"


@dataclass
class OrderSnapshot:
    """Snapshot of an order for detecting price changes."""
    order_id: int
    type_id: int
    price: float
    volume_remain: int
    is_buy_order: bool
    last_seen: str = ""


@dataclass 
class PnLEntry:
    """P&L tracking for a single item type."""
    type_id: int
    type_name: str
    
    # Fee totals (accumulated)
    buy_broker_fees: float = 0.0      # Fees on buy order placements
    sell_broker_fees: float = 0.0     # Fees on sell order placements
    modification_fees: float = 0.0    # Relist/modification fees
    sales_tax_paid: float = 0.0       # Tax on completed sales
    
    # Transaction totals
    total_bought_value: float = 0.0   # Total ISK spent on buys (before fees)
    total_sold_value: float = 0.0     # Total ISK received from sales (before tax)
    total_bought_qty: int = 0
    total_sold_qty: int = 0
    
    # Tracking
    processed_buy_order_ids: List[int] = field(default_factory=list)
    processed_sell_order_ids: List[int] = field(default_factory=list)
    processed_transaction_ids: List[int] = field(default_factory=list)
    
    # Timestamps
    first_activity: str = ""
    last_activity: str = ""
    
    @property
    def total_fees(self) -> float:
        """Sum of all fees paid."""
        return (self.buy_broker_fees + self.sell_broker_fees + 
                self.modification_fees + self.sales_tax_paid)
    
    @property
    def realized_pnl(self) -> float:
        """Realized profit/loss (sold value - cost basis of sold - fees on sold)."""
        if self.total_sold_qty == 0:
            return 0.0
        # Approximate cost basis using average
        if self.total_bought_qty > 0:
            avg_cost = self.total_bought_value / self.total_bought_qty
            cost_of_sold = avg_cost * self.total_sold_qty
        else:
            cost_of_sold = 0.0
        return self.total_sold_value - cost_of_sold - self.sales_tax_paid
    
    @property
    def total_invested(self) -> float:
        """Total ISK invested including buy fees."""
        return self.total_bought_value + self.buy_broker_fees


class PnLManager:
    """Manages P&L tracking for a hub with order modification detection."""
    
    def __init__(self, hub_key: str):
        self.hub_key = hub_key
        self.filepath = get_data_dir() / f"{PNL_FILE_PREFIX}{hub_key}.json"
        
        # P&L entries by type_id
        self.entries: Dict[int, PnLEntry] = {}
        
        # Cached order state for modification detection
        self.order_cache: Dict[int, OrderSnapshot] = {}  # order_id -> snapshot
        
        self._load()
    
    def _load(self):
        """Load P&L data from disk."""
        if not self.filepath.exists():
            return
        
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Load entries
            for type_id_str, entry_data in data.get("entries", {}).items():
                type_id = int(type_id_str)
                # Handle missing fields
                for list_field in ["processed_buy_order_ids", "processed_sell_order_ids", 
                                   "processed_transaction_ids"]:
                    if list_field not in entry_data:
                        entry_data[list_field] = []
                self.entries[type_id] = PnLEntry(**entry_data)
            
            # Load order cache
            for order_id_str, snap_data in data.get("order_cache", {}).items():
                order_id = int(order_id_str)
                self.order_cache[order_id] = OrderSnapshot(**snap_data)
                
            print(f"[PnL] Loaded {len(self.entries)} entries, {len(self.order_cache)} cached orders for {self.hub_key}")
        except Exception as e:
            print(f"[PnL] Error loading {self.hub_key}: {e}")
    
    def _save(self):
        """Save P&L data to disk."""
        try:
            data = {
                "entries": {
                    str(tid): asdict(entry) 
                    for tid, entry in self.entries.items()
                },
                "order_cache": {
                    str(oid): asdict(snap)
                    for oid, snap in self.order_cache.items()
                },
                "hub_key": self.hub_key,
                "last_saved": datetime.now().isoformat(),
            }
            
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[PnL] Error saving {self.hub_key}: {e}")
    
    def _get_or_create_entry(self, type_id: int, type_name: str) -> PnLEntry:
        """Get existing entry or create new one."""
        if type_id not in self.entries:
            now = datetime.now().isoformat()
            self.entries[type_id] = PnLEntry(
                type_id=type_id,
                type_name=type_name,
                first_activity=now,
                last_activity=now,
            )
        return self.entries[type_id]
    
    def _get_skills(self) -> TradingSkills:
        """Load cached skills for this hub."""
        return load_cached_skills(self.hub_key, slot="seller")
    
    # === Order Placement Recording ===
    
    def record_buy_order(self, order_id: int, type_id: int, type_name: str,
                         price: float, quantity: int) -> float:
        """Record broker fee for placing a buy order.
        
        Returns: Fee charged (0 if already processed)
        """
        entry = self._get_or_create_entry(type_id, type_name)
        
        if order_id in entry.processed_buy_order_ids:
            return 0.0
        
        skills = self._get_skills()
        fee = calculate_broker_fee(price, quantity, skills)
        
        entry.buy_broker_fees += fee
        entry.processed_buy_order_ids.append(order_id)
        entry.last_activity = datetime.now().isoformat()
        
        # Cache the order for modification tracking
        self.order_cache[order_id] = OrderSnapshot(
            order_id=order_id,
            type_id=type_id,
            price=price,
            volume_remain=quantity,
            is_buy_order=True,
            last_seen=datetime.now().isoformat(),
        )
        
        self._save()
        print(f"[PnL] Buy order {order_id}: {type_name} - broker fee {fee:,.0f} ISK")
        return fee
    
    def record_sell_order(self, order_id: int, type_id: int, type_name: str,
                          price: float, quantity: int) -> float:
        """Record broker fee for placing a sell order.
        
        Returns: Fee charged (0 if already processed)
        """
        entry = self._get_or_create_entry(type_id, type_name)
        
        if order_id in entry.processed_sell_order_ids:
            return 0.0
        
        skills = self._get_skills()
        fee = calculate_broker_fee(price, quantity, skills)
        
        entry.sell_broker_fees += fee
        entry.processed_sell_order_ids.append(order_id)
        entry.last_activity = datetime.now().isoformat()
        
        # Cache the order for modification tracking
        self.order_cache[order_id] = OrderSnapshot(
            order_id=order_id,
            type_id=type_id,
            price=price,
            volume_remain=quantity,
            is_buy_order=False,
            last_seen=datetime.now().isoformat(),
        )
        
        self._save()
        print(f"[PnL] Sell order {order_id}: {type_name} - broker fee {fee:,.0f} ISK")
        return fee
    
    # === Order Modification Detection ===
    
    def check_order_modifications(self, current_orders: List[dict], 
                                  type_id_filter: Optional[set] = None) -> Dict[int, float]:
        """Compare current orders to cache, detect price changes, calculate fees.
        
        Args:
            current_orders: List of order dicts from ESI
            type_id_filter: Optional set of type_ids to track (holdings)
            
        Returns:
            Dict of {order_id: modification_fee} for orders that changed
        """
        skills = self._get_skills()
        modification_fees: Dict[int, float] = {}
        now = datetime.now().isoformat()
        
        current_order_ids = set()
        
        for order in current_orders:
            order_id = order.get("order_id")
            if not order_id:
                continue
            
            current_order_ids.add(order_id)
            type_id = order.get("type_id")
            
            # Skip if not in our tracked holdings
            if type_id_filter and type_id not in type_id_filter:
                continue
            
            price = order.get("price", 0)
            volume = order.get("volume_remain", 0)
            is_buy = order.get("is_buy_order", False)
            
            # Check if we have this order cached
            if order_id in self.order_cache:
                cached = self.order_cache[order_id]
                
                # Detect price change
                if abs(cached.price - price) > 0.001:
                    # Calculate modification fee
                    fee = calculate_relist_fee(
                        cached.price, price, cached.volume_remain, skills
                    )
                    
                    if fee > 0:
                        modification_fees[order_id] = fee
                        
                        # Record to entry
                        if type_id in self.entries:
                            entry = self.entries[type_id]
                            entry.modification_fees += fee
                            entry.last_activity = now
                            print(f"[PnL] Order {order_id} modified: {cached.price:,.2f} -> {price:,.2f}, fee {fee:,.0f} ISK")
                
                # Update cache
                cached.price = price
                cached.volume_remain = volume
                cached.last_seen = now
            else:
                # New order we haven't seen - cache it
                # (Initial placement should be recorded via record_buy/sell_order)
                self.order_cache[order_id] = OrderSnapshot(
                    order_id=order_id,
                    type_id=type_id,
                    price=price,
                    volume_remain=volume,
                    is_buy_order=is_buy,
                    last_seen=now,
                )
        
        # Clean up expired orders from cache
        expired = [oid for oid in self.order_cache if oid not in current_order_ids]
        for oid in expired:
            del self.order_cache[oid]
        
        if modification_fees:
            self._save()
        
        return modification_fees
    
    # === Transaction Recording ===
    
    def record_buy_fill(self, transaction_id: int, type_id: int, type_name: str,
                        quantity: int, price: float):
        """Record a buy transaction (order filled or instant buy)."""
        entry = self._get_or_create_entry(type_id, type_name)
        
        if transaction_id in entry.processed_transaction_ids:
            return
        
        entry.total_bought_value += quantity * price
        entry.total_bought_qty += quantity
        entry.processed_transaction_ids.append(transaction_id)
        entry.last_activity = datetime.now().isoformat()
        
        self._save()
    
    def record_sale(self, transaction_id: int, type_id: int, type_name: str,
                    quantity: int, price: float) -> float:
        """Record a sale transaction and calculate sales tax.
        
        Returns: Sales tax charged
        """
        entry = self._get_or_create_entry(type_id, type_name)
        
        if transaction_id in entry.processed_transaction_ids:
            return 0.0
        
        skills = self._get_skills()
        tax = calculate_sales_tax(price, quantity, skills)
        
        entry.total_sold_value += quantity * price
        entry.total_sold_qty += quantity
        entry.sales_tax_paid += tax
        entry.processed_transaction_ids.append(transaction_id)
        entry.last_activity = datetime.now().isoformat()
        
        self._save()
        print(f"[PnL] Sale: {quantity}x {type_name} @ {price:,.2f} - tax {tax:,.0f} ISK")
        return tax
    
    # === Summary Methods ===
    
    def get_entry(self, type_id: int) -> Optional[PnLEntry]:
        """Get P&L entry for a specific item."""
        return self.entries.get(type_id)
    
    def get_all_entries(self) -> List[PnLEntry]:
        """Get all P&L entries."""
        return list(self.entries.values())
    
    def get_summary(self) -> dict:
        """Get aggregate P&L summary for the hub.
        
        Returns:
            Dict with totals: invested, sold, fees breakdown, net_pnl
        """
        total_invested = 0.0
        total_sold = 0.0
        total_buy_fees = 0.0
        total_sell_fees = 0.0
        total_mod_fees = 0.0
        total_tax = 0.0
        
        for entry in self.entries.values():
            total_invested += entry.total_bought_value
            total_sold += entry.total_sold_value
            total_buy_fees += entry.buy_broker_fees
            total_sell_fees += entry.sell_broker_fees
            total_mod_fees += entry.modification_fees
            total_tax += entry.sales_tax_paid
        
        total_fees = total_buy_fees + total_sell_fees + total_mod_fees + total_tax
        
        # Net P&L = revenue - cost of goods sold - all fees
        # Simplified: sold value - (invested * sold_ratio) - fees
        # For now, use total_invested as proxy for COGS
        net_pnl = total_sold - total_invested - total_fees
        
        return {
            "total_invested": total_invested,
            "total_sold": total_sold,
            "buy_broker_fees": total_buy_fees,
            "sell_broker_fees": total_sell_fees,
            "modification_fees": total_mod_fees,
            "sales_tax": total_tax,
            "total_fees": total_fees,
            "net_pnl": net_pnl,
            "item_count": len(self.entries),
        }
    
    def clear_entry(self, type_id: int) -> bool:
        """Remove P&L entry for an item."""
        if type_id in self.entries:
            del self.entries[type_id]
            self._save()
            return True
        return False
    
    def clear_all(self):
        """Clear all P&L data for this hub."""
        self.entries.clear()
        self.order_cache.clear()
        self._save()
        print(f"[PnL] Cleared all data for {self.hub_key}")
