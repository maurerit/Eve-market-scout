"""Per-structure observed market history (Phase 1 — silent collection).

CCP has no `/markets/structures/{id}/history/` endpoint. We snapshot the
order book on every structure scan and infer daily activity from snapshot
deltas so structure hubs can eventually feed the same safety checks NPC
hubs feed (velocity gates, ceiling caps, market-crashing flags).

Phase 1 scope: storage + collection only. No consumer reads `structure_daily`
yet — that arrives in later phases. The derived rollup is still built
incrementally on every snapshot so it's queryable when consumers wire in.

See `PLAN_structure_history.md` for the full design and phasing.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sound_manager import get_data_dir

logger = logging.getLogger(__name__)

DB_FILENAME = "structure_history.db"
RETENTION_DAYS = 730  # 2 years

_SINGLETON: Optional["StructureHistoryDB"] = None
_SINGLETON_LOCK = threading.Lock()


class StructureHistoryDB:
    """SQLite singleton for per-structure observed history.

    Tables:
      structure_snapshots — one row per (structure, snapshot_at, order_id).
        The raw observation. Re-derivable rollups should always be rebuildable
        from this table.
      structure_daily — incremental per-day rollup. Columns map to the same
        shape `MarketHistoryDB.get_history` returns so a future dispatcher can
        feed the existing scanner safety checks with no per-call-site changes.
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_data_dir() / DB_FILENAME
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._ensure_schema()

    @classmethod
    def singleton(cls) -> "StructureHistoryDB":
        global _SINGLETON
        if _SINGLETON is None:
            with _SINGLETON_LOCK:
                if _SINGLETON is None:
                    _SINGLETON = cls()
        return _SINGLETON

    # =========================================================================
    # Connection
    # =========================================================================

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            c = sqlite3.connect(str(self.db_path))
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return self._local.conn

    def _ensure_schema(self) -> None:
        c = self._conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS structure_snapshots (
                structure_id  INTEGER NOT NULL,
                snapshot_at   TEXT    NOT NULL,
                order_id      INTEGER NOT NULL,
                type_id       INTEGER NOT NULL,
                is_buy        INTEGER NOT NULL,
                price         REAL    NOT NULL,
                volume_remain INTEGER NOT NULL,
                issued        TEXT    NOT NULL,
                duration      INTEGER NOT NULL,
                PRIMARY KEY (structure_id, snapshot_at, order_id)
            );
            CREATE INDEX IF NOT EXISTS idx_struct_type_time
                ON structure_snapshots (structure_id, type_id, snapshot_at);
            CREATE INDEX IF NOT EXISTS idx_struct_order
                ON structure_snapshots (structure_id, order_id, snapshot_at);

            CREATE TABLE IF NOT EXISTS structure_daily (
                structure_id     INTEGER NOT NULL,
                type_id          INTEGER NOT NULL,
                day_utc          TEXT    NOT NULL,
                volume_sold      INTEGER NOT NULL DEFAULT 0,
                sales_count      INTEGER NOT NULL DEFAULT 0,
                price_min        REAL,
                price_max        REAL,
                price_qty_sum    REAL    NOT NULL DEFAULT 0,
                snapshots_in_day INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (structure_id, type_id, day_utc)
            );
        """)
        c.commit()

    # =========================================================================
    # Public write entry point
    # =========================================================================

    def record_snapshot(self, structure_id: int, orders: list[dict],
                        now_utc: Optional[datetime] = None) -> None:
        """Persist a snapshot and incrementally update the daily rollup.

        Empty `orders` is skipped — treated as no-data / no-access rather than
        a genuine empty market. Better to lose one rare all-clear day than to
        attribute false fills on every 403.

        All exceptions are caught and logged; collection must never break the
        scanner's order-fetch path.
        """
        if not orders:
            return
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        try:
            self._record_snapshot_inner(structure_id, orders, now_utc)
        except Exception:
            logger.exception("[StructHist] record_snapshot failed for %s",
                             structure_id)

    def _record_snapshot_inner(self, structure_id: int, orders: list[dict],
                               now_utc: datetime) -> None:
        snapshot_at = now_utc.isoformat()
        conn = self._conn()

        prior_at = conn.execute(
            "SELECT MAX(snapshot_at) FROM structure_snapshots "
            "WHERE structure_id = ? AND snapshot_at < ?",
            (structure_id, snapshot_at),
        ).fetchone()[0]

        rows = []
        for o in orders:
            if "order_id" not in o or "type_id" not in o:
                continue
            try:
                rows.append((
                    structure_id,
                    snapshot_at,
                    int(o["order_id"]),
                    int(o["type_id"]),
                    1 if o.get("is_buy_order") else 0,
                    float(o["price"]),
                    int(o["volume_remain"]),
                    str(o.get("issued", "")),
                    int(o.get("duration", 0)),
                ))
            except (TypeError, ValueError):
                continue

        if not rows:
            return

        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO structure_snapshots "
                "(structure_id, snapshot_at, order_id, type_id, is_buy, "
                "price, volume_remain, issued, duration) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

            if prior_at is not None:
                self._infer_and_apply(conn, structure_id, prior_at, snapshot_at)

            cutoff_ts = (now_utc - timedelta(days=RETENTION_DAYS)).isoformat()
            cutoff_day = (now_utc - timedelta(days=RETENTION_DAYS)).date().isoformat()
            conn.execute(
                "DELETE FROM structure_snapshots "
                "WHERE structure_id = ? AND snapshot_at < ?",
                (structure_id, cutoff_ts),
            )
            conn.execute(
                "DELETE FROM structure_daily "
                "WHERE structure_id = ? AND day_utc < ?",
                (structure_id, cutoff_day),
            )

    # =========================================================================
    # Inference
    # =========================================================================

    def _infer_and_apply(self, conn: sqlite3.Connection, structure_id: int,
                         prior_at: str, current_at: str) -> None:
        """Compare prior↔current snapshots; apply inferred fills to daily.

        Rules (per `(structure_id, order_id)`):
          - volume_remain dropped by X → partial fill of X at order's price.
          - Present in prior, absent in current, BEFORE issued+duration →
            likely full fill of last-known volume_remain.
          - Present in prior, absent in current, AT/AFTER expiry → ignored
            (expired or cancelled — indistinguishable; not a fill).
          - Absent in prior, present in current → new listing (not a fill).
        """
        prior = {
            r["order_id"]: r
            for r in conn.execute(
                "SELECT order_id, type_id, price, volume_remain, issued, duration "
                "FROM structure_snapshots "
                "WHERE structure_id = ? AND snapshot_at = ?",
                (structure_id, prior_at),
            )
        }
        current_order_ids = {
            r[0]
            for r in conn.execute(
                "SELECT order_id FROM structure_snapshots "
                "WHERE structure_id = ? AND snapshot_at = ?",
                (structure_id, current_at),
            )
        }
        current_remain = {
            r["order_id"]: r["volume_remain"]
            for r in conn.execute(
                "SELECT order_id, volume_remain FROM structure_snapshots "
                "WHERE structure_id = ? AND snapshot_at = ?",
                (structure_id, current_at),
            )
        }

        try:
            current_dt = datetime.fromisoformat(current_at)
        except ValueError:
            return
        day_utc = current_dt.date().isoformat()

        touched_types: set[int] = set()

        for order_id, prior_row in prior.items():
            type_id = prior_row["type_id"]
            price = prior_row["price"]

            if order_id in current_order_ids:
                delta = prior_row["volume_remain"] - current_remain[order_id]
                if delta > 0:
                    self._apply_fill(conn, structure_id, type_id, day_utc,
                                     qty=delta, price=price)
                    touched_types.add(type_id)
            else:
                expiry = _parse_expiry(prior_row["issued"], prior_row["duration"])
                if expiry is not None and current_dt < expiry:
                    qty = prior_row["volume_remain"]
                    if qty > 0:
                        self._apply_fill(conn, structure_id, type_id, day_utc,
                                         qty=qty, price=price)
                        touched_types.add(type_id)
                # else: expired/ambiguous — don't count as a fill.

        for type_id in touched_types:
            conn.execute(
                "UPDATE structure_daily "
                "SET snapshots_in_day = snapshots_in_day + 1 "
                "WHERE structure_id = ? AND type_id = ? AND day_utc = ?",
                (structure_id, type_id, day_utc),
            )

    def _apply_fill(self, conn: sqlite3.Connection, structure_id: int,
                    type_id: int, day_utc: str, qty: int, price: float) -> None:
        existing = conn.execute(
            "SELECT price_min, price_max FROM structure_daily "
            "WHERE structure_id = ? AND type_id = ? AND day_utc = ?",
            (structure_id, type_id, day_utc),
        ).fetchone()

        if existing is None:
            conn.execute(
                "INSERT INTO structure_daily "
                "(structure_id, type_id, day_utc, volume_sold, sales_count, "
                " price_min, price_max, price_qty_sum, snapshots_in_day) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?, 0)",
                (structure_id, type_id, day_utc, qty, price, price, qty * price),
            )
            return

        new_min = price if existing["price_min"] is None else min(existing["price_min"], price)
        new_max = price if existing["price_max"] is None else max(existing["price_max"], price)
        conn.execute(
            "UPDATE structure_daily SET "
            "  volume_sold = volume_sold + ?, "
            "  sales_count = sales_count + 1, "
            "  price_min = ?, "
            "  price_max = ?, "
            "  price_qty_sum = price_qty_sum + ? "
            "WHERE structure_id = ? AND type_id = ? AND day_utc = ?",
            (qty, new_min, new_max, qty * price,
             structure_id, type_id, day_utc),
        )


def _parse_expiry(issued: str, duration: int) -> Optional[datetime]:
    """Return UTC expiry datetime, or None if unparseable."""
    if not issued:
        return None
    try:
        s = issued.replace("Z", "+00:00")
        issued_dt = datetime.fromisoformat(s)
        if issued_dt.tzinfo is None:
            issued_dt = issued_dt.replace(tzinfo=timezone.utc)
        return issued_dt + timedelta(days=int(duration))
    except (TypeError, ValueError):
        return None
