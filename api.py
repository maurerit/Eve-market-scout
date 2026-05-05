"""ESI API client with async requests, rate limiting, and system data."""

import asyncio
import aiohttp
import re
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from config import (
    ESI_BASE_URL, MAX_CONCURRENT_REQUESTS, REQUEST_TIMEOUT,
    JITA_SYSTEM_ID, MIN_SECURITY_STATUS,
    JITA_REGION_ID
)
from esi_supplement import ESISupplementCache
from market_history import MarketHistoryDB
from ssl_context import make_connector

# Local debug flag - set True to enable diagnostic output
DEBUG_ESI = False

# SDE manager for local type lookups (lazy import to avoid circular deps)
_sde_manager = None

def _get_sde():
    """Get SDE manager instance (lazy load)."""
    global _sde_manager
    if _sde_manager is None:
        try:
            from sde_manager import get_sde_manager
            _sde_manager = get_sde_manager()
        except ImportError:
            _sde_manager = False  # Mark as unavailable
    return _sde_manager if _sde_manager else None


class ESIClient:
    """Async client for EVE ESI API."""

    def __init__(self):
        self.semaphore: Optional[asyncio.Semaphore] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.type_name_cache: dict[int, str] = {}
        self.system_cache: dict[int, dict] = {}  # {system_id: {security, neighbors}}
        self.valid_systems: set[int] = set()  # Systems within jump range and high-sec
        
        # ESI cache expiry tracking
        self.market_expires: Optional[datetime] = None  # When market data refreshes
        
        # === CACHING FOR PERFORMANCE ===
        # Jita orders cache (refresh manually, not every scan)
        self.jita_orders_cache: list[dict] = []
        self.jita_orders_timestamp: Optional[datetime] = None
        
        # History cache: region_id -> {type_id -> history_list}
        # Simple in-memory, no disk persistence, no staleness checks
        self.history_cache: dict[int, dict[int, list[dict]]] = {}
        
        # Market history database (set externally, uses SQLite)
        self.market_history: Optional[MarketHistoryDB] = None
        
        # ESI supplement cache (for items missing from market history db)
        self.supplement = ESISupplementCache()
        
        # Per-region fetch locks (in-flight guard for shared order cache).
        # Prevents duplicate concurrent fetches for the same region.
        self._inflight_locks: dict[int, threading.Lock] = {}
        self._inflight_locks_meta = threading.Lock()

    def reset_for_new_loop(self):
        """Reset async primitives for a new event loop. Preserves caches."""
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    def ensure_session(self) -> aiohttp.ClientSession:
        """Ensure a valid aiohttp session exists, creating one if needed.
        
        Call this before any async work. Handles cases where:
        - Session was never created
        - Session was closed by a previous operation (e.g., scanner)
        
        Returns:
            Active aiohttp.ClientSession
        """
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
        # Prefer market history database if available
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
    # Bidirectional Order Caching (Scanner <-> Stock Market)
    # =========================================================================
    
    def cache_orders_for_region(self, region_id: int, orders: list,
                                  expires: Optional[datetime] = None):
        """Cache orders fetched by Stock Market for Scanner use.
        
        Enables bidirectional data sharing between systems.
        
        Args:
            region_id: Region ID the orders are from
            orders: List of market orders
            expires: ESI Expires timestamp for this data (used to keep the
                     scanner's countdown timer accurate on cache hits)
        """
        if not hasattr(self, '_order_cache'):
            self._order_cache = {}
        
        self._order_cache[region_id] = {
            'orders': orders,
            'timestamp': datetime.now(timezone.utc),
            'expires': expires,
        }
        print(f"[API] Cached {len(orders)} orders for region {region_id}")
    
    def get_cached_orders(self, region_id: int, max_age_seconds: int = 300) -> Optional[list]:
        """Get cached orders if fresh enough.
        
        Two independent freshness checks:
        1. Timestamp age must be under max_age_seconds (5 min default).
        2. Stored ESI Expires must still be in the future.
        
        ESI's Cache-Control: max-age value can be anywhere from a few
        seconds to ~300 depending on where in CCP's refresh cycle the
        original fetch landed. A "fresh" cache entry by timestamp can
        already be past its ESI expires. Treating such entries as stale
        forces a live fetch, which yields a fresh Expires header and
        keeps the scanner countdown synced to ESI rather than
        collapsing to the 60s fallback.
        
        Args:
            region_id: Region to get orders for
            max_age_seconds: Maximum age in seconds (default 5 minutes)
            
        Returns:
            List of orders, or None if no fresh cache
        """
        if not hasattr(self, '_order_cache'):
            return None
        
        cached = self._order_cache.get(region_id)
        if not cached:
            return None
        
        now = datetime.now(timezone.utc)
        age = (now - cached['timestamp']).total_seconds()
        if age > max_age_seconds:
            return None
        
        # ESI deadline check: if the stored Expires has already passed,
        # treat as stale regardless of timestamp age. Otherwise a cache
        # hit would feed a past timestamp into market_expires and
        # poison the auto-refresh countdown.
        expires = cached.get('expires')
        if expires is not None and expires <= now:
            return None
        
        print(f"[API] Using cached orders for region {region_id} (age: {age:.0f}s)")
        return cached['orders']

    def _get_region_fetch_lock(self, region_id: int) -> threading.Lock:
        """Get (or create) a per-region fetch lock for in-flight guard.
        
        Used by get_market_orders to serialize concurrent fetches for the
        same region. Second caller blocks until the first completes, then
        reads from the now-populated shared cache.
        """
        with self._inflight_locks_meta:
            if region_id not in self._inflight_locks:
                self._inflight_locks[region_id] = threading.Lock()
            return self._inflight_locks[region_id]

    def _apply_earliest_expires(self, expires: Optional[datetime]):
        """Update self.market_expires using earliest-wins rule.
        
        Used by both the ESI fetch path and the cache-hit path so the
        scanner's countdown timer reflects whichever region's data goes
        stale first across a multi-region scan.
        
        Past timestamps are ignored. Belt-and-suspenders against any
        path feeding a stale expires (cache hit on an entry whose ESI
        deadline already passed, clock skew, etc.). Setting
        market_expires to a past time would zero out the countdown
        and trigger the 60s fallback every cycle.
        """
        if expires is None:
            return
        if expires <= datetime.now(timezone.utc):
            return
        if self.market_expires is None or expires < self.market_expires:
            self.market_expires = expires

    def get_seconds_until_refresh(self) -> float:
        """
        Get seconds until ESI market cache expires.
        Returns 0 if no expiry known, or if already expired.
        Adds 1-second buffer to ensure fresh data.
        """
        if self.market_expires is None:
            return 0
        
        now = datetime.now(timezone.utc)
        delta = (self.market_expires - now).total_seconds()
        
        if delta <= 0:
            return 0
        
        # Add 1s buffer to ensure CCP servers have refreshed
        return delta + 1.0

    def _parse_expires_header(self, response: aiohttp.ClientResponse) -> Optional[datetime]:
        """Parse cache expiry from ESI response headers.
        
        Checks Cache-Control first (new ESI format), falls back to Expires.
        """
        # Try Cache-Control: max-age=XXX first (new ESI format)
        cache_control = response.headers.get("Cache-Control", "")
        match = re.search(r'max-age=(\d+)', cache_control)
        if match:
            max_age = int(match.group(1))
            return datetime.now(timezone.utc) + timedelta(seconds=max_age)
        
        # Fall back to Expires header (legacy)
        expires_str = response.headers.get("Expires")
        if not expires_str:
            return None
        
        try:
            # ESI format: "Thu, 16 Jan 2025 14:05:22 GMT"
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

            # Get connected systems via stargates
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
            return {"security": 0, "name": f"Unknown", "neighbors": []}

    async def build_valid_systems_cache(self, progress_callback=None, system_ids: list[int] = None) -> set[int]:
        """
        Check which systems are high-sec from a list of system IDs.
        Much faster than BFS - only checks systems that have orders.
        """
        if not system_ids:
            return self.valid_systems

        def update(text):
            if progress_callback:
                progress_callback(text, 5)

        # Only fetch systems we haven't seen
        uncached = [sid for sid in system_ids if sid not in self.system_cache]

        if uncached:
            update(f"Checking {len(uncached)} systems...")
            # Batch fetch system info - just need security status
            tasks = []
            for sid in uncached:
                tasks.append(self._get_system_security(sid))
            await asyncio.gather(*tasks, return_exceptions=True)

        # Filter to high-sec only
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
        """
        Fetch all market orders for a region (handles pagination).
        Also captures the Expires header for sync timing.
        
        Cache layering (when force_refresh is False):
        - First: shared per-region cache (5 min freshness)
        - Then: legacy Jita-specific cache (if use_cache=True and Jita region)
        - Otherwise: fetch from ESI
        
        Concurrent calls for the same region are serialized via a per-region
        lock, so a second caller waits for the first's result instead of
        starting a duplicate fetch.
        
        Args:
            region_id: The region to fetch orders from
            use_cache: Legacy. If True and Jita region, allow Jita cache hit
                       (no staleness check). Mostly superseded by shared cache.
            force_refresh: If True, bypass all caches and fetch fresh from ESI.
                           Used by manual refresh buttons.
        """
        # === CACHE CHECK (skipped if force_refresh) ===
        if not force_refresh:
            cached = self.get_cached_orders(region_id)
            if cached is not None:
                # Feed cached region's expiry into earliest-wins so the
                # scanner countdown stays accurate when ESI was skipped.
                cache_entry = self._order_cache.get(region_id, {})
                self._apply_earliest_expires(cache_entry.get('expires'))
                return cached
            
            # Legacy Jita-specific cache fallback (no staleness check)
            if use_cache and region_id == JITA_REGION_ID and self.jita_orders_cache:
                return self.jita_orders_cache
        
        # === IN-FLIGHT GUARD ===
        # Per-region lock prevents duplicate concurrent fetches.
        # If another thread is already fetching this region, we block here.
        fetch_lock = self._get_region_fetch_lock(region_id)
        fetch_lock.acquire()
        try:
            # Re-check shared cache after acquiring the lock — another thread
            # may have populated it while we were waiting.
            if not force_refresh:
                cached = self.get_cached_orders(region_id)
                if cached is not None:
                    cache_entry = self._order_cache.get(region_id, {})
                    self._apply_earliest_expires(cache_entry.get('expires'))
                    return cached
            
            # === FETCH FROM ESI ===
            all_orders = []
            
            url = f"{ESI_BASE_URL}/markets/{region_id}/orders/"
            async with self.semaphore:
                async with self.session.get(url, params={"page": 1}) as response:
                    response.raise_for_status()
                    total_pages = int(response.headers.get("X-Pages", 1))
                    
                    # Capture expiry time for market data sync
                    expires = self._parse_expires_header(response)
                    self._apply_earliest_expires(expires)
                    
                    all_orders.extend(await response.json())
            
            if total_pages > 1:
                # Fetch remaining pages concurrently, handle 404s gracefully
                tasks = [
                    self._get_page_safe(f"/markets/{region_id}/orders/", p)
                    for p in range(2, total_pages + 1)
                ]
                results = await asyncio.gather(*tasks)
                for page_orders in results:
                    if page_orders:  # Skip None from 404s
                        all_orders.extend(page_orders)
            
            # === POPULATE SHARED CACHE (every fetch, every region) ===
            self.cache_orders_for_region(region_id, all_orders, expires=expires)
            
            # === LEGACY JITA CACHE (preserve existing behavior) ===
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
                return None  # Page vanished mid-fetch, skip it
            raise

    async def get_type_name(self, type_id: int) -> str:
        """Get item name from type ID, with SDE lookup and ESI fallback."""
        # Check in-memory cache first
        if type_id in self.type_name_cache:
            return self.type_name_cache[type_id]
        
        # Try SDE (instant local lookup)
        sde = _get_sde()
        if sde and sde.is_available():
            name = sde.get_type_name(type_id)
            if name:
                self.type_name_cache[type_id] = name
                return name
        
        # Fall back to ESI
        try:
            data = await self._get(f"/universe/types/{type_id}/")
            name = data.get("name", f"Unknown ({type_id})")
            self.type_name_cache[type_id] = name
            return name
        except Exception:
            return f"Unknown ({type_id})"

    async def get_type_names_bulk(self, type_ids: list[int]) -> dict[int, str]:
        """Fetch multiple type names with SDE lookup and ESI fallback."""
        result = {}
        uncached = []
        
        # Check in-memory cache first
        for tid in type_ids:
            if tid in self.type_name_cache:
                result[tid] = self.type_name_cache[tid]
            else:
                uncached.append(tid)
        
        if not uncached:
            return result
        
        # Try SDE for uncached items (instant local lookup)
        sde = _get_sde()
        if sde and sde.is_available():
            sde_names = sde.get_type_names_bulk(uncached)
            for tid, name in sde_names.items():
                result[tid] = name
                self.type_name_cache[tid] = name
            # Remove found items from uncached
            uncached = [tid for tid in uncached if tid not in sde_names]
        
        # Fall back to ESI for any remaining
        if uncached:
            await self._fetch_type_names_from_esi(uncached, result)
        
        # Fill in any still-missing with unknown
        for tid in type_ids:
            if tid not in result:
                result[tid] = f"Unknown ({tid})"
        
        return result

    async def _fetch_type_names_from_esi(self, type_ids: list[int], result: dict[int, str]):
        """Fetch type names from ESI in batches, updating result dict in place."""
        BATCH_SIZE = 500
        
        for i in range(0, len(type_ids), BATCH_SIZE):
            batch = type_ids[i:i + BATCH_SIZE]
            try:
                async with self.semaphore:
                    url = f"{ESI_BASE_URL}/universe/names/"
                    async with self.session.post(url, json=batch) as response:
                        if response.status == 200:
                            data = await response.json()
                            for item in data:
                                result[item["id"]] = item["name"]
                                self.type_name_cache[item["id"]] = item["name"]
                        else:
                            # Fall back to individual fetches
                            await self._fetch_type_names_individual(batch, result)
            except Exception:
                await self._fetch_type_names_individual(batch, result)

    async def _fetch_type_names_individual(self, type_ids: list[int], result: dict[int, str]):
        """Fetch type names individually (fallback for batch failures)."""
        tasks = [self.get_type_name(tid) for tid in type_ids]
        names = await asyncio.gather(*tasks, return_exceptions=True)
        for tid, name in zip(type_ids, names):
            if isinstance(name, str):
                result[tid] = name

    def get_system_security(self, system_id: int) -> float:
        """Get cached security status for a system."""
        return self.system_cache.get(system_id, {}).get("security", 0)

    def is_valid_system(self, system_id: int) -> bool:
        """Check if system is within range and high-sec."""
        return system_id in self.valid_systems

    async def get_market_history(self, region_id: int, type_id: int) -> list[dict]:
        """
        Fetch market history for a specific item in a region.
        Returns list of daily stats: average, date, highest, lowest, order_count, volume
        """
        try:
            return await self._get(f"/markets/{region_id}/history/", {"type_id": type_id})
        except Exception:
            return []

    async def get_market_history_bulk(self, region_id: int, type_ids: list[int], use_cache: bool = True) -> dict[int, list[dict]]:
        """
        Fetch market history for multiple items.
        
        Priority:
        1. Market history database (SQLite) - instant indexed lookups
        2. ESI supplement cache (for items missing from db)
        3. In-memory ESI cache
        4. ESI API calls for missing items
        
        Args:
            region_id: Region to fetch history from
            type_ids: List of type IDs to fetch
            use_cache: If True, return cached history for items we've already fetched
        
        Returns:
            Dict mapping type_id to history list.
        """
        # === FAST PATH: Use market history database if available ===
        if self.market_history is not None:
            # Query SQLite for 30 days of history
            result = self.market_history.get_history_bulk(region_id, type_ids, days=30)
            
            # Check if any items are missing from database (very new items)
            missing_from_db = [tid for tid in type_ids if not result.get(tid)]
            has_data = [tid for tid in type_ids if result.get(tid)]
            
            if DEBUG_ESI:
                print(f"[DIAG] Market history DB for region {region_id}:")
                print(f"[DIAG]   Requested: {len(type_ids)} items")
                print(f"[DIAG]   Have data: {len(has_data)} items")
                print(f"[DIAG]   Missing (empty): {len(missing_from_db)} items")
            
            if missing_from_db:
                # Check ESI supplement cache for missing items
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
                    # Filter out items we know will fail (2+ previous errors)
                    known_bad = [tid for tid in still_missing if self.supplement.is_known_bad(region_id, tid)]
                    to_fetch = [tid for tid in still_missing if tid not in known_bad]
                    
                    if known_bad and DEBUG_ESI:
                        print(f"[DIAG]   Skipping {len(known_bad)} known-bad items (2+ errors)")
                        # Return empty for known-bad items
                        for tid in known_bad:
                            result[tid] = []
                    
                    if to_fetch:
                        if DEBUG_ESI:
                            print(f"[DIAG]   Still need ESI fetch: {len(to_fetch)} items")
                            print(f"[DIAG]   Missing type_ids (first 20): {to_fetch[:20]}")
                        
                        # Fetch missing from ESI
                        esi_results, esi_errors = await self._fetch_history_from_esi(region_id, to_fetch, track_errors=True)
                        
                        # Count ESI results and store in supplement cache
                        esi_got_data = 0
                        esi_empty = 0
                        esi_error = 0
                        for tid in to_fetch:
                            if tid in esi_errors:
                                # Store error with attempt tracking
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
                            print(f"[DIAG]   ESI fallback: {esi_got_data} got data, {esi_empty} empty, {esi_error} errors")
            
            # Final count
            final_with_data = sum(1 for tid in type_ids if result.get(tid))
            final_empty = sum(1 for tid in type_ids if not result.get(tid))
            if DEBUG_ESI:
                print(f"[DIAG]   Final: {final_with_data} with data, {final_empty} empty")
            
            # Also store in history_cache so other components can access it
            if region_id not in self.history_cache:
                self.history_cache[region_id] = {}
            self.history_cache[region_id].update(result)
            
            return result
        
        # === FALLBACK: Original ESI-based approach ===
        # Ensure region exists in cache
        if region_id not in self.history_cache:
            self.history_cache[region_id] = {}
        
        region_cache = self.history_cache[region_id]
        
        # Determine which type_ids need fetching
        if use_cache:
            uncached = [tid for tid in type_ids if tid not in region_cache]
        else:
            uncached = list(type_ids)
        
        # Log cache efficiency
        if type_ids and DEBUG_ESI:
            cached_count = len(type_ids) - len(uncached)
            if uncached:
                print(f"[DIAG] History cache: {cached_count}/{len(type_ids)} cached, fetching {len(uncached)} from ESI")
            else:
                print(f"[DIAG] History cache: {cached_count}/{len(type_ids)} cached, no ESI calls needed")
        
        # Fetch uncached items from ESI
        if uncached:
            esi_results, _ = await self._fetch_history_from_esi(region_id, uncached, track_errors=False)
            region_cache.update(esi_results)
        
        # Return all requested items from cache
        return {tid: region_cache.get(tid, []) for tid in type_ids}

    async def _fetch_history_from_esi(self, region_id: int, type_ids: list[int], 
                                       track_errors: bool = False) -> tuple[dict[int, list[dict]], set[int]]:
        """
        Fetch history for multiple items from ESI API.
        
        Args:
            region_id: Region to fetch from
            type_ids: Items to fetch
            track_errors: If True, return error set separately instead of putting [] in results
        
        Returns:
            (results_dict, error_set): Results for fetches, set of type_ids that errored
        """
        result = {}
        errors = set()
        
        # Track for diagnostics
        success_count = 0
        error_count = 0
        empty_count = 0
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
            print(f"[DIAG] ESI fetch for {len(type_ids)} items: {success_count} success, {empty_count} empty, {error_count} errors")
            if errors_seen:
                print(f"[DIAG]   Error types: {errors_seen}")
        
        return result, errors

    async def _get_market_history_raw(self, region_id: int, type_id: int) -> list[dict]:
        """Fetch market history with diagnostic info preserved (exceptions not caught)."""
        return await self._get(f"/markets/{region_id}/history/", {"type_id": type_id})

    async def search_item_by_name(self, search_term: str) -> list[dict]:
        """
        Search for items by name using ESI universe/ids endpoint (no auth needed).
        This does exact matching, so we also search our local cache for partial matches.
        Returns list of {type_id, name} dicts.
        """
        results = []
        
        # First, try exact match via ESI universe/ids (no auth required)
        try:
            async with self.semaphore:
                url = f"{ESI_BASE_URL}/universe/ids/"
                async with self.session.post(url, json=[search_term]) as response:
                    if response.status == 200:
                        data = await response.json()
                        inv_types = data.get("inventory_types", [])
                        for item in inv_types:
                            results.append({
                                "type_id": item["id"],
                                "name": item["name"]
                            })
                            # Cache it
                            self.type_name_cache[item["id"]] = item["name"]
        except Exception as e:
            if DEBUG_ESI:
                print(f"[ESI] universe/ids error: {e}")
        
        # Also search local cache for partial matches
        search_lower = search_term.lower()
        for type_id, name in self.type_name_cache.items():
            if search_lower in name.lower():
                # Don't add duplicates
                if not any(r["type_id"] == type_id for r in results):
                    results.append({"type_id": type_id, "name": name})
        
        # Sort by name, prioritize exact matches
        def sort_key(item):
            name_lower = item["name"].lower()
            if name_lower == search_lower:
                return (0, name_lower)  # Exact match first
            elif name_lower.startswith(search_lower):
                return (1, name_lower)  # Starts with second
            else:
                return (2, name_lower)  # Contains third
        
        results.sort(key=sort_key)
        return results[:20]

    def search_cached_items(self, search_term: str) -> list[dict]:
        """
        Search local type_name_cache for items (instant, no API call).
        Returns list of {type_id, name} dicts.
        """
        search_lower = search_term.lower()
        results = []
        
        for type_id, name in self.type_name_cache.items():
            if search_lower in name.lower():
                results.append({"type_id": type_id, "name": name})
        
        # Sort by name and limit
        results.sort(key=lambda x: x["name"])
        return results[:20]
