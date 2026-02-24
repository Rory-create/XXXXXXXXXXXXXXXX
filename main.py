#!/usr/bin/env python3
"""
Google Drive School Archiver — entry point.

Usage:
    python main.py [--dry-run] [--output-dir PATH]

Options:
    --dry-run       Walk Drive and categorize, but don't download anything.
                    Great for a first pass to see what's there.
    --output-dir    Where to save files (default: archive_output/)
    --max-files N   Stop after N files (safety cap)
    --max-mb N      Stop after N MB downloaded
    --log-level     DEBUG | INFO | WARNING (default: INFO)
"""

import argparse
import logging
import os
import sys
import json

import config


def setup_logging(level_str: str = "INFO"):
    level = getattr(logging, level_str.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        ],
    )


def print_report(report: dict):
    print("\n" + "=" * 60)
    print("ARCHIVE RUN REPORT")
    print("=" * 60)
    print(f"Status        : {report['status']}")
    print(f"Elapsed       : {report['elapsed_seconds']}s")
    print(f"Dry-run       : {report['dry_run']}")
    print(f"Files found   : {report['total_files_found']}")
    dl = report["download_stats"]
    print(f"Downloaded    : {dl['downloaded']} files ({dl['total_mb']} MB)")
    print(f"Exported      : {dl['exported_google_workspace']} Google Workspace files")
    print(f"Skipped       : {dl['skipped']}")
    print(f"Failed        : {dl['failed']}")
    print(f"Output dir    : {report['output_directory']}")

    print("\nBreakdown by School Year & Subject:")
    breakdown = report.get("breakdown_by_year_and_subject", {})
    for year in sorted(breakdown):
        print(f"  {year}:")
        for subj, count in sorted(breakdown[year].items(), key=lambda x: -x[1]):
            print(f"    {subj:<25} {count} file(s)")

    if report.get("errors"):
        print(f"\nErrors ({len(report['errors'])}):")
        for e in report["errors"][:10]:
            print(f"  - {e}")
        if len(report["errors"]) > 10:
            print(f"  ... and {len(report['errors']) - 10} more (see archive_report.json)")

    print("\nFull report: archive_output/archive_report.json")
    print("File manifest: archive_output/manifest.csv")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Archive your school Google Drive")
    parser.add_argument("--dry-run", action="store_true",
                        help="Walk and categorize only; don't download")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory (default: archive_output)")
    parser.add_argument("--max-files", type=int, default=None,
                        help="Max files to process")
    parser.add_argument("--max-mb", type=float, default=None,
                        help="Max total MB to download")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    # Apply CLI overrides to config
    if args.output_dir:
        config.OUTPUT_DIR = args.output_dir
    if args.max_files:
        config.MAX_FILES = args.max_files
    if args.max_mb:
        config.MAX_DOWNLOAD_MB = args.max_mb

    logger.info("Google Drive School Archiver starting up")

    # Auth
    try:
        from src.auth import get_credentials, build_drive_service
        creds = get_credentials()
        service = build_drive_service(creds)
    except FileNotFoundError as e:
        print(f"\nERROR: {e}\n")
        sys.exit(1)
    except Exception as e:
        logger.error("Authentication failed: %s", e, exc_info=True)
        sys.exit(1)

    # Archive
    from src.archiver import Archiver
    archiver = Archiver(service, dry_run=args.dry_run)

    try:
        report = archiver.run()
    except Exception as e:
        logger.error("Unexpected fatal error: %s", e, exc_info=True)
        # Try to get a partial report
        report = archiver._build_report(aborted=True)
        report["fatal_error"] = str(e)

    print_report(report)


if __name__ == "__main__":
    main()
