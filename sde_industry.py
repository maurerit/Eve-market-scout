"""SDE Industry data manager for EVE Market Scout.

Downloads and caches blueprint manufacturing data from Fuzzwork SDE.
Provides lookups for item -> blueprint -> materials relationships.

Data source: Fuzzwork's SDE CSV exports
    https://www.fuzzwork.co.uk/dump/latest/industryActivityMaterials.csv
    https://www.fuzzwork.co.uk/dump/latest/industryActivityProducts.csv

Database location: %APPDATA%/EVEMarketScout/sde_industry.db
"""

import csv
import io
import json
import sqlite3
import asyncio
import aiohttp
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any, Callable
from dataclasses import dataclass

from sound_manager import get_data_dir
from ssl_context import make_connector


# Database and version files
INDUSTRY_DB_FILE = "sde_industry.db"
INDUSTRY_VERSION_FILE = "sde_industry_version.json"

# Fuzzwork URLs
# Fuzzwork moved the CSV exports into a csv/ subdirectory (mid-2026); the old
# /dump/latest/*.csv paths now 404.
FUZZWORK_BASE = "https://www.fuzzwork.co.uk/dump/latest/csv"
MATERIALS_URL = f"{FUZZWORK_BASE}/industryActivityMaterials.csv"
PRODUCTS_URL = f"{FUZZWORK_BASE}/industryActivityProducts.csv"

# Activity ID for manufacturing
ACTIVITY_MANUFACTURING = 1


@dataclass
class BlueprintMaterial:
    """A single material requirement for a blueprint."""
    type_id: int
    quantity: int


class SDEIndustryDB:
    """Manages SDE industry data for blueprint/material lookups."""
    
    def __init__(self):
        self.data_dir = get_data_dir()
        self.db_path = self.data_dir / INDUSTRY_DB_FILE
        self.version_path = self.data_dir / INDUSTRY_VERSION_FILE
        
        # Caches
        self._product_to_blueprint: Dict[int, int] = {}
        self._blueprint_materials: Dict[int, List[BlueprintMaterial]] = {}
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get database connection."""
        if not self.db_path.exists():
            raise FileNotFoundError(f"Industry database not found: {self.db_path}")
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def is_available(self) -> bool:
        """Check if database exists and is usable."""
        if not self.db_path.exists():
            return False
        try:
            conn = self._get_conn()
            cursor = conn.execute("SELECT COUNT(*) FROM industry_materials LIMIT 1")
            count = cursor.fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False
    
    def get_version_info(self) -> Dict[str, Any]:
        """Get version info (download date, record counts)."""
        if not self.version_path.exists():
            return {}
        try:
            with open(self.version_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    
    def get_age_days(self) -> Optional[int]:
        """Get age of data in days."""
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
    # Lookup Methods
    # =========================================================================
    
    def get_blueprint_for_item(self, type_id: int) -> Optional[int]:
        """Get the blueprint ID that produces this item.
        
        Args:
            type_id: The product type ID
            
        Returns:
            Blueprint type ID, or None if no blueprint (faction/officer/etc)
        """
        # Check cache
        if type_id in self._product_to_blueprint:
            return self._product_to_blueprint[type_id]
        
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT blueprint_id FROM industry_products WHERE product_type_id = ?",
                (type_id,)
            )
            row = cursor.fetchone()
            conn.close()
            
            if row:
                bp_id = row["blueprint_id"]
                self._product_to_blueprint[type_id] = bp_id
                return bp_id
            
            # Cache miss (no blueprint)
            self._product_to_blueprint[type_id] = None
            return None
            
        except Exception as e:
            print(f"[SDEIndustry] Error looking up blueprint for {type_id}: {e}")
            return None
    
    def get_materials(self, blueprint_id: int) -> List[BlueprintMaterial]:
        """Get materials required for a blueprint.
        
        Args:
            blueprint_id: Blueprint type ID
            
        Returns:
            List of BlueprintMaterial (type_id, quantity)
        """
        # Check cache
        if blueprint_id in self._blueprint_materials:
            return self._blueprint_materials[blueprint_id]
        
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT material_type_id, quantity FROM industry_materials WHERE blueprint_id = ?",
                (blueprint_id,)
            )
            
            materials = []
            for row in cursor:
                materials.append(BlueprintMaterial(
                    type_id=row["material_type_id"],
                    quantity=row["quantity"]
                ))
            conn.close()
            
            self._blueprint_materials[blueprint_id] = materials
            return materials
            
        except Exception as e:
            print(f"[SDEIndustry] Error looking up materials for blueprint {blueprint_id}: {e}")
            return []
    
    def get_materials_for_item(self, type_id: int) -> Optional[List[BlueprintMaterial]]:
        """Convenience method: get materials for an item by its type_id.
        
        Args:
            type_id: Product type ID
            
        Returns:
            List of BlueprintMaterial, or None if no blueprint exists
        """
        blueprint_id = self.get_blueprint_for_item(type_id)
        if blueprint_id is None:
            return None
        return self.get_materials(blueprint_id)
    
    def get_all_manufacturable_items(self) -> List[int]:
        """Get list of all type_ids that have blueprints (can be manufactured)."""
        try:
            conn = self._get_conn()
            cursor = conn.execute("SELECT DISTINCT product_type_id FROM industry_products")
            result = [row[0] for row in cursor.fetchall()]
            conn.close()
            return result
        except Exception:
            return []
    
    # =========================================================================
    # Download and Build
    # =========================================================================
    
    async def download_and_build(
        self,
        progress_callback: Optional[Callable[[str, int], None]] = None
    ) -> bool:
        """Download SDE industry data and build database.
        
        Args:
            progress_callback: Optional callback(status_message, percent)
            
        Returns:
            True if successful
        """
        def update(msg: str, pct: int):
            print(f"[SDEIndustry] {msg}")
            if progress_callback:
                progress_callback(msg, pct)
        
        update("Starting industry data download...", 0)
        
        # Clear caches
        self._product_to_blueprint.clear()
        self._blueprint_materials.clear()
        
        # Remove old database
        if self.db_path.exists():
            try:
                self.db_path.unlink()
            except Exception as e:
                update(f"Failed to remove old database: {e}", 0)
                return False
        
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(connector=make_connector(), timeout=timeout) as session:
                
                # Download materials CSV
                update("Downloading materials data...", 5)
                materials_data = await self._download_csv(session, MATERIALS_URL, update, 5, 30)
                if materials_data is None:
                    return False
                
                # Download products CSV
                update("Downloading products data...", 35)
                products_data = await self._download_csv(session, PRODUCTS_URL, update, 35, 50)
                if products_data is None:
                    return False
            
            # Build database
            update("Building database...", 55)
            materials_count, products_count = self._build_database(
                materials_data, products_data, update
            )
            
            # Save version info
            version_info = {
                "download_date": datetime.now().isoformat(),
                "source": "fuzzwork",
                "materials_count": materials_count,
                "products_count": products_count,
            }
            with open(self.version_path, "w") as f:
                json.dump(version_info, f, indent=2)
            
            update(f"Complete: {materials_count:,} materials, {products_count:,} products", 100)
            return True
            
        except Exception as e:
            update(f"Error: {e}", 0)
            if self.db_path.exists():
                try:
                    self.db_path.unlink()
                except Exception:
                    pass
            return False
    
    async def _download_csv(
        self,
        session: aiohttp.ClientSession,
        url: str,
        update: Callable,
        start_pct: int,
        end_pct: int
    ) -> Optional[str]:
        """Download a CSV file."""
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    update(f"Download failed: HTTP {response.status}", start_pct)
                    return None
                
                total_size = int(response.headers.get("content-length", 0))
                downloaded = 0
                chunks = []
                
                async for chunk in response.content.iter_chunked(65536):
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        pct = int(start_pct + (downloaded / total_size) * (end_pct - start_pct))
                        update(f"Downloading... {downloaded // 1024}KB", pct)
                
                # utf-8-sig: the current Fuzzwork CSVs start with a BOM,
                # which would otherwise corrupt the first column name.
                return b"".join(chunks).decode("utf-8-sig")
                
        except Exception as e:
            update(f"Download error: {e}", start_pct)
            return None
    
    def _build_database(
        self,
        materials_csv: str,
        products_csv: str,
        update: Callable
    ) -> Tuple[int, int]:
        """Build SQLite database from CSV data."""
        conn = sqlite3.connect(str(self.db_path))
        
        # Create tables
        conn.execute("""
            CREATE TABLE industry_materials (
                blueprint_id INTEGER NOT NULL,
                material_type_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                PRIMARY KEY (blueprint_id, material_type_id)
            )
        """)
        
        conn.execute("""
            CREATE TABLE industry_products (
                blueprint_id INTEGER NOT NULL,
                product_type_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                PRIMARY KEY (blueprint_id, product_type_id)
            )
        """)
        
        # Index for reverse lookup (item -> blueprint)
        conn.execute(
            "CREATE INDEX idx_product_type ON industry_products(product_type_id)"
        )
        
        # Parse and insert materials
        # Columns: typeID,activityID,materialTypeID,quantity
        update("Importing materials...", 60)
        materials_count = 0
        reader = csv.DictReader(io.StringIO(materials_csv))
        batch = []
        
        for row in reader:
            try:
                activity_id = int(row["activityID"])
                if activity_id != ACTIVITY_MANUFACTURING:
                    continue
                
                batch.append((
                    int(row["typeID"]),          # blueprint_id
                    int(row["materialTypeID"]),
                    int(row["quantity"])
                ))
                materials_count += 1
                
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO industry_materials VALUES (?, ?, ?)",
                        batch
                    )
                    batch = []
                    update(f"Materials: {materials_count:,}...", 65)
                    
            except (ValueError, KeyError):
                continue
        
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_materials VALUES (?, ?, ?)",
                batch
            )
        
        # Parse and insert products
        # Columns: typeID,activityID,productTypeID,quantity
        update("Importing products...", 80)
        products_count = 0
        reader = csv.DictReader(io.StringIO(products_csv))
        batch = []
        
        for row in reader:
            try:
                activity_id = int(row["activityID"])
                if activity_id != ACTIVITY_MANUFACTURING:
                    continue
                
                batch.append((
                    int(row["typeID"]),           # blueprint_id
                    int(row["productTypeID"]),
                    int(row["quantity"])
                ))
                products_count += 1
                
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO industry_products VALUES (?, ?, ?)",
                        batch
                    )
                    batch = []
                    update(f"Products: {products_count:,}...", 85)
                    
            except (ValueError, KeyError):
                continue
        
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_products VALUES (?, ?, ?)",
                batch
            )
        
        conn.commit()
        conn.close()
        
        return materials_count, products_count
    
    def refresh(self, progress_callback: Optional[Callable[[str, int], None]] = None) -> bool:
        """Synchronous wrapper for download_and_build.
        
        For use with GUI thread callbacks.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.download_and_build(progress_callback))
        finally:
            loop.close()


# =============================================================================
# Module-level singleton
# =============================================================================

_instance: Optional[SDEIndustryDB] = None


def get_sde_industry_db() -> SDEIndustryDB:
    """Get or create the singleton SDEIndustryDB instance."""
    global _instance
    if _instance is None:
        _instance = SDEIndustryDB()
    return _instance


def refresh_sde_industry(progress_callback: Optional[Callable[[str, int], None]] = None) -> bool:
    """Force refresh of industry data.
    
    Convenience function for GUI button callbacks.
    """
    db = get_sde_industry_db()
    return db.refresh(progress_callback)
