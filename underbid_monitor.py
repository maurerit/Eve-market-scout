"""Underbid detection for tracked trades.

Monitors listed trades and detects when your sell price is no longer
the lowest at the hub. Integrates with ESI refresh cycle.
"""

from typing import Dict, List, Optional, Set
from dataclasses import dataclass

from trade_tracker import TrackedTrade
from config import TRADE_HUBS


@dataclass
class UnderbidInfo:
    """Info about an underbid on a tracked trade."""
    trade_id: str
    type_id: int
    your_price: float
    lowest_price: float
    undercut_by: float  # How much lower the competition is
    
    @property
    def undercut_percent(self) -> float:
        """Percentage you've been undercut by."""
        if self.your_price > 0:
            return ((self.your_price - self.lowest_price) / self.your_price) * 100
        return 0.0


class UnderbidMonitor:
    """Monitors tracked trades for underbids."""
    
    def __init__(self):
        # Set of trade_ids where underbid warning is ignored
        self.ignored_underbids: Set[str] = set()
        
        # Last known underbid state: {trade_id: UnderbidInfo}
        self.underbid_state: Dict[str, UnderbidInfo] = {}
    
    def ignore_underbid(self, trade_id: str):
        """Mark a trade as ignoring underbid warnings."""
        self.ignored_underbids.add(trade_id)
        # Remove from active underbid state
        if trade_id in self.underbid_state:
            del self.underbid_state[trade_id]
    
    def clear_ignore(self, trade_id: str):
        """Clear ignore flag for a trade (called on price change/relist)."""
        self.ignored_underbids.discard(trade_id)
    
    def is_ignored(self, trade_id: str) -> bool:
        """Check if underbid warning is ignored for this trade."""
        return trade_id in self.ignored_underbids
    
    def check_underbids(
        self,
        listed_trades: List[TrackedTrade],
        market_orders: List[dict],
        hub_key: str
    ) -> Dict[str, UnderbidInfo]:
        """
        Check all listed trades for underbids.
        
        Args:
            listed_trades: Trades with status='listed'
            market_orders: Current market orders for the hub region
            hub_key: Hub key (e.g., 'amarr', 'jita')
            
        Returns:
            Dict of {trade_id: UnderbidInfo} for trades that are underbid
        """
        hub_config = TRADE_HUBS.get(hub_key)
        if not hub_config:
            return {}
        
        station_id = hub_config["station_id"]
        
        # Build lookup: type_id -> lowest sell price at this station
        lowest_sells: Dict[int, float] = {}
        
        for order in market_orders:
            # Skip buy orders
            if order.get("is_buy_order", False):
                continue
            
            # Only orders at our station
            if order.get("location_id") != station_id:
                continue
            
            type_id = order.get("type_id")
            price = order.get("price", 0)
            
            if type_id and price > 0:
                if type_id not in lowest_sells or price < lowest_sells[type_id]:
                    lowest_sells[type_id] = price
        
        # Check each listed trade
        underbids: Dict[str, UnderbidInfo] = {}
        
        for trade in listed_trades:
            # Skip ignored trades
            if trade.trade_id in self.ignored_underbids:
                continue
            
            # Skip trades without a current price
            if trade.current_price <= 0:
                continue
            
            type_id = trade.type_id
            your_price = trade.current_price
            
            # Get lowest sell for this item
            lowest = lowest_sells.get(type_id)
            
            if lowest is None:
                # No other orders - you're the only seller (or order data missing)
                # Remove from underbid state if previously flagged
                if trade.trade_id in self.underbid_state:
                    del self.underbid_state[trade.trade_id]
                continue
            
            # Check if underbid (someone else is lower than you)
            # Use small epsilon for float comparison
            if lowest < (your_price - 0.001):
                info = UnderbidInfo(
                    trade_id=trade.trade_id,
                    type_id=type_id,
                    your_price=your_price,
                    lowest_price=lowest,
                    undercut_by=your_price - lowest
                )
                underbids[trade.trade_id] = info
                self.underbid_state[trade.trade_id] = info
            else:
                # You're at or below lowest - clear any previous underbid state
                if trade.trade_id in self.underbid_state:
                    del self.underbid_state[trade.trade_id]
        
        return underbids
    
    def get_underbid_info(self, trade_id: str) -> Optional[UnderbidInfo]:
        """Get underbid info for a specific trade."""
        return self.underbid_state.get(trade_id)
    
    def is_underbid(self, trade_id: str) -> bool:
        """Check if a trade is currently underbid."""
        return trade_id in self.underbid_state
    
    def clear_trade(self, trade_id: str):
        """Clear all state for a trade (called when trade is deleted/sold)."""
        self.ignored_underbids.discard(trade_id)
        if trade_id in self.underbid_state:
            del self.underbid_state[trade_id]
