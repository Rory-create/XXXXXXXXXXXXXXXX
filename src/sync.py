"""
Sync state: tracks what's been downloaded so re-runs only fetch new/changed files.

State is persisted to archive_output/sync_state.json as:
  {
    "<file_id>": {
      "modified_time": "2024-01-15T10:30:00Z",   # Drive's modifiedTime
      "local_path": "2023-2024/Math/HW.pdf",
      "downloaded_at": "2025-06-01T14:22:00Z"
    },
    ...
  }

A file needs a re-download if:
  - Its file_id is not in the state (brand new file)
  - Its modifiedTime in Drive differs from what we recorded (file was edited)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "sync_state.json"


class SyncState:
    """
    Persistent record of every file we've successfully downloaded.
    Thread-safety not required — single-process use only.
    """

    def __init__(self, output_dir: str):
        self._path = os.path.join(output_dir, STATE_FILENAME)
        self._state: dict[str, dict] = {}
        self._dirty = False

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load state from disk. Safe to call even if file doesn't exist yet."""
        if not os.path.exists(self._path):
            logger.debug("No existing sync state at %s — starting fresh", self._path)
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                self._state = json.load(f)
            logger.info(
                "Loaded sync state: %d previously downloaded files", len(self._state)
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load sync state (%s) — treating all files as new", e)
            self._state = {}

    def save(self) -> None:
        """Persist current state to disk."""
        if not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp, self._path)   # atomic on POSIX
            logger.debug("Sync state saved (%d entries)", len(self._state))
            self._dirty = False
        except OSError as e:
            logger.error("Could not save sync state: %s", e)

    # ------------------------------------------------------------------
    # Query / Update
    # ------------------------------------------------------------------

    def needs_download(self, file_id: str, modified_time: Optional[str]) -> bool:
        """
        Return True if this file should be downloaded.
        - True  → file is new, or has been modified since last download
        - False → file is unchanged; skip
        """
        entry = self._state.get(file_id)
        if entry is None:
            return True   # Never seen before
        if modified_time and entry.get("modified_time") != modified_time:
            logger.debug(
                "File %s changed: was %s, now %s",
                file_id,
                entry.get("modified_time"),
                modified_time,
            )
            return True
        return False

    def mark_downloaded(
        self,
        file_id: str,
        modified_time: Optional[str],
        local_path: str,
    ) -> None:
        """Record a successful download."""
        self._state[file_id] = {
            "modified_time": modified_time or "",
            "local_path": local_path,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        self._dirty = True

    # ------------------------------------------------------------------
    # Stats helpers
    # ------------------------------------------------------------------

    @property
    def total_recorded(self) -> int:
        return len(self._state)

    def previously_downloaded_paths(self) -> list[str]:
        return [v["local_path"] for v in self._state.values()]
