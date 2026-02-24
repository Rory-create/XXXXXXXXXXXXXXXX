"""
Download files from Google Drive.

Handles:
- Regular files (binary download via get_media)
- Google Workspace files (Docs/Sheets/Slides/etc.) via export
- Skips files too large or that exceed budget
- Resumes partial downloads via skip-if-exists
"""

import os
import io
import time
import logging
from typing import Optional

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

import config

logger = logging.getLogger(__name__)

# Google Workspace MIME types that need export (not direct download)
GOOGLE_WORKSPACE_MIMES = set(config.GOOGLE_EXPORT_FORMATS.keys())

# Non-downloadable Google types (we skip these)
SKIP_MIMES = {
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.map",
}


class DownloadStats:
    def __init__(self):
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.exported = 0
        self.bytes_downloaded = 0

    def summary(self) -> str:
        mb = self.bytes_downloaded / (1024 * 1024)
        return (
            f"Downloaded: {self.downloaded} files ({mb:.1f} MB)  |  "
            f"Exported (Google → Office): {self.exported}  |  "
            f"Skipped: {self.skipped}  |  "
            f"Failed: {self.failed}"
        )


class Downloader:
    def __init__(self, service, output_root: str = config.OUTPUT_DIR):
        self.service = service
        self.output_root = output_root
        self.stats = DownloadStats()
        self._total_bytes = 0

    def download(self, file_meta: dict, relative_path: str) -> bool:
        """
        Download a single file to output_root/relative_path.
        Returns True on success, False on skip/error.
        """
        mime = file_meta.get("mimeType", "")
        file_id = file_meta["id"]
        file_name = file_meta.get("name", file_id)

        if mime in SKIP_MIMES:
            logger.debug("Skipping unsupported type %s: %s", mime, file_name)
            self.stats.skipped += 1
            return False

        # Check download budget
        if config.MAX_DOWNLOAD_MB:
            budget_bytes = config.MAX_DOWNLOAD_MB * 1024 * 1024
            if self._total_bytes >= budget_bytes:
                logger.warning("Download budget exhausted (%.1f MB)", config.MAX_DOWNLOAD_MB)
                self.stats.skipped += 1
                return False

        if mime in GOOGLE_WORKSPACE_MIMES:
            return self._export_google_file(file_meta, relative_path)
        else:
            return self._download_binary(file_meta, relative_path)

    # ------------------------------------------------------------------

    def _export_google_file(self, file_meta: dict, relative_path: str) -> bool:
        """Export a Google Workspace file (Docs/Sheets/Slides) to Office format."""
        mime = file_meta["mimeType"]
        export_mime, extension = config.GOOGLE_EXPORT_FORMATS[mime]

        # Ensure the output path has the right extension
        base, _ = os.path.splitext(relative_path)
        out_path = os.path.join(self.output_root, base + extension)

        if os.path.exists(out_path):
            logger.debug("Already exists, skipping: %s", out_path)
            self.stats.skipped += 1
            return True

        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        try:
            request = self.service.files().export_media(
                fileId=file_meta["id"],
                mimeType=export_mime,
            )
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            data = buffer.getvalue()
        except HttpError as e:
            logger.error("Export failed for '%s': %s", file_meta.get("name"), e)
            self.stats.failed += 1
            return False

        with open(out_path, "wb") as f:
            f.write(data)

        size = len(data)
        self._total_bytes += size
        self.stats.exported += 1
        self.stats.bytes_downloaded += size
        logger.info("Exported → %s (%.1f KB)", out_path, size / 1024)
        time.sleep(config.API_DELAY_SECONDS)
        return True

    def _download_binary(self, file_meta: dict, relative_path: str) -> bool:
        """Download a regular (non-Google-Workspace) file."""
        out_path = os.path.join(self.output_root, relative_path)

        if os.path.exists(out_path):
            logger.debug("Already exists, skipping: %s", out_path)
            self.stats.skipped += 1
            return True

        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        try:
            request = self.service.files().get_media(fileId=file_meta["id"],
                                                      supportsAllDrives=True)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            data = buffer.getvalue()
        except HttpError as e:
            logger.error("Download failed for '%s': %s", file_meta.get("name"), e)
            self.stats.failed += 1
            return False

        with open(out_path, "wb") as f:
            f.write(data)

        size = len(data)
        self._total_bytes += size
        self.stats.downloaded += 1
        self.stats.bytes_downloaded += size
        logger.info("Downloaded → %s (%.1f KB)", out_path, size / 1024)
        time.sleep(config.API_DELAY_SECONDS)
        return True
