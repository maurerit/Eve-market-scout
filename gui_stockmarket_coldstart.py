"""Stock Market cold-start orchestrator for EVE Market Scout.

Single dedicated worker thread + asyncio loop that drives the
Stock Market tab through 7 sequential phases on launch:

    0. Detect      — what's missing
    1. Archive     — observe background_import (everef CSVs)
    2. DB import   — observe background_import (CSV -> market_history.db)
    3. Profiles    — extract per-region from market_history.db
    4. ESI burst   — pull stale hub orders from ESI
    5. MF          — material filter per hub (24h gate)
    6. LI          — leading indicators per hub (24h gate)
    7. Unlock      — hide locked overlay

Scanner runs untouched on its own thread/loop.  Step 1's per-loop
semaphore (api.ESIClient._per_loop) keeps the two threads from
stomping each other.

This file currently implements only phase 0 (detection + logging).
It runs alongside the existing _startup_refresh path so we can
verify state detection without changing behavior.
"""

import threading
from dataclasses import dataclass, field
from typing import Optional

from config import TRADE_HUBS
from tk_queue import submit


@dataclass
class PhaseState:
    """Shared state read by the locked overlay each poll tick.

    Filled in by the cold-start worker thread; read on the main
    (Tk) thread.  Plain dataclass with simple types — no locking
    needed because writes are atomic for these field types and
    readers tolerate transient inconsistency between fields.
    """

    current_phase: int = 0           # 0..7
    phase_name: str = "Detecting state"
    current: int = 0                  # progress within current phase
    total: int = 0                    # 0 = indeterminate
    detail: str = ""                  # free-text sub-status
    done: bool = False                # all phases complete -> unlock
    error: Optional[str] = None       # fatal phase error if any


@dataclass
class DetectedState:
    """Output of phase 0 — what's missing across all phases.

    Used by later phases to decide whether to skip themselves.
    """

    archive_missing_by_year: dict = field(default_factory=dict)  # {year: count}
    db_row_count: int = 0
    db_items_per_region: dict = field(default_factory=dict)      # {region_id: count}
    profiles_per_region: dict = field(default_factory=dict)      # {region_id: count}
    stale_hubs: list = field(default_factory=list)               # [(hub_key, region_id, name)]
    mf_pending_hubs: list = field(default_factory=list)          # [hub_key]
    li_pending_hubs: list = field(default_factory=list)          # [hub_key]


class StockMarketColdStartMixin:
    """Mixin providing the cold-start orchestrator.

    Expected parent attributes:
        frame            — ttk.Frame, the Stock Market tab frame
        downloader       — ArchiveDownloader instance
        profiles         — ProfileManager instance
        get_client       — callable returning ESIClient (may return None)
    """

    def _init_cold_start(self):
        """Initialise cold-start state.  Call from __init__."""
        self.phase_state = PhaseState()
        self._cold_start_thread: Optional[threading.Thread] = None

    def _start_cold_start_worker(self):
        """Spawn the worker thread.  Idempotent."""
        if self._cold_start_thread and self._cold_start_thread.is_alive():
            print("[ColdStart] Worker already running, skipping spawn")
            return

        self._cold_start_thread = threading.Thread(
            target=self._cold_start_run,
            daemon=True,
            name="StockMarketColdStart",
        )
        self._cold_start_thread.start()

    def _cold_start_run(self):
        """Worker entry point.  Runs phases sequentially."""
        try:
            print("[ColdStart] === Worker thread started ===")
            detected = self._cold_start_phase0_detect()
            self._cold_start_log_detected(detected)

            self._cold_start_phase3_profiles(detected)

            # Phases 1, 2, 4, 5, 6 will be added in subsequent steps.
            print("[ColdStart] === Cold start complete ===")
            self.phase_state.done = True
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.phase_state.error = str(e)
            print(f"[ColdStart] Worker crashed: {e}")

    # =========================================================================
    # Phase 0 — detect what's missing
    # =========================================================================

    def _cold_start_phase0_detect(self) -> DetectedState:
        """Inspect filesystem + DB + trackers; return DetectedState.

        Pure read-only.  Does not touch ESI or do any computation.
        """
        self.phase_state.current_phase = 0
        self.phase_state.phase_name = "Detecting state"
        self.phase_state.detail = ""

        state = DetectedState()

        # --- archive ---
        try:
            for year in self.downloader.get_years_to_download():
                missing = self.downloader.get_missing_dates(year)
                if missing:
                    state.archive_missing_by_year[year] = len(missing)
        except Exception as e:
            print(f"[ColdStart] archive detection failed: {e}")

        # --- market_history.db ---
        try:
            from market_history import get_market_history_db
            db = get_market_history_db()
            stats = db.get_stats()
            state.db_row_count = stats.get("row_count", 0)
            for hub_key, config in TRADE_HUBS.items():
                if not config.get("enabled", True):
                    continue
                region_id = config["region_id"]
                items = db.get_items_in_region(region_id)
                state.db_items_per_region[region_id] = len(items)
        except Exception as e:
            print(f"[ColdStart] db detection failed: {e}")

        # --- profiles per region ---
        try:
            for hub_key, config in TRADE_HUBS.items():
                if not config.get("enabled", True):
                    continue
                region_id = config["region_id"]
                profiles = self.profiles.get_profiles_for_region(region_id)
                state.profiles_per_region[region_id] = len(profiles) if profiles else 0
        except Exception as e:
            print(f"[ColdStart] profile detection failed: {e}")

        # --- stale hub order caches ---
        try:
            client = self.get_client() if self.get_client else None
            if client:
                state.stale_hubs = self._get_stale_hubs(client)
        except Exception as e:
            print(f"[ColdStart] stale-hub detection failed: {e}")

        # --- MF / LI 24h trackers ---
        try:
            from stockmarket_filters import MaterialFilterTracker
            from leading_indicators_tracker import LeadingIndicatorsTracker
            mf = MaterialFilterTracker()
            li = LeadingIndicatorsTracker()
            for hub_key, config in TRADE_HUBS.items():
                if not config.get("enabled", True):
                    continue
                if mf.should_run(hub_key):
                    state.mf_pending_hubs.append(hub_key)
                if li.should_run(hub_key):
                    state.li_pending_hubs.append(hub_key)
        except Exception as e:
            print(f"[ColdStart] tracker detection failed: {e}")

        return state

    # =========================================================================
    # Phase 3 — sequential profile build for all 5 regions
    # =========================================================================

    def _cold_start_phase3_profiles(self, detected: DetectedState):
        """Build profiles for any region that has DB history but no profiles.

        Sync work — uses ProfileManager.extract_all_from_db on the worker
        thread.  Updates phase_state so the locked overlay reflects
        progress: title shows current hub, progress bar shows item
        extraction within that region, detail shows region X of N.
        """
        regions_to_build = []
        for hub_key, config in TRADE_HUBS.items():
            if not config.get("enabled", True):
                continue
            region_id = config["region_id"]
            existing = detected.profiles_per_region.get(region_id, 0)
            if existing > 0:
                print(f"[ColdStart] phase 3: skipping {hub_key} "
                      f"(already has {existing:,} profiles)")
                continue
            db_items = detected.db_items_per_region.get(region_id, 0)
            if db_items == 0:
                print(f"[ColdStart] phase 3: skipping {hub_key} "
                      f"(no DB history for region)")
                continue
            regions_to_build.append((hub_key, region_id, config["name"]))

        if not regions_to_build:
            print("[ColdStart] phase 3: nothing to build, skipping")
            return

        # User-stated priority: Jita first, others in their natural
        # TRADE_HUBS order after that.  Stable sort preserves the
        # remaining order.
        regions_to_build.sort(key=lambda t: 0 if t[0] == "jita" else 1)

        print(f"[ColdStart] === Phase 3: building profiles for "
              f"{len(regions_to_build)} regions ===")

        from market_history import get_market_history_db
        db = get_market_history_db()

        total_regions = len(regions_to_build)
        for region_idx, (hub_key, region_id, hub_name) in enumerate(
            regions_to_build, start=1
        ):
            print(f"[ColdStart] phase 3 [{region_idx}/{total_regions}]: "
                  f"{hub_name} (region {region_id})")

            self.phase_state.current_phase = 3
            self.phase_state.phase_name = f"Building profiles for {hub_name}"
            self.phase_state.current = 0
            self.phase_state.total = 0
            self.phase_state.detail = f"region {region_idx} of {total_regions}"

            def progress_cb(
                msg: str, current: int, total: int,
                _ridx=region_idx, _rtot=total_regions,
            ):
                # Throttle phase_state writes — extract_all_from_db calls
                # this on every item.  200 matches the existing
                # _build_profiles_for_hub cadence.
                if current % 200 == 0 or current == total:
                    self.phase_state.current = current
                    self.phase_state.total = total
                    self.phase_state.detail = (
                        f"region {_ridx} of {_rtot} — {msg}"
                    )

            try:
                success, failed = self.profiles.extract_all_from_db(
                    region_id=region_id,
                    market_db=db,
                    progress_callback=progress_cb,
                )
                print(f"[ColdStart] phase 3: {hub_name} done — "
                      f"{success:,} ok, {failed} failed")

                # Trigger a panel refresh on the main thread so the
                # newly-built profiles render without waiting for the
                # MF/LI 24h trackers to expire.  Mirrors what
                # _on_profiles_built does after _build_profiles_for_hub.
                if success > 0:
                    panel = self.hub_panels.get(hub_key)
                    if panel:
                        submit(lambda p=panel: p.refresh_display_async())
            except Exception as e:
                print(f"[ColdStart] phase 3: {hub_name} failed: {e}")
                # Continue with remaining regions instead of bailing the
                # whole orchestrator — partial success is better than none.

        print("[ColdStart] === Phase 3 complete ===")

    # =========================================================================
    # Logging helpers
    # =========================================================================

    def _cold_start_log_detected(self, s: DetectedState):
        """Pretty-print detected state for visibility during scaffold phase."""
        print("[ColdStart] --- Detected state ---")
        if s.archive_missing_by_year:
            for year, n in sorted(s.archive_missing_by_year.items()):
                print(f"[ColdStart]   archive {year}: {n} files missing")
        else:
            print("[ColdStart]   archive: complete")
        print(f"[ColdStart]   db rows: {s.db_row_count:,}")
        for region_id, n in s.db_items_per_region.items():
            print(f"[ColdStart]   db items in region {region_id}: {n:,}")
        for region_id, n in s.profiles_per_region.items():
            print(f"[ColdStart]   profiles in region {region_id}: {n:,}")
        if s.stale_hubs:
            names = ", ".join(h[0] for h in s.stale_hubs)
            print(f"[ColdStart]   stale hubs: {names}")
        else:
            print("[ColdStart]   stale hubs: none (all caches fresh)")
        print(f"[ColdStart]   MF pending: {s.mf_pending_hubs or 'none'}")
        print(f"[ColdStart]   LI pending: {s.li_pending_hubs or 'none'}")
        print("[ColdStart] -----------------------")
