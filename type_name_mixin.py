"""Type name lookup and item search mixin for ESIClient."""

import asyncio
import aiohttp
from config import ESI_BASE_URL

DEBUG_ESI = False

_sde_manager = None


def _get_sde():
    """Get SDE manager instance (lazy load)."""
    global _sde_manager
    if _sde_manager is None:
        try:
            from sde_manager import get_sde_manager
            _sde_manager = get_sde_manager()
        except ImportError:
            _sde_manager = False
    return _sde_manager if _sde_manager else None


class TypeNameMixin:
    """Type name lookup and item search for ESIClient.

    Expects self.session, self.semaphore, self.type_name_cache from ESIClient.
    """

    async def get_type_name(self, type_id: int) -> str:
        """Get item name from type ID, with SDE lookup and ESI fallback."""
        if type_id in self.type_name_cache:
            return self.type_name_cache[type_id]

        sde = _get_sde()
        if sde and sde.is_available():
            name = sde.get_type_name(type_id)
            if name:
                self.type_name_cache[type_id] = name
                return name

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

        for tid in type_ids:
            if tid in self.type_name_cache:
                result[tid] = self.type_name_cache[tid]
            else:
                uncached.append(tid)

        if not uncached:
            return result

        sde = _get_sde()
        if sde and sde.is_available():
            sde_names = sde.get_type_names_bulk(uncached)
            for tid, name in sde_names.items():
                result[tid] = name
                self.type_name_cache[tid] = name
            uncached = [tid for tid in uncached if tid not in sde_names]

        if uncached:
            await self._fetch_type_names_from_esi(uncached, result)

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

    async def search_item_by_name(self, search_term: str) -> list[dict]:
        """Search for items by name via ESI universe/ids, then local cache for partials.

        Returns list of {type_id, name} dicts, up to 20 results.
        """
        results = []

        try:
            async with self.semaphore:
                url = f"{ESI_BASE_URL}/universe/ids/"
                async with self.session.post(url, json=[search_term]) as response:
                    if response.status == 200:
                        data = await response.json()
                        for item in data.get("inventory_types", []):
                            results.append({"type_id": item["id"], "name": item["name"]})
                            self.type_name_cache[item["id"]] = item["name"]
        except Exception as e:
            if DEBUG_ESI:
                print(f"[ESI] universe/ids error: {e}")

        search_lower = search_term.lower()
        for type_id, name in self.type_name_cache.items():
            if search_lower in name.lower():
                if not any(r["type_id"] == type_id for r in results):
                    results.append({"type_id": type_id, "name": name})

        def sort_key(item):
            name_lower = item["name"].lower()
            if name_lower == search_lower:
                return (0, name_lower)
            elif name_lower.startswith(search_lower):
                return (1, name_lower)
            else:
                return (2, name_lower)

        results.sort(key=sort_key)
        return results[:20]

    # =========================================================================
    # Station search (for AddStationDialog)
    # =========================================================================

    async def search_station_by_name(self, search_term: str) -> list[dict]:
        """Search ESI for NPC stations matching search_term.

        Returns list of dicts with keys:
            station_id, name, system_id, system_name, region_id, region_name, corp_id
        Limited to 10 results.
        """
        station_ids: list[int] = []
        try:
            async with self.semaphore:
                url = f"{ESI_BASE_URL}/search/"
                params = {"categories": "station", "search": search_term, "strict": "false"}
                async with self.session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        station_ids = data.get("station", [])[:10]
        except Exception as e:
            if DEBUG_ESI:
                print(f"[ESI] station search error: {e}")

        if not station_ids:
            return []

        tasks = [self._get_station_info(sid) for sid in station_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, dict)]

    async def _get_station_info(self, station_id: int) -> dict:
        """Fetch station + system + region info for a single station."""
        if not hasattr(self, "_region_name_cache"):
            self._region_name_cache: dict[int, str] = {}

        try:
            station_data = await self._get(f"/universe/stations/{station_id}/")
            system_id = station_data.get("system_id")
            corp_id = station_data.get("owner")
            name = station_data.get("name", f"Station {station_id}")

            system_name = f"System {system_id}"
            region_id = None
            if system_id:
                try:
                    sys_data = await self._get(f"/universe/systems/{system_id}/")
                    system_name = sys_data.get("name", system_name)
                    constellation_id = sys_data.get("constellation_id")
                    if constellation_id:
                        const_data = await self._get(
                            f"/universe/constellations/{constellation_id}/"
                        )
                        region_id = const_data.get("region_id")
                except Exception:
                    pass

            region_name = str(region_id) if region_id else "Unknown"
            if region_id and region_id not in self._region_name_cache:
                try:
                    reg_data = await self._get(f"/universe/regions/{region_id}/")
                    self._region_name_cache[region_id] = reg_data.get(
                        "name", str(region_id)
                    )
                except Exception:
                    self._region_name_cache[region_id] = str(region_id)
            if region_id:
                region_name = self._region_name_cache.get(region_id, str(region_id))

            return {
                "station_id": station_id,
                "name": name,
                "system_id": system_id,
                "system_name": system_name,
                "region_id": region_id,
                "region_name": region_name,
                "corp_id": corp_id,
            }
        except Exception as e:
            if DEBUG_ESI:
                print(f"[ESI] _get_station_info({station_id}) error: {e}")
            return {}

    def search_cached_items(self, search_term: str) -> list[dict]:
        """Search local type_name_cache for items (instant, no API call).

        Returns list of {type_id, name} dicts, up to 20 results.
        """
        search_lower = search_term.lower()
        results = [
            {"type_id": type_id, "name": name}
            for type_id, name in self.type_name_cache.items()
            if search_lower in name.lower()
        ]
        results.sort(key=lambda x: x["name"])
        return results[:20]
