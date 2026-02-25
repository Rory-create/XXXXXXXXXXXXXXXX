"""
Main archival orchestrator.

Ties together DriveWalker → Categorizer → Downloader → SyncState.

Run phases:
  1. Walk Drive (My Drive + Shared Drives + Shared With Me)
  2. Categorize all files and compute size estimate
  3. Show size report + disk space check → prompt for confirmation
  4. Download (sync-aware: skip unchanged files)
  5. Save sync state + write report + manifest
"""

import os
import sys
import shutil
import time
import signal
import logging
import json
from datetime import datetime, timezone
from typing import Optional

from tqdm import tqdm

import config
from src.drive_walker import DriveWalker
from src.categorizer import Categorizer
from src.downloader import Downloader
from src.sync import SyncState

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Graceful shutdown on Ctrl-C or SIGTERM
# -------------------------------------------------------------------
_shutdown_requested = False


def _signal_handler(sig, frame):
    global _shutdown_requested
    if _shutdown_requested:
        # Second signal: force exit immediately
        logger.warning("Force-quit requested.")
        sys.exit(1)
    logger.warning("Shutdown signal received — finishing current file then stopping. Ctrl-C again to force quit.")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

GOOGLE_WORKSPACE_MIMES = set(config.GOOGLE_EXPORT_FORMATS.keys())
SKIP_MIMES = {
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.map",
}


def _fmt_bytes(n: int) -> str:
    """Human-readable byte size."""
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{n / 1024:.1f} KB"


def _get_free_space(path: str) -> int:
    """Return free bytes on the filesystem containing path."""
    os.makedirs(path, exist_ok=True)
    return shutil.disk_usage(path).free


class LoopGuard:
    def __init__(self, max_files: Optional[int] = None):
        self.max_files = max_files or config.MAX_FILES
        self.file_count = 0
        self.seen_ids: set[str] = set()
        self.duplicates = 0

    def check_file(self, file_id: str) -> bool:
        if file_id in self.seen_ids:
            self.duplicates += 1
            if self.duplicates > 50:
                logger.error("LoopGuard: >50 duplicate file IDs — likely infinite loop. Aborting.")
                return False
            return True
        self.seen_ids.add(file_id)
        self.file_count += 1
        if self.max_files and self.file_count > self.max_files:
            logger.warning("LoopGuard: MAX_FILES=%d reached — stopping.", self.max_files)
            return False
        return True


# -------------------------------------------------------------------

class SizeEstimate:
    """
    Summarises expected download size from Drive file metadata.
    Google Workspace files don't have a size in Drive metadata (they're
    stored server-side), so we report them separately as 'size unknown'.
    """
    def __init__(self):
        self.known_bytes: int = 0         # sum of size fields for binary files
        self.unknown_count: int = 0       # Google Workspace files (no size in metadata)
        self.skip_count: int = 0          # unsupported types that will be skipped
        self.new_count: int = 0           # files that need downloading (new or changed)
        self.unchanged_count: int = 0     # files sync will skip

    def add(self, file_meta: dict, needs_download: bool) -> None:
        mime = file_meta.get("mimeType", "")
        if mime in SKIP_MIMES:
            self.skip_count += 1
            return

        if not needs_download:
            self.unchanged_count += 1
            return

        self.new_count += 1
        if mime in GOOGLE_WORKSPACE_MIMES:
            self.unknown_count += 1
        else:
            try:
                self.known_bytes += int(file_meta.get("size") or 0)
            except (ValueError, TypeError):
                self.unknown_count += 1

    def print_summary(self, free_bytes: int) -> None:
        print()
        print("┌─────────────────────────────────────────────────────┐")
        print("│              PRE-DOWNLOAD SIZE ESTIMATE              │")
        print("├─────────────────────────────────────────────────────┤")
        print(f"│  Files to download (new/changed):  {self.new_count:<18}│")
        print(f"│  Already up-to-date (sync skip):   {self.unchanged_count:<18}│")
        print(f"│  Unsupported types (will skip):    {self.skip_count:<18}│")
        print("├─────────────────────────────────────────────────────┤")
        known_str = _fmt_bytes(self.known_bytes)
        print(f"│  Known download size:              {known_str:<18}│")
        if self.unknown_count:
            unk_str = f"{self.unknown_count} Google file(s)"
            print(f"│  + Google Workspace exports:       {unk_str:<18}│")
            print(f"│    (Docs/Sheets/Slides — size unknown            │")
            print(f"│     until exported, typically small)             │")
        print("├─────────────────────────────────────────────────────┤")
        free_str = _fmt_bytes(free_bytes)
        print(f"│  Free disk space:                  {free_str:<18}│")
        if self.known_bytes > free_bytes:
            print("│  !! WARNING: Known size exceeds free space!         │")
        elif self.known_bytes > free_bytes * 0.9:
            print("│  !! NOTE: Will use >90% of your free disk space.    │")
        else:
            print("│  OK: Looks like you have enough space.              │")
        print("└─────────────────────────────────────────────────────┘")
        print()


# -------------------------------------------------------------------

class Archiver:
    def __init__(self, service, dry_run: bool = False, yes: bool = False):
        self.service = service
        self.dry_run = dry_run
        self.yes = yes   # skip confirmation prompt
        self.walker = DriveWalker(service)
        self.categorizer = Categorizer()
        self.sync_state = SyncState(config.OUTPUT_DIR)
        self.downloader = Downloader(service, config.OUTPUT_DIR, self.sync_state)
        self.loop_guard = LoopGuard()
        self.start_time = datetime.now(timezone.utc)
        self.files_processed: list[dict] = []
        self.errors: list[str] = []

    def run(self) -> dict:
        global _shutdown_requested

        logger.info("=" * 60)
        logger.info("Google Drive School Archiver — starting run")
        logger.info("Output: %s | Dry-run: %s", config.OUTPUT_DIR, self.dry_run)
        logger.info("=" * 60)

        os.makedirs(config.OUTPUT_DIR, exist_ok=True)

        # Load sync state from previous run (if any)
        self.sync_state.load()
        prior_count = self.sync_state.total_recorded
        if prior_count:
            logger.info(
                "Sync: %d files from previous run recorded — "
                "unchanged files will be skipped",
                prior_count,
            )

        # ── Phase 1: Walk ──────────────────────────────────────────
        logger.info(
            "Phase 1: Walking Google Drive "
            "(My Drive + Shared Drives + Shared With Me)..."
        )
        all_files = []
        try:
            for file_meta in self.walker.walk_all():
                if _shutdown_requested:
                    logger.warning("Shutdown requested during walk phase.")
                    break
                if not self.loop_guard.check_file(file_meta["id"]):
                    _shutdown_requested = True
                    break
                all_files.append(file_meta)
        except Exception as e:
            logger.error("Error during Drive walk: %s", e, exc_info=True)
            self.errors.append(f"Walk error: {e}")

        logger.info("Phase 1 complete: %d files found", len(all_files))

        # ── Phase 2: Categorize + Size Estimate ───────────────────
        logger.info("Phase 2: Categorizing and estimating download size...")
        estimate = SizeEstimate()
        categorized: list[tuple[dict, object]] = []

        for file_meta in all_files:
            try:
                category = self.categorizer.categorize(file_meta)
                needs_dl = self.sync_state.needs_download(
                    file_meta["id"], file_meta.get("modifiedTime")
                )
                estimate.add(file_meta, needs_dl)
                categorized.append((file_meta, category))
            except Exception as e:
                msg = f"Categorize error for '{file_meta.get('name', '?')}': {e}"
                logger.error(msg)
                self.errors.append(msg)

        # ── Phase 3: Size check + confirmation ────────────────────
        if not self.dry_run:
            free_bytes = _get_free_space(config.OUTPUT_DIR)
            estimate.print_summary(free_bytes)

            if not self.yes:
                confirmed = _prompt_confirmation(estimate)
                if not confirmed:
                    logger.info("Download cancelled by user.")
                    return self._build_report(aborted=True)

        # ── Phase 4: Download ──────────────────────────────────────
        action = "Dry-run scan" if self.dry_run else "Downloading"
        logger.info("Phase 4: %s — %d files...", action, len(categorized))
        bar = tqdm(
            categorized,
            desc="Archiving",
            unit="file",
            disable=not sys.stdout.isatty(),
        )

        for file_meta, category in bar:
            if _shutdown_requested:
                logger.warning("Shutdown requested — stopping download phase.")
                break

            name = file_meta.get("name", "?")
            bar.set_description(f"Archiving: {name[:40]}")

            try:
                record = {
                    "id": file_meta["id"],
                    "name": name,
                    "school_year": category.school_year,
                    "subject": category.subject,
                    "output_path": category.full_path,
                    "mime": file_meta.get("mimeType", ""),
                    "created": file_meta.get("createdTime", ""),
                    "modified": file_meta.get("modifiedTime", ""),
                    "drive_path": file_meta.get("full_path", ""),
                    "shared_with_me": file_meta.get("_shared_with_me", False),
                }

                if not self.dry_run:
                    self.downloader.download(file_meta, category.full_path)

                self.files_processed.append(record)

            except Exception as e:
                msg = f"Error processing '{name}': {e}"
                logger.error(msg, exc_info=True)
                self.errors.append(msg)

        # ── Phase 5: Persist sync state ────────────────────────────
        if not self.dry_run:
            self.sync_state.save()
            logger.info("Sync state saved.")

        return self._build_report(aborted=_shutdown_requested)

    # ------------------------------------------------------------------

    def _build_report(self, aborted: bool = False) -> dict:
        elapsed = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        dl = self.downloader.stats

        breakdown: dict[str, dict[str, int]] = {}
        shared_count = 0
        for rec in self.files_processed:
            yr = rec["school_year"]
            subj = rec["subject"]
            breakdown.setdefault(yr, {})
            breakdown[yr][subj] = breakdown[yr].get(subj, 0) + 1
            if rec.get("shared_with_me"):
                shared_count += 1

        report = {
            "run_timestamp": self.start_time.isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "status": "ABORTED" if aborted else "COMPLETE",
            "dry_run": self.dry_run,
            "total_files_found": len(self.files_processed),
            "shared_with_me_count": shared_count,
            "download_stats": {
                "downloaded": dl.downloaded,
                "exported_google_workspace": dl.exported,
                "synced_unchanged": dl.synced_unchanged,
                "skipped": dl.skipped,
                "failed": dl.failed,
                "total_mb": round(dl.bytes_downloaded / (1024 * 1024), 2),
            },
            "breakdown_by_year_and_subject": breakdown,
            "errors": self.errors,
            "output_directory": os.path.abspath(config.OUTPUT_DIR),
        }

        report_path = os.path.join(config.OUTPUT_DIR, "archive_report.json")
        try:
            with open(report_path, "w") as f:
                json.dump(report, f, indent=2)
            logger.info("Report written to %s", report_path)
        except Exception as e:
            logger.error("Could not write report: %s", e)

        manifest_path = os.path.join(config.OUTPUT_DIR, "manifest.csv")
        try:
            _write_manifest(self.files_processed, manifest_path)
            logger.info("Manifest written to %s", manifest_path)
        except Exception as e:
            logger.error("Could not write manifest: %s", e)

        return report


# -------------------------------------------------------------------

def _prompt_confirmation(estimate: SizeEstimate) -> bool:
    """Ask the user if they want to proceed with the download."""
    if estimate.new_count == 0:
        print("Nothing new to download — all files are already up to date!\n")
        return False

    while True:
        try:
            ans = input("Proceed with download? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no", ""):
            return False
        print("Please enter y or n.")


def _write_manifest(records: list[dict], path: str):
    import csv
    if not records:
        return
    fieldnames = list(records[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
