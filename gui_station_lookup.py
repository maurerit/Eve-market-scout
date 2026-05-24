"""Persistent station -> owner-corp / faction / system lookup.

Used by the NPC Orders max-buy calc to figure out which corp owns the
station of a buy order, so the user's standings against that corp/faction
can be plugged into the sales-tax formula.

Resolution order:
  1. Built-in TRADE_HUBS (no ESI call needed; corp_id + faction_id known).
  2. ESI `/universe/stations/{station_id}/` -> owner (corp_id), system_id.
     Then ESI `/corporations/{corp_id}/` -> faction_id (may be None for
     player corps; tolerated downstream).
  3. Player structures (station_id >= 1e12): returned as None -- NPC sales
     tax doesn't apply the same way; caller should fall back to default.

Cache lives in `station_info_cache.json` in the data dir. Persisted across
sessions because station ownership effectively never changes.
"""

import json
import os
from typing import Optional, TYPE_CHECKING

from sound_manager import get_data_dir

if TYPE_CHECKING:
    import aiohttp


PLAYER_STRUCTURE_ID_THRESHOLD = 1_000_000_000_000


def _cache_path() -> str:
    return str(get_data_dir() / "station_info_cache.json")


class StationLookup:
    """Singleton-ish station_id -> info cache."""

    _instance: "Optional[StationLookup]" = None

    @classmethod
    def singleton(cls) -> "StationLookup":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # station_id -> {"corp_id", "faction_id", "system_id", "name"}
        self._data: dict[int, dict] = {}
        # corp_id -> faction_id (or None)
        self._corp_faction: dict[int, Optional[int]] = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        path = _cache_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for sid_str, info in raw.get("stations", {}).items():
                    self._data[int(sid_str)] = info
                for cid_str, fid in raw.get("corp_faction", {}).items():
                    self._corp_faction[int(cid_str)] = fid
            except Exception as e:
                print(f"[StationLookup] load error: {e}")
        self._loaded = True

    def _save(self):
        path = _cache_path()
        try:
            payload = {
                "stations": {str(sid): info for sid, info in self._data.items()},
                "corp_faction": {str(cid): fid for cid, fid in self._corp_faction.items()},
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            print(f"[StationLookup] save error: {e}")

    def lookup(self, station_id: int) -> Optional[dict]:
        """Synchronous cached lookup. Returns None if not yet resolved."""
        self._load()
        # Built-in hubs first -- avoids ever hitting ESI for the common case.
        from config import TRADE_HUBS
        for cfg in TRADE_HUBS.values():
            if cfg.get("station_id") == station_id:
                return {
                    "corp_id": cfg.get("corp_id"),
                    "faction_id": cfg.get("faction_id"),
                    "system_id": cfg.get("system_id"),
                    "name": cfg.get("name"),
                }
        if station_id >= PLAYER_STRUCTURE_ID_THRESHOLD:
            return None
        return self._data.get(station_id)

    async def fetch(self, session: "aiohttp.ClientSession",
                    station_id: int) -> Optional[dict]:
        """Resolve via cache or ESI. Returns None for player structures or on
        error. Idempotent: subsequent calls return the cached entry.
        """
        cached = self.lookup(station_id)
        if cached is not None:
            return cached
        if station_id >= PLAYER_STRUCTURE_ID_THRESHOLD:
            return None

        # /universe/stations/{station_id}/ -> owner (corp_id), system_id, name
        url = f"https://esi.evetech.net/latest/universe/stations/{station_id}/"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                sta = await resp.json()
        except Exception as e:
            print(f"[StationLookup] station fetch error {station_id}: {e}")
            return None

        corp_id = sta.get("owner")
        system_id = sta.get("system_id")
        name = sta.get("name")
        faction_id = await self._fetch_corp_faction(session, corp_id) if corp_id else None

        info = {
            "corp_id": corp_id,
            "faction_id": faction_id,
            "system_id": system_id,
            "name": name,
        }
        self._data[station_id] = info
        self._save()
        return info

    async def _fetch_corp_faction(self, session: "aiohttp.ClientSession",
                                   corp_id: int) -> Optional[int]:
        if corp_id in self._corp_faction:
            return self._corp_faction[corp_id]
        url = f"https://esi.evetech.net/latest/corporations/{corp_id}/"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    self._corp_faction[corp_id] = None
                    return None
                corp = await resp.json()
        except Exception as e:
            print(f"[StationLookup] corp fetch error {corp_id}: {e}")
            self._corp_faction[corp_id] = None
            return None
        faction_id = corp.get("faction_id")  # may be absent for player corps
        self._corp_faction[corp_id] = faction_id
        # Saved by the caller after the station_info upsert.
        return faction_id
