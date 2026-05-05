"""Leading indicators tracker - once-per-day-per-hub gating.

Mirrors MaterialFilterTracker pattern. Tracks which hubs have had
leading indicators computed today, both in-memory (for the current
session) and via persisted DB rows (for cross-launch detection).

Phase 1: tracker only. No GUI integration yet.

Public API:
    get_leading_indicators_tracker() -> LeadingIndicatorsTracker
    LeadingIndicatorsTracker.should_run(hub_key) -> bool
    LeadingIndicatorsTracker.mark_complete(hub_key)
    LeadingIndicatorsTracker.has_run(hub_key) -> bool
"""

from datetime import date
from typing import Any, Dict, Optional, Set

import leading_indicators_storage


class LeadingIndicatorsTracker:
    """Tracks which hubs have had leading indicators computed today.

    Singleton pattern matches MaterialFilterTracker.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._ran_today: Set[str] = set()
            cls._instance._date = date.today()
        return cls._instance

    def should_run(self, hub_key: str) -> bool:
        """Check if leading indicators should be computed for this hub.

        Returns True if:
        - New day (resets tracking)
        - Hub hasn't been processed today (in-memory or persisted DB check)
        """
        # Reset on new day
        if date.today() != self._date:
            self._ran_today.clear()
            self._date = date.today()
            print("[LeadingIndicators] New day - reset tracking")

        # Fast path: confirmed in this session
        if hub_key in self._ran_today:
            print(f"[LeadingIndicators] {hub_key}: skipping "
                  f"(already ran today)")
            return False

        # Cross-launch: persisted rows from earlier today?
        from config import TRADE_HUBS
        hub_config = TRADE_HUBS.get(hub_key)
        if hub_config:
            region_id = hub_config["region_id"]
            if leading_indicators_storage.has_today_data(region_id):
                self._ran_today.add(hub_key)
                print(f"[LeadingIndicators] {hub_key}: skipping "
                      f"(persisted data from earlier today)")
                return False

        print(f"[LeadingIndicators] {hub_key}: will run "
              f"(first scan today)")
        return True

    def mark_complete(self, hub_key: str):
        """Mark hub as having completed leading indicators today."""
        self._ran_today.add(hub_key)
        print(f"[LeadingIndicators] {hub_key}: marked complete for today")

    def has_run(self, hub_key: str) -> bool:
        """Check if hub has already run today (no side effects)."""
        if date.today() != self._date:
            return False
        return hub_key in self._ran_today

    def get_status(self) -> Dict[str, Any]:
        """Status for debugging."""
        return {
            "date": str(self._date),
            "hubs_completed": list(self._ran_today),
            "is_current_day": date.today() == self._date,
        }


_tracker_instance: Optional[LeadingIndicatorsTracker] = None


def get_leading_indicators_tracker() -> LeadingIndicatorsTracker:
    """Get the global tracker instance."""
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = LeadingIndicatorsTracker()
    return _tracker_instance
