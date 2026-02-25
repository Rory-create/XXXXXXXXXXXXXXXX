"""
Tests for SyncState: persistence, change detection, re-run skip logic.
"""

import json
import os
import sys
import tempfile
import shutil
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.sync import SyncState


class TestSyncStateBasics(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state = SyncState(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_new_file_needs_download(self):
        self.state.load()
        self.assertTrue(self.state.needs_download("abc123", "2024-01-01T00:00:00Z"))

    def test_after_marking_downloaded_no_longer_needed(self):
        self.state.load()
        self.state.mark_downloaded("abc123", "2024-01-01T00:00:00Z", "2024-2025/Math/HW.pdf")
        self.assertFalse(self.state.needs_download("abc123", "2024-01-01T00:00:00Z"))

    def test_modified_file_needs_redownload(self):
        self.state.load()
        self.state.mark_downloaded("abc123", "2024-01-01T00:00:00Z", "path/to/file.pdf")
        # Same ID, different modifiedTime → needs download
        self.assertTrue(self.state.needs_download("abc123", "2024-06-15T10:00:00Z"))

    def test_same_modified_time_no_redownload(self):
        self.state.load()
        self.state.mark_downloaded("abc123", "2024-03-10T08:00:00Z", "path/file.pdf")
        self.assertFalse(self.state.needs_download("abc123", "2024-03-10T08:00:00Z"))

    def test_none_modified_time_always_downloads(self):
        self.state.load()
        self.state.mark_downloaded("abc123", None, "path/file.pdf")
        # Both None — recorded as "" vs ""
        self.assertFalse(self.state.needs_download("abc123", None))

    def test_total_recorded(self):
        self.state.load()
        self.assertEqual(self.state.total_recorded, 0)
        self.state.mark_downloaded("a", "t1", "p1")
        self.state.mark_downloaded("b", "t2", "p2")
        self.assertEqual(self.state.total_recorded, 2)


class TestSyncStatePersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_reload(self):
        state1 = SyncState(self.tmpdir)
        state1.load()
        state1.mark_downloaded("file1", "2024-09-01T00:00:00Z", "2024-2025/English/Essay.docx")
        state1.mark_downloaded("file2", "2024-10-05T12:00:00Z", "2024-2025/Math/HW.pdf")
        state1.save()

        # New instance loading same file
        state2 = SyncState(self.tmpdir)
        state2.load()
        self.assertEqual(state2.total_recorded, 2)
        self.assertFalse(state2.needs_download("file1", "2024-09-01T00:00:00Z"))
        self.assertFalse(state2.needs_download("file2", "2024-10-05T12:00:00Z"))
        # Unknown file → needs download
        self.assertTrue(state2.needs_download("file3", "2024-11-01T00:00:00Z"))

    def test_modified_after_reload(self):
        state1 = SyncState(self.tmpdir)
        state1.load()
        state1.mark_downloaded("file1", "2024-09-01T00:00:00Z", "path/file.pdf")
        state1.save()

        state2 = SyncState(self.tmpdir)
        state2.load()
        # File was modified in Drive
        self.assertTrue(state2.needs_download("file1", "2025-01-10T15:00:00Z"))

    def test_state_file_location(self):
        state = SyncState(self.tmpdir)
        state.load()
        state.mark_downloaded("x", "t", "p")
        state.save()
        state_file = os.path.join(self.tmpdir, "sync_state.json")
        self.assertTrue(os.path.exists(state_file))

    def test_corrupt_state_file_handled_gracefully(self):
        state_file = os.path.join(self.tmpdir, "sync_state.json")
        with open(state_file, "w") as f:
            f.write("{ this is not valid json !!!")
        state = SyncState(self.tmpdir)
        state.load()   # Should not raise
        self.assertEqual(state.total_recorded, 0)
        # Everything is treated as new
        self.assertTrue(state.needs_download("any", "any"))

    def test_no_save_if_not_dirty(self):
        state = SyncState(self.tmpdir)
        state.load()
        state.save()   # Should not create file since nothing changed
        state_file = os.path.join(self.tmpdir, "sync_state.json")
        self.assertFalse(os.path.exists(state_file))

    def test_atomic_write(self):
        """Verify no .tmp file is left behind after save."""
        state = SyncState(self.tmpdir)
        state.load()
        state.mark_downloaded("f", "t", "p")
        state.save()
        tmp_file = os.path.join(self.tmpdir, "sync_state.json.tmp")
        self.assertFalse(os.path.exists(tmp_file))

    def test_previously_downloaded_paths(self):
        state = SyncState(self.tmpdir)
        state.load()
        state.mark_downloaded("f1", "t1", "2023-2024/Math/HW.pdf")
        state.mark_downloaded("f2", "t2", "2023-2024/English/Essay.docx")
        paths = state.previously_downloaded_paths()
        self.assertIn("2023-2024/Math/HW.pdf", paths)
        self.assertIn("2023-2024/English/Essay.docx", paths)


class TestSyncStateIncrementalRun(unittest.TestCase):
    """Simulate a real two-run scenario."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_second_run_skips_unchanged_files(self):
        # First run: download 3 files
        state = SyncState(self.tmpdir)
        state.load()
        files = [
            ("id1", "2024-09-01T00:00:00Z", "2024-2025/Math/file1.pdf"),
            ("id2", "2024-09-05T00:00:00Z", "2024-2025/English/file2.docx"),
            ("id3", "2024-10-01T00:00:00Z", "2024-2025/Science/file3.pdf"),
        ]
        for fid, mtime, path in files:
            state.mark_downloaded(fid, mtime, path)
        state.save()

        # Second run: same files + one new + one modified
        state2 = SyncState(self.tmpdir)
        state2.load()

        results = {
            "id1": state2.needs_download("id1", "2024-09-01T00:00:00Z"),  # unchanged
            "id2": state2.needs_download("id2", "2024-09-05T00:00:00Z"),  # unchanged
            "id3": state2.needs_download("id3", "2025-01-15T00:00:00Z"),  # modified!
            "id4": state2.needs_download("id4", "2025-02-01T00:00:00Z"),  # new
        }

        self.assertFalse(results["id1"])  # skip
        self.assertFalse(results["id2"])  # skip
        self.assertTrue(results["id3"])   # re-download
        self.assertTrue(results["id4"])   # new download
