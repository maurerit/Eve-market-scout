"""SDE (Static Data Export) manager for EVE Market Scout.

Provides local SQLite database of item types for instant lookups.
Eliminates ESI API calls for type names and enables future features
like cargo volume filtering and market group categorization.

Data source: Fuzzwork's SDE SQLite conversion
https://www.fuzzwork.co.uk/dump/latest/

Database location: %APPDATA%/EVEMarketScout/sde_types.db (Windows)
"""

import os
import sqlite3
import json
import asyncio
import aiohttp
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass

from sound_manager import get_data_dir
from ssl_context import make_connector


# File locations
SDE_DB_FILE = "sde_types.db"
SDE_VERSION_FILE = "sde_version.json"

# Fuzzwork SDE downloads
# invTypes table has everything we need
FUZZWORK_BASE = "https://www.fuzzwork.co.uk/dump/latest"
FUZZWORK_TYPES_URL = f"{FUZZWORK_BASE}/invTypes.csv.bz2"

# How old before we suggest updating (days)
SDE_STALE_DAYS = 30


@dataclass
class TypeInfo:
    """Full type information from SDE."""
    type_id: int
    name: str
    volume: float  # Packaged volume in m3
    market_group_id: Optional[int]
    published: bool
    portion_size: int
    group_id: Optional[int] = None  # Item group (e.g., "Frigate", "Shield Hardener")


class SDEManager:
    """Manages local SDE database for type lookups."""
    
    def __init__(self):
        self.data_dir = get_data_dir()
        self.db_path = self.data_dir / SDE_DB_FILE
        self.version_path = self.data_dir / SDE_VERSION_FILE
        
        # In-memory cache for hot lookups
        self._name_cache: Dict[int, str] = {}
        self._info_cache: Dict[int, TypeInfo] = {}
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get database connection. Creates new connection each call for thread safety."""
        if not self.db_path.exists():
            raise FileNotFoundError(f"SDE database not found: {self.db_path}")
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def close(self):
        """Close database connection (legacy - now no-op since connections are per-call)."""
        pass
    
    def is_available(self) -> bool:
        """Check if SDE database exists and is usable."""
        if not self.db_path.exists():
            return False
        try:
            conn = self._get_conn()
            cursor = conn.execute("SELECT COUNT(*) FROM types LIMIT 1")
            count = cursor.fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False
    
    def get_version_info(self) -> Dict[str, Any]:
        """Get SDE version info (download date, record count, etc)."""
        if not self.version_path.exists():
            return {}
        try:
            with open(self.version_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    
    def is_stale(self) -> bool:
        """Check if SDE is older than threshold."""
        info = self.get_version_info()
        if not info:
            return True
        
        download_date = info.get("download_date")
        if not download_date:
            return True
        
        try:
            downloaded = datetime.fromisoformat(download_date)
            age_days = (datetime.now() - downloaded).days
            return age_days > SDE_STALE_DAYS
        except Exception:
            return True
    
    def get_age_days(self) -> Optional[int]:
        """Get age of SDE in days, or None if unknown."""
        info = self.get_version_info()
        download_date = info.get("download_date")
        if not download_date:
            return None
        try:
            downloaded = datetime.fromisoformat(download_date)
            return (datetime.now() - downloaded).days
        except Exception:
            return None
    
    # =========================================================================
    # LOOKUP METHODS
    # =========================================================================
    
    def get_type_name(self, type_id: int) -> Optional[str]:
        """
        Get item name by type ID.
        
        Returns None if not found (caller should fall back to ESI).
        """
        # Check cache first
        if type_id in self._name_cache:
            return self._name_cache[type_id]
        
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT name FROM types WHERE type_id = ?",
                (type_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                name = row["name"]
                self._name_cache[type_id] = name
                return name
            return None
        except Exception:
            return None
    
    def get_type_names_bulk(self, type_ids: list[int]) -> Dict[int, str]:
        """
        Get names for multiple type IDs.
        
        Returns dict of {type_id: name}.
        Missing IDs are not included in result.
        """
        result = {}
        uncached = []
        
        # Check cache first
        for tid in type_ids:
            if tid in self._name_cache:
                result[tid] = self._name_cache[tid]
            else:
                uncached.append(tid)
        
        if not uncached:
            return result
        
        try:
            conn = self._get_conn()
            # SQLite has a limit on placeholders, batch if needed
            BATCH_SIZE = 500
            for i in range(0, len(uncached), BATCH_SIZE):
                batch = uncached[i:i + BATCH_SIZE]
                placeholders = ",".join("?" * len(batch))
                cursor = conn.execute(
                    f"SELECT type_id, name FROM types WHERE type_id IN ({placeholders})",
                    batch
                )
                for row in cursor:
                    tid = row["type_id"]
                    name = row["name"]
                    result[tid] = name
                    self._name_cache[tid] = name
            conn.close()
        except Exception:
            pass
        
        return result
    
    def get_type_info(self, type_id: int) -> Optional[TypeInfo]:
        """
        Get full type information.
        
        Returns None if not found.
        """
        # Check cache
        if type_id in self._info_cache:
            return self._info_cache[type_id]
        
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                """SELECT type_id, name, volume, market_group_id, 
                          published, portion_size, group_id
                   FROM types WHERE type_id = ?""",
                (type_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                info = TypeInfo(
                    type_id=row["type_id"],
                    name=row["name"],
                    volume=row["volume"] or 0.0,
                    market_group_id=row["market_group_id"],
                    published=bool(row["published"]),
                    portion_size=row["portion_size"] or 1,
                    group_id=row["group_id"]
                )
                self._info_cache[type_id] = info
                return info
            return None
        except Exception:
            return None
    
    def get_type_volume(self, type_id: int) -> Optional[float]:
        """Get packaged volume in m3 for a type."""
        info = self.get_type_info(type_id)
        return info.volume if info else None
    
    def get_types_by_market_group(self, market_group_id: int) -> list[TypeInfo]:
        """Get all types in a market group (for future filtering)."""
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                """SELECT type_id, name, volume, market_group_id,
                          published, portion_size, group_id
                   FROM types WHERE market_group_id = ? AND published = 1""",
                (market_group_id,)
            )
            results = []
            for row in cursor:
                results.append(TypeInfo(
                    type_id=row["type_id"],
                    name=row["name"],
                    volume=row["volume"] or 0.0,
                    market_group_id=row["market_group_id"],
                    published=bool(row["published"]),
                    portion_size=row["portion_size"] or 1,
                    group_id=row["group_id"]
                ))
            conn.close()
            return results
        except Exception:
            return []
    
    # =========================================================================
    # DOWNLOAD AND INITIALIZATION
    # =========================================================================
    
    async def download_and_build(
        self,
        progress_callback: Optional[callable] = None
    ) -> bool:
        """
        Download SDE data and build local database.
        
        Args:
            progress_callback: Optional callback(status_message, percent_complete)
            
        Returns:
            True if successful, False on error.
        """
        import bz2
        import csv
        import io
        
        def update(msg: str, pct: int):
            print(f"[SDE] {msg}")
            if progress_callback:
                progress_callback(msg, pct)
        
        update("Starting SDE download...", 0)
        
        # Close existing connection
        self.close()
        
        # Remove old database if exists
        if self.db_path.exists():
            try:
                self.db_path.unlink()
            except Exception as e:
                update(f"Failed to remove old database: {e}", 0)
                return False
        
        try:
            # Download invTypes.csv.bz2
            update("Downloading type data from Fuzzwork...", 5)
            
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(connector=make_connector(), timeout=timeout) as session:
                async with session.get(FUZZWORK_TYPES_URL) as response:
                    if response.status != 200:
                        update(f"Download failed: HTTP {response.status}", 0)
                        return False
                    
                    total_size = int(response.headers.get("content-length", 0))
                    downloaded = 0
                    chunks = []
                    
                    async for chunk in response.content.iter_chunked(65536):
                        chunks.append(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            pct = int(5 + (downloaded / total_size) * 40)
                            update(f"Downloading... {downloaded // 1024}KB", pct)
                    
                    compressed_data = b"".join(chunks)
            
            update("Decompressing...", 50)
            csv_data = bz2.decompress(compressed_data).decode("utf-8")
            
            update("Building database...", 55)
            
            # Create database
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                CREATE TABLE types (
                    type_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    volume REAL,
                    market_group_id INTEGER,
                    published INTEGER,
                    portion_size INTEGER,
                    group_id INTEGER
                )
            """)
            conn.execute("CREATE INDEX idx_market_group ON types(market_group_id)")
            conn.execute("CREATE INDEX idx_published ON types(published)")
            conn.execute("CREATE INDEX idx_group ON types(group_id)")
            
            # Parse CSV
            # Fuzzwork invTypes columns:
            # typeID,groupID,typeName,description,mass,volume,capacity,portionSize,
            # raceID,basePrice,published,marketGroupID,iconID,soundID,graphicID
            reader = csv.DictReader(io.StringIO(csv_data))
            
            update("Importing types...", 60)
            
            records = []
            count = 0
            for row in reader:
                try:
                    type_id = int(row["typeID"])
                    name = row["typeName"]
                    volume = float(row["volume"]) if row["volume"] else 0.0
                    market_group = int(row["marketGroupID"]) if row["marketGroupID"] else None
                    published = int(row["published"]) if row["published"] else 0
                    portion_size = int(row["portionSize"]) if row["portionSize"] else 1
                    group_id = int(row["groupID"]) if row["groupID"] else None
                    
                    records.append((
                        type_id, name, volume, market_group,
                        published, portion_size, group_id
                    ))
                    count += 1
                    
                    # Batch insert
                    if len(records) >= 5000:
                        conn.executemany(
                            """INSERT INTO types 
                               (type_id, name, volume, market_group_id, 
                                published, portion_size, group_id)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            records
                        )
                        records = []
                        pct = int(60 + (count / 50000) * 30)  # Estimate ~47k types
                        update(f"Imported {count:,} types...", min(pct, 90))
                except (ValueError, KeyError):
                    continue
            
            # Insert remaining
            if records:
                conn.executemany(
                    """INSERT INTO types 
                       (type_id, name, volume, market_group_id,
                        published, portion_size, group_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    records
                )
            
            conn.commit()
            conn.close()
            
            # Save version info
            version_info = {
                "download_date": datetime.now().isoformat(),
                "source": "fuzzwork",
                "record_count": count,
                "sde_version": date.today().isoformat()
            }
            with open(self.version_path, "w") as f:
                json.dump(version_info, f, indent=2)
            
            # Clear caches
            self._name_cache.clear()
            self._info_cache.clear()
            self._conn = None
            
            update(f"SDE loaded: {count:,} types", 100)
            return True
            
        except Exception as e:
            update(f"Error: {e}", 0)
            # Clean up partial database
            if self.db_path.exists():
                try:
                    self.db_path.unlink()
                except Exception:
                    pass
            return False


# Global instance for easy access
_sde_instance: Optional[SDEManager] = None


def get_sde_manager() -> SDEManager:
    """Get the global SDE manager instance."""
    global _sde_instance
    if _sde_instance is None:
        _sde_instance = SDEManager()
    return _sde_instance
