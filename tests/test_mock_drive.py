"""
Integration test: full pipeline with a mocked Google Drive service.

Simulates a realistic school Drive structure:
  /
  ├── 2022-2023/
  │   ├── AP English/
  │   │   ├── Essay1.docx  (Google Doc)
  │   │   └── ReadingLog.xlsx (Google Sheet)
  │   ├── Algebra II/
  │   │   └── Unit4Notes.pdf
  │   └── Misc Stuff/
  │       └── Random.txt
  ├── Junior Year 23-24/
  │   ├── AP Chemistry/
  │   │   └── LabReport.docx (Google Doc)
  │   └── APUSH/
  │       └── Essay_APUSH.docx
  └── Random Old File.txt  (no year folder → fall back to timestamp)

Shared With Me:
  [Shared] Alex Johnson/
      GroupProject.pptx  (Google Slides)
  SharedDirectFile.pdf   (shared directly, no folder)

All API calls are mocked — no credentials needed.
"""

import sys
import os
import io
import re
import json
import shutil
import unittest
import tempfile
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from src.categorizer import Categorizer
from src.drive_walker import DriveWalker
from src.downloader import Downloader
from src.archiver import Archiver, SizeEstimate
from src.sync import SyncState


# ---------------------------------------------------------------
# Fake Drive file structure
# ---------------------------------------------------------------

FOLDER = "application/vnd.google-apps.folder"
GDOC   = "application/vnd.google-apps.document"
GSHEET = "application/vnd.google-apps.spreadsheet"
PDF    = "application/pdf"
TXT    = "text/plain"


def _file(fid, name, mime, parent, created="2022-09-10T08:00:00Z", path=""):
    return {
        "id": fid,
        "name": name,
        "mimeType": mime,
        "parents": [parent],
        "createdTime": created,
        "modifiedTime": created,
        "size": "1024",
        "full_path": path or name,
        "webViewLink": f"https://drive.google.com/file/{fid}",
    }


# Folder IDs
ROOT    = "root"
F_2223  = "folder_2223"
F_ENG   = "folder_eng"
F_ALG   = "folder_alg"
F_MISC  = "folder_misc"
F_JR    = "folder_junior"
F_CHEM  = "folder_chem"
F_PUSH  = "folder_apush"
F_SHARD = "folder_shared_alex"   # a folder shared with the user

# File IDs
FID_ESSAY      = "file_essay"
FID_READING    = "file_reading"
FID_NOTES      = "file_notes"
FID_RANDOM     = "file_random"
FID_LAB        = "file_lab"
FID_APUSH      = "file_apush"
FID_OLDTXT     = "file_oldtxt"
FID_SHARED_PPT = "file_shared_ppt"   # inside a shared folder
FID_SHARED_PDF = "file_shared_pdf"   # shared directly (no folder)


GSLIDES = "application/vnd.google-apps.presentation"

# Shared-with-me top-level items: a folder and a bare file
SHARED_WITH_ME_ITEMS = [
    {
        "id": F_SHARD,
        "name": "Group Project Alex",
        "mimeType": FOLDER,
        "parents": [],
        "createdTime": "2024-03-01T00:00:00Z",
        "modifiedTime": "2024-03-01T00:00:00Z",
        "size": "0",
        "full_path": "[Shared] Alex Johnson/Group Project Alex",
        "webViewLink": "https://drive.google.com/folder/shared_alex",
        "sharingUser": {"displayName": "Alex Johnson"},
        "owners": [{"displayName": "Alex Johnson"}],
    },
    {
        "id": FID_SHARED_PDF,
        "name": "SharedDirectFile.pdf",
        "mimeType": PDF,
        "parents": [],
        "createdTime": "2024-04-10T00:00:00Z",
        "modifiedTime": "2024-04-10T00:00:00Z",
        "size": "2048",
        "full_path": "[Shared] Alex Johnson/SharedDirectFile.pdf",
        "webViewLink": "https://drive.google.com/file/shared_pdf",
        "sharingUser": {"displayName": "Alex Johnson"},
        "owners": [{"displayName": "Alex Johnson"}],
    },
]

# Contents of the shared folder
SHARED_FOLDER_CONTENTS = [
    {
        "id": FID_SHARED_PPT,
        "name": "GroupProject.pptx",
        "mimeType": GSLIDES,
        "parents": [F_SHARD],
        "createdTime": "2024-03-05T00:00:00Z",
        "modifiedTime": "2024-03-10T00:00:00Z",
        "size": "0",
        "full_path": "[Shared] Alex Johnson/Group Project Alex/GroupProject.pptx",
        "webViewLink": "https://drive.google.com/file/shared_ppt",
    },
]


def build_mock_service():
    """
    Build a mock googleapiclient service that returns our fake tree.
    list() calls are keyed by the 'q' parameter (parent folder or sharedWithMe).
    """
    service = MagicMock()

    # Map: parent_id → list of items (folders + files)
    tree = {
        ROOT: [
            _file(F_2223, "2022-2023", FOLDER, ROOT, path="2022-2023"),
            _file(F_JR,   "Junior Year 23-24", FOLDER, ROOT, created="2023-08-15T00:00:00Z", path="Junior Year 23-24"),
            _file(FID_OLDTXT, "Random Old File.txt", TXT, ROOT, created="2021-11-01T00:00:00Z", path="Random Old File.txt"),
        ],
        F_2223: [
            _file(F_ENG,  "AP English", FOLDER, F_2223, path="2022-2023/AP English"),
            _file(F_ALG,  "Algebra II", FOLDER, F_2223, path="2022-2023/Algebra II"),
            _file(F_MISC, "Misc Stuff", FOLDER, F_2223, path="2022-2023/Misc Stuff"),
        ],
        F_ENG: [
            _file(FID_ESSAY,   "Essay1",     GDOC,   F_ENG,  path="2022-2023/AP English/Essay1"),
            _file(FID_READING, "ReadingLog", GSHEET, F_ENG,  path="2022-2023/AP English/ReadingLog"),
        ],
        F_ALG: [
            _file(FID_NOTES, "Unit4Notes.pdf", PDF, F_ALG, path="2022-2023/Algebra II/Unit4Notes.pdf"),
        ],
        F_MISC: [
            _file(FID_RANDOM, "Random.txt", TXT, F_MISC, path="2022-2023/Misc Stuff/Random.txt"),
        ],
        F_JR: [
            _file(F_CHEM, "AP Chemistry", FOLDER, F_JR, created="2023-09-01T00:00:00Z", path="Junior Year 23-24/AP Chemistry"),
            _file(F_PUSH, "APUSH",        FOLDER, F_JR, created="2023-09-01T00:00:00Z", path="Junior Year 23-24/APUSH"),
        ],
        F_CHEM: [
            _file(FID_LAB, "LabReport", GDOC, F_CHEM, created="2023-10-05T00:00:00Z", path="Junior Year 23-24/AP Chemistry/LabReport"),
        ],
        F_PUSH: [
            _file(FID_APUSH, "Essay_APUSH", GDOC, F_PUSH, created="2023-11-01T00:00:00Z", path="Junior Year 23-24/APUSH/Essay_APUSH"),
        ],
        # Shared folder contents
        F_SHARD: SHARED_FOLDER_CONTENTS,
    }

    def make_list_execute(parent_id):
        items = tree.get(parent_id, [])
        return {"files": items, "nextPageToken": None}

    def files_list_side_effect(**kwargs):
        q = kwargs.get("q", "")
        # sharedWithMe query
        if "sharedWithMe=true" in q:
            mock_req = MagicMock()
            mock_req.execute.return_value = {
                "files": SHARED_WITH_ME_ITEMS,
                "nextPageToken": None,
            }
            return mock_req
        # folder query
        m = re.search(r"'([^']+)' in parents", q)
        parent_id = m.group(1) if m else ROOT
        mock_req = MagicMock()
        mock_req.execute.return_value = make_list_execute(parent_id)
        return mock_req

    service.files.return_value.list.side_effect = files_list_side_effect

    # Mock drives().list() — no shared drives
    drives_mock = MagicMock()
    drives_mock.execute.return_value = {"drives": [], "nextPageToken": None}
    service.drives.return_value.list.return_value = drives_mock

    # Mock export_media — returns fake bytes
    service.files.return_value.export_media.return_value = MagicMock()

    # Mock get_media for binary files
    service.files.return_value.get_media.return_value = MagicMock()

    return service


# ---------------------------------------------------------------
# Walker Tests
# ---------------------------------------------------------------

class TestDriveWalker(unittest.TestCase):
    def setUp(self):
        self.service = build_mock_service()
        self.walker = DriveWalker(self.service)

    def test_walk_finds_all_files(self):
        files = list(self.walker.walk("root"))
        # My Drive has 7 non-folder files (shared-with-me is separate)
        self.assertEqual(len(files), 7)

    def test_walk_no_folders_returned(self):
        files = list(self.walker.walk("root"))
        for f in files:
            self.assertNotEqual(f["mimeType"], FOLDER)

    def test_file_names_present(self):
        files = list(self.walker.walk("root"))
        names = {f["name"] for f in files}
        self.assertIn("Essay1", names)
        self.assertIn("LabReport", names)
        self.assertIn("Unit4Notes.pdf", names)

    def test_paths_set_correctly(self):
        files = list(self.walker.walk("root"))
        paths = {f["full_path"] for f in files}
        self.assertIn("2022-2023/AP English/Essay1", paths)
        self.assertIn("Junior Year 23-24/APUSH/Essay_APUSH", paths)

    def test_dedup_in_walk_all(self):
        # walk_all should not return duplicates
        files = list(self.walker.walk_all())
        ids = [f["id"] for f in files]
        self.assertEqual(len(ids), len(set(ids)))


# ---------------------------------------------------------------
# Categorizer Integration with Drive Metadata
# ---------------------------------------------------------------

class TestCategorizerWithDriveFiles(unittest.TestCase):
    def setUp(self):
        self.cat = Categorizer()

    def _get_file(self, fid, path=""):
        tree_flat = {
            FID_ESSAY:   _file(FID_ESSAY,   "Essay1",     GDOC,   F_ENG,  path="2022-2023/AP English/Essay1"),
            FID_READING: _file(FID_READING, "ReadingLog", GSHEET, F_ENG,  path="2022-2023/AP English/ReadingLog"),
            FID_NOTES:   _file(FID_NOTES,   "Unit4Notes.pdf", PDF, F_ALG, path="2022-2023/Algebra II/Unit4Notes.pdf"),
            FID_RANDOM:  _file(FID_RANDOM,  "Random.txt", TXT,   F_MISC,  path="2022-2023/Misc Stuff/Random.txt"),
            FID_LAB:     _file(FID_LAB,     "LabReport",  GDOC,  F_CHEM,  created="2023-10-05T00:00:00Z", path="Junior Year 23-24/AP Chemistry/LabReport"),
            FID_APUSH:   _file(FID_APUSH,   "Essay_APUSH",GDOC,  F_PUSH,  created="2023-11-01T00:00:00Z", path="Junior Year 23-24/APUSH/Essay_APUSH"),
            FID_OLDTXT:  _file(FID_OLDTXT,  "Random Old File.txt", TXT, ROOT, created="2021-11-01T00:00:00Z", path="Random Old File.txt"),
        }
        return tree_flat[fid]

    def test_essay_categorized_english_2022_2023(self):
        f = self._get_file(FID_ESSAY)
        result = self.cat.categorize(f)
        self.assertEqual(result.school_year, "2022-2023")
        self.assertEqual(result.subject, "English")

    def test_algebra_notes(self):
        f = self._get_file(FID_NOTES)
        result = self.cat.categorize(f)
        self.assertEqual(result.school_year, "2022-2023")
        self.assertEqual(result.subject, "Math")

    def test_chem_lab_junior_year(self):
        f = self._get_file(FID_LAB)
        result = self.cat.categorize(f)
        self.assertEqual(result.school_year, "2023-2024")
        self.assertEqual(result.subject, "Science")

    def test_apush_essay(self):
        f = self._get_file(FID_APUSH)
        result = self.cat.categorize(f)
        self.assertEqual(result.school_year, "2023-2024")
        self.assertEqual(result.subject, "History")

    def test_random_misc_fallback(self):
        f = self._get_file(FID_RANDOM)
        result = self.cat.categorize(f)
        self.assertEqual(result.school_year, "2022-2023")
        self.assertEqual(result.subject, "Misc")

    def test_old_file_timestamp_fallback(self):
        f = self._get_file(FID_OLDTXT)
        result = self.cat.categorize(f)
        # Created Nov 2021 → 2021-2022 (before Aug 2022)
        self.assertEqual(result.school_year, "2021-2022")


# ---------------------------------------------------------------
# Downloader Tests (with mocked MediaIoBaseDownload)
# ---------------------------------------------------------------

class TestDownloader(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.service = build_mock_service()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_downloader(self):
        return Downloader(self.service, output_root=self.tmpdir)

    @patch("src.downloader.MediaIoBaseDownload")
    def test_binary_download(self, mock_dl_cls):
        # Mock the downloader to write fake bytes
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (MagicMock(progress=lambda: 1.0), True)
        mock_dl_cls.return_value = mock_dl

        # Patch the buffer write behavior
        fake_data = b"fake pdf content"
        def fake_next_chunk():
            mock_dl_cls.call_args[0][0].write(fake_data)  # write to the BytesIO
            return MagicMock(), True
        mock_dl.next_chunk.side_effect = fake_next_chunk

        f = _file(FID_NOTES, "Unit4Notes.pdf", PDF, F_ALG)
        dl = self._make_downloader()
        result = dl.download(f, "2022-2023/Math/Unit4Notes.pdf")
        self.assertTrue(result)
        out_path = os.path.join(self.tmpdir, "2022-2023/Math/Unit4Notes.pdf")
        self.assertTrue(os.path.exists(out_path))

    @patch("src.downloader.MediaIoBaseDownload")
    def test_google_doc_export(self, mock_dl_cls):
        fake_data = b"fake docx content"
        def fake_next_chunk():
            mock_dl_cls.call_args[0][0].write(fake_data)
            return MagicMock(), True
        mock_dl = MagicMock()
        mock_dl.next_chunk.side_effect = fake_next_chunk
        mock_dl_cls.return_value = mock_dl

        f = _file(FID_ESSAY, "Essay1", GDOC, F_ENG)
        dl = self._make_downloader()
        result = dl.download(f, "2022-2023/English/Essay1")
        self.assertTrue(result)
        # Should be exported as .docx
        out_path = os.path.join(self.tmpdir, "2022-2023/English/Essay1.docx")
        self.assertTrue(os.path.exists(out_path))

    def test_sync_state_skips_unchanged_file(self):
        # Sync state says this file was already downloaded with the same modifiedTime
        state = SyncState(self.tmpdir)
        state.load()
        mtime = "2022-09-10T08:00:00Z"
        state.mark_downloaded(FID_NOTES, mtime, "2022-2023/Math/Unit4Notes.pdf")

        f = _file(FID_NOTES, "Unit4Notes.pdf", PDF, F_ALG)
        f["modifiedTime"] = mtime
        dl = Downloader(self.service, output_root=self.tmpdir, sync_state=state)
        result = dl.download(f, "2022-2023/Math/Unit4Notes.pdf")
        self.assertTrue(result)
        self.assertEqual(dl.stats.synced_unchanged, 1)
        self.assertEqual(dl.stats.downloaded, 0)

    def test_skip_unsupported_mime(self):
        f = _file("x", "site", "application/vnd.google-apps.site", ROOT)
        dl = self._make_downloader()
        result = dl.download(f, "site")
        self.assertFalse(result)
        self.assertEqual(dl.stats.skipped, 1)


# ---------------------------------------------------------------
# Full Pipeline / Archiver dry-run test
# ---------------------------------------------------------------

class TestArchiverDryRun(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_output = config.OUTPUT_DIR
        config.OUTPUT_DIR = self.tmpdir
        self.service = build_mock_service()

    def tearDown(self):
        config.OUTPUT_DIR = self.orig_output
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dry_run_produces_report(self):
        archiver = Archiver(self.service, dry_run=True)
        report = archiver.run()

        self.assertIn("total_files_found", report)
        # 7 My Drive files + 2 Shared With Me files (1 in folder, 1 direct)
        self.assertEqual(report["total_files_found"], 9)
        self.assertEqual(report["dry_run"], True)
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["download_stats"]["downloaded"], 0)

    def test_dry_run_breakdown(self):
        archiver = Archiver(self.service, dry_run=True)
        report = archiver.run()
        breakdown = report["breakdown_by_year_and_subject"]

        self.assertIn("2022-2023", breakdown)
        self.assertIn("2023-2024", breakdown)

    def test_dry_run_writes_report_json(self):
        archiver = Archiver(self.service, dry_run=True)
        archiver.run()
        report_path = os.path.join(self.tmpdir, "archive_report.json")
        self.assertTrue(os.path.exists(report_path))
        with open(report_path) as fp:
            data = json.load(fp)
        self.assertEqual(data["status"], "COMPLETE")

    def test_dry_run_writes_manifest_csv(self):
        archiver = Archiver(self.service, dry_run=True)
        archiver.run()
        manifest_path = os.path.join(self.tmpdir, "manifest.csv")
        self.assertTrue(os.path.exists(manifest_path))
        with open(manifest_path) as fp:
            lines = fp.readlines()
        # header + 9 file rows (7 mine + 2 shared)
        self.assertEqual(len(lines), 10)

    def test_year_2021_file_present(self):
        archiver = Archiver(self.service, dry_run=True)
        report = archiver.run()
        breakdown = report["breakdown_by_year_and_subject"]
        all_years = list(breakdown.keys())
        self.assertTrue(any("2021" in yr for yr in all_years))

    def test_shared_with_me_count_in_report(self):
        archiver = Archiver(self.service, dry_run=True)
        report = archiver.run()
        # 2 shared files: GroupProject.pptx and SharedDirectFile.pdf
        self.assertEqual(report["shared_with_me_count"], 2)

    def test_manifest_has_shared_with_me_column(self):
        archiver = Archiver(self.service, dry_run=True)
        archiver.run()
        manifest_path = os.path.join(self.tmpdir, "manifest.csv")
        with open(manifest_path) as fp:
            header = fp.readline()
        self.assertIn("shared_with_me", header)


# ---------------------------------------------------------------
# Shared With Me Walker Tests
# ---------------------------------------------------------------

class TestSharedWithMe(unittest.TestCase):
    def setUp(self):
        self.service = build_mock_service()
        self.walker = DriveWalker(self.service)

    def test_walk_all_includes_shared_files(self):
        files = list(self.walker.walk_all())
        ids = {f["id"] for f in files}
        self.assertIn(FID_SHARED_PPT, ids)   # file inside shared folder
        self.assertIn(FID_SHARED_PDF, ids)   # file shared directly

    def test_shared_files_have_shared_with_me_flag(self):
        files = list(self.walker.walk_all())
        shared = [f for f in files if f.get("_shared_with_me")]
        self.assertGreater(len(shared), 0)

    def test_shared_files_have_correct_path_prefix(self):
        files = list(self.walker.walk_all())
        shared_pdf = next(f for f in files if f["id"] == FID_SHARED_PDF)
        self.assertIn("[Shared]", shared_pdf["full_path"])

    def test_shared_folder_contents_included(self):
        files = list(self.walker.walk_all())
        ids = {f["id"] for f in files}
        # GroupProject.pptx is inside a shared folder, not listed at top level
        self.assertIn(FID_SHARED_PPT, ids)

    def test_no_duplicates_across_my_drive_and_shared(self):
        files = list(self.walker.walk_all())
        ids = [f["id"] for f in files]
        self.assertEqual(len(ids), len(set(ids)))

    def test_total_count_with_shared(self):
        files = list(self.walker.walk_all())
        # 7 My Drive + 1 in shared folder + 1 direct share = 9
        self.assertEqual(len(files), 9)


# ---------------------------------------------------------------
# SizeEstimate Tests
# ---------------------------------------------------------------

class TestSizeEstimate(unittest.TestCase):
    def _make_file(self, fid, mime, size_bytes=None, modified="2024-01-01T00:00:00Z"):
        f = {"id": fid, "mimeType": mime, "modifiedTime": modified}
        if size_bytes is not None:
            f["size"] = str(size_bytes)
        return f

    def test_binary_files_counted_in_known_bytes(self):
        est = SizeEstimate()
        est.add(self._make_file("f1", "application/pdf", 1024 * 1024), True)
        est.add(self._make_file("f2", "image/jpeg", 512 * 1024), True)
        self.assertEqual(est.known_bytes, 1536 * 1024)
        self.assertEqual(est.new_count, 2)

    def test_google_workspace_counted_as_unknown(self):
        est = SizeEstimate()
        est.add(self._make_file("f1", "application/vnd.google-apps.document"), True)
        est.add(self._make_file("f2", "application/vnd.google-apps.spreadsheet"), True)
        self.assertEqual(est.unknown_count, 2)
        self.assertEqual(est.known_bytes, 0)

    def test_skip_mime_not_counted(self):
        est = SizeEstimate()
        est.add(self._make_file("f1", "application/vnd.google-apps.site"), True)
        self.assertEqual(est.skip_count, 1)
        self.assertEqual(est.new_count, 0)

    def test_unchanged_files_not_counted_as_new(self):
        est = SizeEstimate()
        est.add(self._make_file("f1", "application/pdf", 1024), False)
        self.assertEqual(est.unchanged_count, 1)
        self.assertEqual(est.new_count, 0)
        self.assertEqual(est.known_bytes, 0)   # unchanged → don't add to size

    def test_mixed_set(self):
        est = SizeEstimate()
        est.add(self._make_file("f1", "application/pdf", 2 * 1024 * 1024), True)
        est.add(self._make_file("f2", "application/vnd.google-apps.document"), True)
        est.add(self._make_file("f3", "application/pdf", 1024), False)   # unchanged
        est.add(self._make_file("f4", "application/vnd.google-apps.site"), True)  # skip
        self.assertEqual(est.new_count, 2)
        self.assertEqual(est.unchanged_count, 1)
        self.assertEqual(est.skip_count, 1)
        self.assertEqual(est.known_bytes, 2 * 1024 * 1024)
        self.assertEqual(est.unknown_count, 1)


# ---------------------------------------------------------------
# Archiver + Sync State integration
# ---------------------------------------------------------------

class TestArchiverSyncIntegration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_output = config.OUTPUT_DIR
        config.OUTPUT_DIR = self.tmpdir
        self.service = build_mock_service()

    def tearDown(self):
        config.OUTPUT_DIR = self.orig_output
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_first_dry_run_no_sync_state_file(self):
        archiver = Archiver(self.service, dry_run=True)
        archiver.run()
        # Dry-run should NOT write sync_state.json
        state_file = os.path.join(self.tmpdir, "sync_state.json")
        self.assertFalse(os.path.exists(state_file))

    def test_sync_state_reports_unchanged_on_second_dry_run(self):
        """
        Pre-populate sync state as if all files were already downloaded,
        then do a dry-run — the estimate should show them all as unchanged.
        """
        # Simulate a previous download: mark all known file IDs as current
        state = SyncState(self.tmpdir)
        state.load()
        all_file_ids = [
            FID_ESSAY, FID_READING, FID_NOTES, FID_RANDOM,
            FID_LAB, FID_APUSH, FID_OLDTXT,
            FID_SHARED_PPT, FID_SHARED_PDF,
        ]
        for fid in all_file_ids:
            # Use the default createdTime/modifiedTime from _file()
            state.mark_downloaded(fid, "2022-09-10T08:00:00Z", f"path/{fid}")
        state.save()

        # Reload in archiver's sync state
        archiver = Archiver(self.service, dry_run=True)
        archiver.sync_state.load()
        report = archiver.run()

        # All files should be "found" in the walk
        self.assertEqual(report["total_files_found"], 9)

    def test_report_has_synced_unchanged_key(self):
        archiver = Archiver(self.service, dry_run=True)
        report = archiver.run()
        self.assertIn("synced_unchanged", report["download_stats"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
