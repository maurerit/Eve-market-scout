"""ESI sync layer for scanner_inventory.InventoryManager.

Populates InventoryManager from ESIWallet data. Only acts on type_ids that
already have an InventoryEntry (flagged via "Add to Tracker"). Idempotent --
dedup is handled inside InventoryManager via transaction_id / order_id.

Designed to run alongside the existing TradeTracker sync without interfering.
"""

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from scanner_inventory import InventoryManager
    from esi_wallet import ESIWallet


def _ts(value) -> str:
    """Convert datetime (or anything) to ISO string."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _sales_tax_for(wallet, transaction) -> float:
    """Find the transaction_tax journal entry matching a sell transaction."""
    for entry in wallet.journal:
        if entry.ref_type != "transaction_tax":
            continue
        if entry.context_id == transaction.transaction_id:
            return abs(entry.amount)
        if entry.entry_id == transaction.journal_ref_id:
            return abs(entry.amount)
    return 0.0


def sync_inventory_from_wallet(inventory: "InventoryManager",
                               wallet: Optional["ESIWallet"]) -> dict:
    """Sync ESI wallet data into scanner inventory.

    Only processes transactions/orders for type_ids that already have an
    InventoryEntry. Order of operations matters:
      1. Buys (so FIFO lots exist before sales consume them)
      2. Active sell orders (add/update + relist detection)
      3. Sales (FIFO-matched against lots from step 1)
      4. Retire orders that vanished from active list (look up final state)
    """
    results = {
        "buys_recorded": 0,
        "sales_recorded": 0,
        "listings_added": 0,
        "listings_updated": 0,
        "relists_recorded": 0,
        "orders_retired": 0,
    }

    if wallet is None:
        return results

    tracked_type_ids = set(inventory.entries.keys())
    if not tracked_type_ids:
        return results

    # 1. Buys first -- lots must exist before sales can FIFO-consume them.
    for tx in wallet.transactions:
        if not tx.is_buy:
            continue
        if tx.type_id not in tracked_type_ids:
            continue
        entry = inventory.entries[tx.type_id]
        _, was_new = inventory.record_buy(
            type_id=tx.type_id,
            type_name=entry.type_name or tx.type_name,
            transaction_id=tx.transaction_id,
            buy_price=tx.unit_price,
            quantity=tx.quantity,
            bought_at=_ts(tx.date),
        )
        if was_new:
            results["buys_recorded"] += 1

    # 2. Active sell orders -- add/update and detect relists.
    active_order_ids_per_type: dict = {}
    for order in wallet.orders:
        if order.is_buy_order:
            continue
        if order.type_id not in tracked_type_ids:
            continue

        active_order_ids_per_type.setdefault(order.type_id, set()).add(order.order_id)

        entry = inventory.entries[order.type_id]
        existing = next(
            (a for a in entry.active_listings if a.order_id == order.order_id),
            None
        )

        # Broker fee is only meaningful when first creating the listing record.
        # On updates, add_or_update_listing ignores it (early-returns before adding to fees).
        broker_fee = 0.0
        if existing is None:
            broker_fee = wallet.get_broker_fee_for_order(order.order_id) or 0.0

        _, listing, was_new = inventory.add_or_update_listing(
            type_id=order.type_id,
            type_name=entry.type_name or order.type_name,
            order_id=order.order_id,
            list_price=order.price,
            qty_listed=order.volume_total,
            qty_remaining=order.volume_remain,
            listed_at=_ts(order.issued),
            broker_fee=broker_fee,
        )
        if was_new:
            results["listings_added"] += 1
        else:
            results["listings_updated"] += 1
            # Price changed -> relist
            if abs(order.price - listing.current_price) > 0.001:
                relist_fees, relist_count = wallet.get_relist_fees_for_order(order.order_id)
                if relist_count > listing.relist_count:
                    new_fee = max(0.0, relist_fees - listing.relist_fees)
                    inventory.record_relist(
                        type_id=order.type_id,
                        order_id=order.order_id,
                        new_price=order.price,
                        fee=new_fee,
                    )
                    results["relists_recorded"] += 1

    # 3. Sales -- after buys so FIFO has lots to consume.
    for tx in wallet.transactions:
        if tx.is_buy:
            continue
        if tx.type_id not in tracked_type_ids:
            continue
        entry = inventory.entries[tx.type_id]
        sales_tax = _sales_tax_for(wallet, tx)
        _, was_new = inventory.record_sale(
            type_id=tx.type_id,
            type_name=entry.type_name or tx.type_name,
            transaction_id=tx.transaction_id,
            sell_price=tx.unit_price,
            quantity=tx.quantity,
            sales_tax=sales_tax,
            sold_at=_ts(tx.date),
        )
        if was_new:
            results["sales_recorded"] += 1

    # 4. Retire orders that disappeared from active list.
    # Reason: items expire after 90 days (returns to hangar, qty_out unchanged)
    # or get cancelled (same effect) or get fulfilled (qty_out already accounted
    # via the matching sell transaction in step 3).
    for type_id in tracked_type_ids:
        entry = inventory.entries[type_id]
        active_ids = active_order_ids_per_type.get(type_id, set())
        for listing in list(entry.active_listings):
            if listing.order_id in active_ids:
                continue
            # Look up final state from order_history; default to "fulfilled"
            # if not found (matches gui_tracking_sync's existing fallback).
            state = "fulfilled"
            for hist in wallet.order_history:
                if hist.order_id == listing.order_id:
                    state = hist.state
                    break
            inventory.retire_listing(type_id, listing.order_id, state)
            results["orders_retired"] += 1

    return results
