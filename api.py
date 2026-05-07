"""ESI API client with async requests, rate limiting, and system data."""

import asyncio
import aiohttp
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from config import (
    ESI_BASE_URL, MAX_CONCURRENT_REQUESTS, REQUEST_TIMEOUT,
    JITA_SYSTEM_ID, MIN_SECURITY_STATUS,
    JITA_REGION_ID,
)
from esi_supplement import ESISupplementCache
from market_history import MarketHistoryDB
from ssl_context import make_connector
from order_cache import OrderCacheStore
from type_name_mixin import TypeNameMixin

# Local debug flag - set True to enable diagnostic output
DEBUG_ESI = False


class ESIClient(TypeNameMixin):
    """Async client for EVE ESI API."""

    def __init__(self):
        self.semaphore: Optional[asyncio.Semaphore] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.type_name_cache: dict[int, str] = {}
        self.system_cache: dict[int, dict] = {}
        self.valid_systems: set[int] = set()

        # ESI cache expiry tracking
        self.market_expires: Optional[datetime] = None

        # Legacy Jita-specific cache (mostly superseded by order_cache)
        self.jita_orders_cache: list[dict] = []
        self.jita_orders_timestamp: Optional[datetime] = None

        # History cache: region_id -> {type_id -> history_list}
        self.history_cache: dict[int, dict[int, list[dict]]] = {}

        # Market history database (set externally, uses SQLite)
        self.market_history: Optional[MarketHistoryDB] = None

        # ESI supplement cache (for items missing from market history db)
        self.supplement = ESISupplementCache()

        # Shared per-region order cache (scanner <-> stock market)
        self.order_cache = OrderCacheStore()

        # Backfill legacy Jita fields from disk-loaded cache so has_jita_cache()
        # returns True on startup without forcing a fresh ESI fetch.
        _jita_entry = self.order_cache._order_cache.get(JITA_REGION_ID)
        if _jita_entry and _jita_entry.get('orders'):
            self.jita_orders_cache = _jita_entry['orders']
            self.jita_orders_timestamp = _jita_entry.get('timestamp')

    def reset_for_new_loop(self):
        """Reset async primitives for a new event loop. Preserves caches."""
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    def ensure_session(self) -> aiohttp.ClientSession:
        """Ensure a valid aiohttp session exists, creating one if needed."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                connector=make_connector(),
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            )
        return self.session

    def clear_jita_cache(self):
        """Clear Jita orders cache to force refresh on next scan."""
        self.jita_orders_cache = []
        self.jita_orders_timestamp = None

    def has_jita_cache(self) -> bool:
        """Check if Jita orders are cached."""
        return len(self.jita_orders_cache) > 0

    def get_jita_cache_age(self) -> str:
        """Get human-readable age of Jita cache."""
        if self.jita_orders_timestamp is None:
            return "No cache"

        age = datetime.now(timezone.utc) - self.jita_orders_timestamp
        minutes = int(age.total_seconds() / 60)

        if minutes < 1:
            return "< 1 min"
        elif minutes < 60:
            return f"{minutes} min"
        else:
            hours = minutes // 60
            return f"{hours}h {minutes % 60}m"

    def get_history_cache_stats(self) -> str:
        """Get human-readable history cache stats."""
        if self.market_history is not None:
            try:
                stats = self.market_history.get_stats()
                row_count = stats.get('row_count', 0)
                if row_count > 0:
                    return f"{row_count:,} records (SQLite)"
            except Exception:
                pass

        total = sum(len(types) for types in self.history_cache.values())
        return f"{total} items cached (ESI)"

    def clear_history_cache(self):
        """Clear history cache to force refresh."""
        self.history_cache = {}

    # =========================================================================
    # Order cache delegation (backed by OrderCacheStore)
    # =========================================================================

    def cache_orders_for_region(self, region_id: int, orders: list,
                                 expires: Optional[datetime] = None):
        self.order_cache.cache_orders_for_region(region_id, orders, expires)

    def get_cached_orders(self, region_id: int, max_age_seconds: int = 300) -> Optional[list]:
        return self.order_cache.get_cached_orders(region_id, max_age_seconds)

    def clear_region_disk_cache(self, region_id: int):
        self.order_cache.clear_region_disk_cache(region_id)

    # =========================================================================
    # ESI expiry / refresh timing
    # =========================================================================

    def _apply_earliest_expires(self, expires: Optional[datetime]):
        """Update market_expires using earliest-wins rule.

        Past timestamps are ignored to prevent zeroing the countdown on stale
        cache hits or clock skew.
        """
        if expires is None:
            return
        if expires <= datetime.now(timezone.utc):
            return
        if self.market_expires is None or expires < self.market_expires:
            self.market_expires = expires

    def get_seconds_until_refresh(self) -> float:
        """Get seconds until ESI market cache expires. Returns 0 if unknown or expired."""
        if self.market_expires is None:
            return 0

        now = datetime.now(timezone.utc)
        delta = (self.market_expires - now).total_seconds()

        if delta <= 0:
            return 0

        return delta + 1.0  # 1s buffer so CCP servers have refreshed

    def _parse_expires_header(self, response: aiohttp.ClientResponse) -> Optional[datetime]:
        """Parse cache expiry from ESI response headers (Cache-Control first, Expires fallback)."""
        cache_control = response.headers.get("Cache-Control", "")
        match = re.search(r'max-age=(\d+)', cache_control)
        if match:
            max_age = int(match.group(1))
            return datetime.now(timezone.utc) + timedelta(seconds=max_age)

        expires_str = response.headers.get("Expires")
        if not expires_str:
            return None

        try:
            return datetime.strptime(expires_str, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            connector=make_connector(),
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _get(self, endpoint: str, params: dict = None) -> dict | list:
        """Make a rate-limited GET request."""
        async with self.semaphore:
            url = f"{ESI_BASE_URL}{endpoint}"
            async with self.session.get(url, params=params) as response:
                response.raise_for_status()
                return await response.json()

    async def get_system_info(self, system_id: int) -> dict:
        """Get system info including security status and stargates."""
        if system_id in self.system_cache:
            return self.system_cache[system_id]

        try:
            data = await self._get(f"/universe/systems/{system_id}/")

            neighbors = []
            if "stargates" in data:
                stargate_tasks = [
                    self._get(f"/universe/stargates/{sg_id}/")
                    for sg_id in data["stargates"]
                ]
                stargates = await asyncio.gather(*stargate_tasks, return_exceptions=True)
                for sg in stargates:
                    if isinstance(sg, dict) and "destination" in sg:
                        neighbors.append(sg["destination"]["system_id"])

            info = {
                "security": data.get("security_status", 0),
                "name": data.get("name", f"System {system_id}"),
                "neighbors": neighbors
            }
            self.system_cache[system_id] = info
            return info
        except Exception:
            return {"security": 0, "name": "Unknown", "neighbors": []}

    async def build_valid_systems_cache(self, progress_callback=None, system_ids: list[int] = None) -> set[int]:
        """Check which systems are high-sec from a list of system IDs."""
        if not system_ids:
            return self.valid_systems

        def update(text):
            if progress_callback:
                progress_callback(text, 5)

        uncached = [sid for sid in system_ids if sid not in self.system_cache]

        if uncached:
            update(f"Checking {len(uncached)} systems...")
            tasks = [self._get_system_security(sid) for sid in uncached]
            await asyncio.gather(*tasks, return_exceptions=True)

        for system_id in system_ids:
            security = self.system_cache.get(system_id, {}).get("security", 0)
            if security >= MIN_SECURITY_STATUS:
                self.valid_systems.add(system_id)

        return self.valid_systems

    async def _get_system_security(self, system_id: int):
        """Fetch just the security status for a system."""
        if system_id in self.system_cache:
            return
        try:
            data = await self._get(f"/universe/systems/{system_id}/")
            self.system_cache[system_id] = {
                "security": data.get("security_status", 0),
                "name": data.get("name", f"System {system_id}")
            }
        except Exception:
            self.system_cache[system_id] = {"security": 0, "name": "Unknown"}

    async def get_market_orders(self, region_id: int, use_cache: bool = False,
                                 force_refresh: bool = False) -> list[dict]:
        """Fetch all market orders for a region (handles pagination).

        Cache layering (when force_refresh is False):
        - First: shared per-region cache (5 min freshness + ESI expires check)
        - Then: legacy Jita-specific cache (if use_cache=True and Jita region)
        - Otherwise: fetch from ESI

        Concurrent calls for the same region are serialized via a per-region
        lock so a second caller reads from the populated cache instead of
        starting a duplicate fetch.
        """
        if not force_refresh:
            cached = self.order_cache.get_cached_orders(region_id)
            if cached is not None:
                cache_entry = self.order_cache._order_cache.get(region_id, {})
                self._apply_earliest_expires(cache_entry.get('expires'))
                return cached

            if use_cache and region_id == JITA_REGION_ID and self.jita_orders_cache:
                return self.jita_orders_cache

        fetch_lock = self.order_cache._get_region_fetch_lock(region_id)
        fetch_lock.acquire()
        try:
            # Re-check after acquiring lock — another thread may have populated cache
            if not force_refresh:
                cached = self.order_cache.get_cached_orders(region_id)
                if cached is not None:
                    cache_entry = self.order_cache._order_cache.get(region_id, {})
                    self._apply_earliest_expires(cache_entry.get('expires'))
                    return cached

            all_orders = []

            url = f"{ESI_BASE_URL}/markets/{region_id}/orders/"
            async with self.semaphore:
                async with self.session.get(url, params={"page": 1}) as response:
                    response.raise_for_status()
                    total_pages = int(response.headers.get("X-Pages", 1))
                    expires = self._parse_expires_header(response)
                    self._apply_earliest_expires(expires)
                    all_orders.extend(await response.json())

            if total_pages > 1:
                tasks = [
                    self._get_page_safe(f"/markets/{region_id}/orders/", p)
                    for p in range(2, total_pages + 1)
                ]
                results = await asyncio.gather(*tasks)
                for page_orders in results:
                    if page_orders:
                        all_orders.extend(page_orders)

            self.order_cache.cache_orders_for_region(region_id, all_orders, expires=expires)

            # Legacy Jita cache (preserve existing behavior)
            if region_id == JITA_REGION_ID:
                self.jita_orders_cache = all_orders
                self.jita_orders_timestamp = datetime.now(timezone.utc)

            return all_orders
        finally:
            fetch_lock.release()

    async def _get_page_safe(self, endpoint: str, page: int) -> list | None:
        """Fetch a page, returning None on 404 (stale pagination)."""
        try:
            return await self._get(endpoint, {"page": page})
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return None
            raise

    def get_system_security(self, system_id: int) -> float:
        """Get cached security status for a system."""
        return self.system_cache.get(system_id, {}).get("security", 0)

    def is_valid_system(self, system_id: int) -> bool:
        """Check if system is within range and high-sec."""
        return system_id in self.valid_systems

    async def get_market_history(self, region_id: int, type_id: int) -> list[dict]:
        """Fetch market history for a specific item in a region."""
        try:
            return await self._get(f"/markets/{region_id}/history/", {"type_id": type_id})
        except Exception:
            return []

    async def get_market_history_bulk(self, region_id: int, type_ids: list[int],
                                       use_cache: bool = True) -> dict[int, list[dict]]:
        """Fetch market history for multiple items.

        Priority:
        1. Market history database (SQLite) - instant indexed lookups
        2. ESI supplement cache (for items missing from db)
        3. In-memory ESI cache
        4. ESI API calls for missing items
        """
        if self.market_history is not None:
            result = self.market_history.get_history_bulk(region_id, type_ids, days=30)

            missing_from_db = [tid for tid in type_ids if not result.get(tid)]
            has_data = [tid for tid in type_ids if result.get(tid)]

            if DEBUG_ESI:
                print(f"[DIAG] Market history DB for region {region_id}:")
                print(f"[DIAG]   Requested: {len(type_ids)} items")
                print(f"[DIAG]   Have data: {len(has_data)} items")
                print(f"[DIAG]   Missing (empty): {len(missing_from_db)} items")

            if missing_from_db:
                still_missing = []
                supplement_hits = 0

                for tid in missing_from_db:
                    cached = self.supplement.get_if_fresh(region_id, tid)
                    if cached is not None:
                        result[tid] = cached
                        supplement_hits += 1
                    else:
                        still_missing.append(tid)

                if supplement_hits > 0 and DEBUG_ESI:
                    print(f"[DIAG]   ESI supplement cache: {supplement_hits} items")

                if still_missing:
                    known_bad = [tid for tid in still_missing
                                 if self.supplement.is_known_bad(region_id, tid)]
                    to_fetch = [tid for tid in still_missing if tid not in known_bad]

                    if known_bad and DEBUG_ESI:
                        print(f"[DIAG]   Skipping {len(known_bad)} known-bad items (2+ errors)")
                    for tid in known_bad:
                        result[tid] = []

                    if to_fetch:
                        if DEBUG_ESI:
                            print(f"[DIAG]   Still need ESI fetch: {len(to_fetch)} items")
                            print(f"[DIAG]   Missing type_ids (first 20): {to_fetch[:20]}")

                        esi_results, esi_errors = await self._fetch_history_from_esi(
                            region_id, to_fetch, track_errors=True)

                        esi_got_data = esi_empty = esi_error = 0
                        for tid in to_fetch:
                            if tid in esi_errors:
                                esi_error += 1
                                self.supplement.store(region_id, tid, [], is_error=True)
                                result[tid] = []
                            else:
                                data = esi_results.get(tid, [])
                                if data:
                                    esi_got_data += 1
                                    self.supplement.store(region_id, tid, data)
                                else:
                                    esi_empty += 1
                                    self.supplement.store(region_id, tid, [])
                                result[tid] = data

                        if DEBUG_ESI:
                            print(f"[DIAG]   ESI fallback: {esi_got_data} got data, "
                                  f"{esi_empty} empty, {esi_error} errors")

            final_with_data = sum(1 for tid in type_ids if result.get(tid))
            final_empty = sum(1 for tid in type_ids if not result.get(tid))
            if DEBUG_ESI:
                print(f"[DIAG]   Final: {final_with_data} with data, {final_empty} empty")

            if region_id not in self.history_cache:
                self.history_cache[region_id] = {}
            self.history_cache[region_id].update(result)

            return result

        # === FALLBACK: Original ESI-based approach ===
        if region_id not in self.history_cache:
            self.history_cache[region_id] = {}

        region_cache = self.history_cache[region_id]
        uncached = [tid for tid in type_ids if tid not in region_cache] if use_cache else list(type_ids)

        if type_ids and DEBUG_ESI:
            cached_count = len(type_ids) - len(uncached)
            if uncached:
                print(f"[DIAG] History cache: {cached_count}/{len(type_ids)} cached, "
                      f"fetching {len(uncached)} from ESI")
            else:
                print(f"[DIAG] History cache: {cached_count}/{len(type_ids)} cached, no ESI calls needed")

        if uncached:
            esi_results, _ = await self._fetch_history_from_esi(region_id, uncached, track_errors=False)
            region_cache.update(esi_results)

        return {tid: region_cache.get(tid, []) for tid in type_ids}

    async def _fetch_history_from_esi(self, region_id: int, type_ids: list[int],
                                       track_errors: bool = False) -> tuple[dict[int, list[dict]], set[int]]:
        """Fetch history for multiple items from ESI API."""
        result = {}
        errors = set()

        success_count = error_count = empty_count = 0
        errors_seen = {}

        tasks = [self._get_market_history_raw(region_id, tid) for tid in type_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for tid, history in zip(type_ids, results):
            if isinstance(history, Exception):
                error_count += 1
                err_type = type(history).__name__
                errors_seen[err_type] = errors_seen.get(err_type, 0) + 1
                if track_errors:
                    errors.add(tid)
                else:
                    result[tid] = []
            elif isinstance(history, list):
                if history:
                    success_count += 1
                else:
                    empty_count += 1
                result[tid] = history
            else:
                error_count += 1
                if track_errors:
                    errors.add(tid)
                else:
                    result[tid] = []

        if DEBUG_ESI:
            print(f"[DIAG] ESI fetch for {len(type_ids)} items: "
                  f"{success_count} success, {empty_count} empty, {error_count} errors")
            if errors_seen:
                print(f"[DIAG]   Error types: {errors_seen}")

        return result, errors

    async def _get_market_history_raw(self, region_id: int, type_id: int) -> list[dict]:
        """Fetch market history with exceptions not caught (for diagnostics)."""
        return await self._get(f"/markets/{region_id}/history/", {"type_id": type_id})
