"""
Main archival orchestrator.

Ties together DriveWalker → Categorizer → Downloader.
Tracks token/API usage, handles graceful shutdown on budget/error,
and writes a final report.
"""

import os
import sys
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

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Graceful shutdown on Ctrl-C or SIGTERM
# -------------------------------------------------------------------
_shutdown_requested = False


def _signal_handler(sig, frame):
    global _shutdown_requested
    logger.warning("Shutdown signal received — will finish current file then stop.")
    _shutdown_requested = False   # set True to trigger clean exit
    _shutdown_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# -------------------------------------------------------------------

class LoopGuard:
    """
    Detects potential infinite loops / runaway API usage and signals abort.
    Aborts if:
      - More than MAX_FILES files queued
      - More than MAX_API_CALLS API pages fetched
      - Identical file IDs seen more than once (dedup guard)
    """

    def __init__(self, max_files: Optional[int] = config.MAX_FILES):
        self.max_files = max_files
        self.file_count = 0
        self.seen_ids: set[str] = set()
        self.duplicates = 0

    def check_file(self, file_id: str) -> bool:
        """Returns False if we should stop (budget hit or dupe loop)."""
        if file_id in self.seen_ids:
            self.duplicates += 1
            if self.duplicates > 50:
                logger.error("LoopGuard: >50 duplicate file IDs — likely infinite loop. Aborting.")
                return False
            return True   # Allow a few dupes (shared-drive overlap)
        self.seen_ids.add(file_id)
        self.file_count += 1

        if self.max_files and self.file_count > self.max_files:
            logger.warning("LoopGuard: MAX_FILES=%d reached — stopping.", self.max_files)
            return False
        return True


# -------------------------------------------------------------------

class Archiver:
    def __init__(self, service, dry_run: bool = False):
        self.service = service
        self.dry_run = dry_run
        self.walker = DriveWalker(service)
        self.categorizer = Categorizer()
        self.downloader = Downloader(service, config.OUTPUT_DIR)
        self.loop_guard = LoopGuard()
        self.start_time = datetime.now(timezone.utc)
        self.files_processed: list[dict] = []
        self.errors: list[str] = []

    def run(self) -> dict:
        """
        Main entry point.
        Returns a report dict.
        """
        global _shutdown_requested

        logger.info("=" * 60)
        logger.info("Google Drive School Archiver — starting run")
        logger.info("Output: %s | Dry-run: %s", config.OUTPUT_DIR, self.dry_run)
        logger.info("=" * 60)

        os.makedirs(config.OUTPUT_DIR, exist_ok=True)

        # --- Collect all files first (with progress) ---
        logger.info("Phase 1: Walking Google Drive (this may take a while)...")
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

        total = len(all_files)
        logger.info("Phase 1 complete: %d files found", total)

        # --- Download phase ---
        logger.info("Phase 2: Categorizing and downloading %d files...", total)
        bar = tqdm(all_files, desc="Archiving", unit="file", disable=not sys.stdout.isatty())

        for file_meta in bar:
            if _shutdown_requested:
                logger.warning("Shutdown requested — stopping download phase.")
                break

            name = file_meta.get("name", "?")
            bar.set_description(f"Archiving: {name[:40]}")

            try:
                category = self.categorizer.categorize(file_meta)
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
                }

                if not self.dry_run:
                    self.downloader.download(file_meta, category.full_path)

                self.files_processed.append(record)

            except Exception as e:
                msg = f"Error processing '{name}': {e}"
                logger.error(msg, exc_info=True)
                self.errors.append(msg)

        return self._build_report(aborted=_shutdown_requested)

    # ------------------------------------------------------------------

    def _build_report(self, aborted: bool = False) -> dict:
        elapsed = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        dl = self.downloader.stats

        # Build year/subject breakdown
        breakdown: dict[str, dict[str, int]] = {}
        for rec in self.files_processed:
            yr = rec["school_year"]
            subj = rec["subject"]
            breakdown.setdefault(yr, {})
            breakdown[yr][subj] = breakdown[yr].get(subj, 0) + 1

        report = {
            "run_timestamp": self.start_time.isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "status": "ABORTED" if aborted else "COMPLETE",
            "dry_run": self.dry_run,
            "total_files_found": len(self.files_processed),
            "download_stats": {
                "downloaded": dl.downloaded,
                "exported_google_workspace": dl.exported,
                "skipped": dl.skipped,
                "failed": dl.failed,
                "total_mb": round(dl.bytes_downloaded / (1024 * 1024), 2),
            },
            "breakdown_by_year_and_subject": breakdown,
            "errors": self.errors,
            "output_directory": os.path.abspath(config.OUTPUT_DIR),
        }

        # Write JSON report
        report_path = os.path.join(config.OUTPUT_DIR, "archive_report.json")
        try:
            with open(report_path, "w") as f:
                json.dump(report, f, indent=2)
            logger.info("Report written to %s", report_path)
        except Exception as e:
            logger.error("Could not write report: %s", e)

        # Write manifest CSV
        manifest_path = os.path.join(config.OUTPUT_DIR, "manifest.csv")
        try:
            _write_manifest(self.files_processed, manifest_path)
            logger.info("Manifest written to %s", manifest_path)
        except Exception as e:
            logger.error("Could not write manifest: %s", e)

        return report


def _write_manifest(records: list[dict], path: str):
    import csv
    if not records:
        return
    fieldnames = list(records[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
