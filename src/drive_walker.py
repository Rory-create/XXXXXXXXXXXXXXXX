"""
Recursively walk Google Drive and yield file metadata.

Handles:
- My Drive (root)
- Shared drives (Team Drives)
- Pagination (1000 files per page max)
- Rate limiting via configurable delay
"""

import time
import logging
from typing import Iterator, Optional

from googleapiclient.errors import HttpError

import config

logger = logging.getLogger(__name__)

# Fields requested for every file. Enough for categorization + download.
FILE_FIELDS = (
    "id, name, mimeType, size, parents, "
    "createdTime, modifiedTime, "
    "owners, sharingUser, "
    "webViewLink"
)

LIST_FIELDS = f"nextPageToken, files({FILE_FIELDS})"


class DriveWalker:
    """
    Walks a Google Drive and yields all non-folder file metadata dicts.
    Builds a folder-path cache so each file knows its full path.
    """

    def __init__(self, service, max_pages: Optional[int] = None):
        self.service = service
        self.max_pages = max_pages or config.MAX_LIST_PAGES
        self._folder_cache: dict[str, str] = {}   # id → full path string

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def walk(self, root_folder_id: str = "root") -> Iterator[dict]:
        """
        Yield all files (non-folders) reachable from root_folder_id.
        Each yielded dict is the Drive API file resource + injected 'full_path'.
        """
        yield from self._walk_folder(root_folder_id, parent_path="")

    def walk_all(self) -> Iterator[dict]:
        """
        Convenience: yield ALL files in My Drive (root) and all
        accessible shared drives, deduped by file id.
        """
        seen_ids: set[str] = set()

        # My Drive
        logger.info("Walking My Drive...")
        for f in self.walk("root"):
            if f["id"] not in seen_ids:
                seen_ids.add(f["id"])
                yield f

        # Shared / Team Drives
        try:
            drives = self._list_shared_drives()
            for drive in drives:
                logger.info("Walking Shared Drive: %s", drive["name"])
                for f in self._walk_shared_drive(drive["id"], drive["name"]):
                    if f["id"] not in seen_ids:
                        seen_ids.add(f["id"])
                        yield f
        except HttpError as e:
            if e.resp.status == 403:
                logger.debug("No shared drives accessible (403) — skipping")
            else:
                raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _walk_folder(self, folder_id: str, parent_path: str) -> Iterator[dict]:
        """Recursively walk a folder by ID."""
        page_token = None
        page_count = 0

        while True:
            if self.max_pages and page_count >= self.max_pages:
                logger.warning("Hit max_pages=%d limit in folder %s", self.max_pages, folder_id)
                break

            try:
                resp = (
                    self.service.files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed=false",
                        fields=LIST_FIELDS,
                        pageToken=page_token,
                        pageSize=1000,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            except HttpError as e:
                logger.error("HTTP error listing folder %s: %s", folder_id, e)
                break

            time.sleep(config.API_DELAY_SECONDS)
            page_count += 1

            for item in resp.get("files", []):
                item_path = f"{parent_path}/{item['name']}" if parent_path else item["name"]
                item["full_path"] = item_path

                if item["mimeType"] == config.GOOGLE_FOLDER_MIME:
                    self._folder_cache[item["id"]] = item_path
                    yield from self._walk_folder(item["id"], item_path)
                else:
                    yield item

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def _list_shared_drives(self) -> list[dict]:
        drives = []
        page_token = None
        while True:
            resp = (
                self.service.drives()
                .list(pageSize=100, pageToken=page_token)
                .execute()
            )
            drives.extend(resp.get("drives", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return drives

    def _walk_shared_drive(self, drive_id: str, drive_name: str) -> Iterator[dict]:
        """Walk a shared drive from its root folder."""
        # The root of a shared drive has the same ID as the drive itself
        yield from self._walk_folder(drive_id, parent_path=f"[Shared] {drive_name}")
