"""ESI Wallet and Order data fetching for EVE Market Scout."""

import requests
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from esi_auth import ESIAuth

BASE_URL = "https://esi.evetech.net/latest"


@dataclass
class Transaction:
    """A wallet transaction (buy or sell)."""
    transaction_id: int
    date: datetime
    type_id: int
    type_name: str  # Populated later
    quantity: int
    unit_price: float
    is_buy: bool
    location_id: int
    journal_ref_id: int  # Links to journal for fees
    
    @property
    def total(self) -> float:
        return self.quantity * self.unit_price


@dataclass
class JournalEntry:
    """A wallet journal entry (fees, taxes, etc)."""
    entry_id: int
    date: datetime
    ref_type: str  # 'brokers_fee', 'transaction_tax', 'market_escrow', etc.
    amount: float  # Negative = cost, Positive = income
    balance: float
    description: str
    context_id: Optional[int] = None  # Links to order_id or transaction_id
    context_type: Optional[str] = None


@dataclass 
class MarketOrder:
    """An active or historical market order."""
    order_id: int
    type_id: int
    type_name: str  # Populated later
    is_buy_order: bool
    price: float
    volume_total: int
    volume_remain: int
    issued: datetime
    duration: int  # Days
    location_id: int
    state: str = "active"  # active, cancelled, expired, fulfilled
    escrow: float = 0  # ISK in escrow for buy orders
    
    @property
    def volume_filled(self) -> int:
        return self.volume_total - self.volume_remain
    
    @property
    def is_complete(self) -> bool:
        return self.volume_remain == 0


# NOTE: TrackedTrade class moved to trade_tracker.py
# This file only handles ESI data fetching, not trade tracking logic


class ESIWallet:
    """Fetches wallet data from ESI."""

    def __init__(self, auth: ESIAuth):
        self.auth = auth
        
        # Cached data
        self.balance: float = 0
        self.transactions: List[Transaction] = []
        self.journal: List[JournalEntry] = []
        self.orders: List[MarketOrder] = []
        self.order_history: List[MarketOrder] = []
        
        self.last_update: Optional[datetime] = None
        
        # ESI cache expiry tracking - we track orders specifically
        # since that's what matters for trade sync (not balance which is ~60s)
        self.orders_cache_expires: Optional[datetime] = None

    def _parse_expires_header(self, response: requests.Response) -> Optional[datetime]:
        """Parse the Expires header from an ESI response."""
        expires_str = response.headers.get("Expires")
        if not expires_str:
            return None
        
        try:
            # ESI format: "Thu, 16 Jan 2025 14:05:22 GMT"
            from datetime import timezone
            return datetime.strptime(expires_str, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def get_seconds_until_refresh(self) -> float:
        """
        Get seconds until ESI orders cache expires.
        Returns 0 if no expiry known, or if already expired.
        Adds 1-second buffer to ensure fresh data.
        """
        if self.orders_cache_expires is None:
            return 0
        
        from datetime import timezone
        now = datetime.now(timezone.utc)
        delta = (self.orders_cache_expires - now).total_seconds()
        
        if delta <= 0:
            return 0
        
        # Add 1s buffer to ensure CCP servers have refreshed
        return delta + 1.0

    def _make_request(self, endpoint: str, params: dict = None) -> Optional[dict | list]:
        """Make authenticated ESI request."""
        headers = self.auth.get_auth_headers()
        if not headers:
            print("Not authenticated")
            return None

        url = f"{BASE_URL}{endpoint}"
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            
            # Track orders endpoint cache expiry specifically
            if "/orders/" in endpoint and "/history" not in endpoint:
                expires = self._parse_expires_header(response)
                if expires:
                    self.orders_cache_expires = expires
            
            return response.json()
        except requests.RequestException as e:
            print(f"ESI request error: {e}")
            return None

    def fetch_balance(self) -> float:
        """Fetch current wallet balance."""
        char_id = self.auth.character_id
        if not char_id:
            return 0
            
        data = self._make_request(f"/characters/{char_id}/wallet/")
        if data is not None:
            self.balance = float(data)
        return self.balance

    def fetch_transactions(self, from_id: int = None) -> List[Transaction]:
        """
        Fetch wallet transactions.
        Returns newest first. Use from_id to paginate backwards.
        """
        char_id = self.auth.character_id
        if not char_id:
            return []

        params = {"datasource": "tranquility"}
        if from_id:
            params["from_id"] = from_id

        data = self._make_request(f"/characters/{char_id}/wallet/transactions/", params)
        
        if data is not None:
            self.transactions = []
            for t in data:
                self.transactions.append(Transaction(
                    transaction_id=t["transaction_id"],
                    date=datetime.fromisoformat(t["date"].replace("Z", "+00:00")),
                    type_id=t["type_id"],
                    type_name="",  # Resolve later
                    quantity=t["quantity"],
                    unit_price=t["unit_price"],
                    is_buy=t["is_buy"],
                    location_id=t["location_id"],
                    journal_ref_id=t["journal_ref_id"]
                ))
            # Sort newest first
            self.transactions.sort(key=lambda x: x.date, reverse=True)
        
        return self.transactions

    def fetch_journal(self, page: int = 1) -> List[JournalEntry]:
        """
        Fetch wallet journal entries.
        Includes broker fees, sales tax, market escrow, etc.
        """
        char_id = self.auth.character_id
        if not char_id:
            return []

        params = {"datasource": "tranquility", "page": page}
        data = self._make_request(f"/characters/{char_id}/wallet/journal/", params)
        
        if data is not None:
            if page == 1:
                self.journal = []
            
            for j in data:
                self.journal.append(JournalEntry(
                    entry_id=j["id"],
                    date=datetime.fromisoformat(j["date"].replace("Z", "+00:00")),
                    ref_type=j.get("ref_type", ""),
                    amount=j.get("amount", 0),
                    balance=j.get("balance", 0),
                    description=j.get("description", ""),
                    context_id=j.get("context_id"),
                    context_type=j.get("context_id_type")
                ))
            
            self.journal.sort(key=lambda x: x.date, reverse=True)
        
        return self.journal

    def fetch_orders(self) -> List[MarketOrder]:
        """Fetch active market orders."""
        char_id = self.auth.character_id
        if not char_id:
            print("fetch_orders: No character_id")
            return []

        print(f"Fetching orders for character {char_id}...")
        data = self._make_request(f"/characters/{char_id}/orders/")
        
        print(f"Orders response: {type(data)}, length: {len(data) if data else 'None'}")
        
        if data is not None:
            self.orders = []
            for o in data:
                try:
                    # ESI returns is_buy_order directly as a boolean
                    # Default to False (sell order) if not present
                    is_buy = o.get("is_buy_order", False)
                    
                    self.orders.append(MarketOrder(
                        order_id=o["order_id"],
                        type_id=o["type_id"],
                        type_name="",
                        is_buy_order=is_buy,
                        price=o["price"],
                        volume_total=o["volume_total"],
                        volume_remain=o["volume_remain"],
                        issued=datetime.fromisoformat(o["issued"].replace("Z", "+00:00")),
                        duration=o["duration"],
                        location_id=o["location_id"],
                        escrow=o.get("escrow", 0)
                    ))
                except KeyError as e:
                    print(f"Order parse error, missing key: {e}")
                    print(f"Order data: {o}")
        
        buy_count = len([o for o in self.orders if o.is_buy_order])
        sell_count = len([o for o in self.orders if not o.is_buy_order])
        print(f"Parsed {len(self.orders)} orders ({buy_count} buy, {sell_count} sell)")
        return self.orders

    def fetch_order_history(self, page: int = 1) -> List[MarketOrder]:
        """Fetch historical (completed/cancelled) orders."""
        char_id = self.auth.character_id
        if not char_id:
            return []

        params = {"datasource": "tranquility", "page": page}
        data = self._make_request(f"/characters/{char_id}/orders/history/", params)
        
        if data is not None:
            if page == 1:
                self.order_history = []
            
            for o in data:
                try:
                    # ESI returns is_buy_order directly as a boolean
                    is_buy = o.get("is_buy_order", False)
                    
                    self.order_history.append(MarketOrder(
                        order_id=o["order_id"],
                        type_id=o["type_id"],
                        type_name="",
                        is_buy_order=is_buy,
                        price=o["price"],
                        volume_total=o["volume_total"],
                        volume_remain=o["volume_remain"],
                        issued=datetime.fromisoformat(o["issued"].replace("Z", "+00:00")),
                        duration=o["duration"],
                        location_id=o["location_id"],
                        state=o.get("state", "expired")
                    ))
                except KeyError as e:
                    print(f"Order history parse error, missing key: {e}")
                    print(f"Order data: {o}")
        
        return self.order_history

    def refresh_all(self, progress_callback=None) -> bool:
        """Refresh all wallet data."""
        if not self.auth.is_authenticated:
            return False

        try:
            if progress_callback:
                progress_callback("Fetching balance...", 0)
            self.fetch_balance()

            if progress_callback:
                progress_callback("Fetching transactions...", 20)
            self.fetch_transactions()

            if progress_callback:
                progress_callback("Fetching journal...", 40)
            self.fetch_journal()

            if progress_callback:
                progress_callback("Fetching active orders...", 60)
            self.fetch_orders()

            if progress_callback:
                progress_callback("Fetching order history...", 80)
            self.fetch_order_history()

            self.last_update = datetime.now()
            
            if progress_callback:
                progress_callback("Complete!", 100)
            
            return True
            
        except Exception as e:
            print(f"Error refreshing wallet data: {e}")
            return False

    # === Analysis helpers ===

    def get_broker_fee_for_order(self, order_id: int) -> float:
        """Find the broker fee journal entry for an order."""
        for entry in self.journal:
            if entry.ref_type == "brokers_fee" and entry.context_id == order_id:
                return abs(entry.amount)
        return 0

    def get_sales_tax_for_transaction(self, journal_ref_id: int) -> float:
        """Find the sales tax for a transaction via its journal reference."""
        # Sales tax entries have context_id pointing to the transaction
        for entry in self.journal:
            if entry.ref_type == "transaction_tax" and entry.entry_id == journal_ref_id:
                return abs(entry.amount)
        return 0

    def get_transactions_for_type(self, type_id: int, is_buy: Optional[bool] = None) -> List[Transaction]:
        """Get all transactions for a specific item type."""
        result = [t for t in self.transactions if t.type_id == type_id]
        if is_buy is not None:
            result = [t for t in result if t.is_buy == is_buy]
        return result

    def get_orders_for_type(self, type_id: int, is_buy: Optional[bool] = None) -> List[MarketOrder]:
        """Get all active orders for a specific item type."""
        result = [o for o in self.orders if o.type_id == type_id]
        if is_buy is not None:
            result = [o for o in result if o.is_buy_order == is_buy]
        return result

    def get_relist_fees_for_order(self, order_id: int) -> tuple[float, int]:
        """
        Find all modification fees for an order.
        Returns (total_fees, modification_count).
        """
        total = 0
        count = 0
        for entry in self.journal:
            # Relist fees show as brokers_fee with the order as context
            # but after the initial placement
            if entry.ref_type == "brokers_fee" and entry.context_id == order_id:
                # First one is placement, rest are relists
                if count > 0:
                    total += abs(entry.amount)
                count += 1
        
        return (total, max(0, count - 1))  # Subtract 1 for initial placement
