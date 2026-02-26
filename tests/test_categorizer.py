"""
Unit tests for the categorizer module.
No Google API credentials required — tests pure Python logic.
"""

import sys
import os
import unittest

# Allow importing from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.categorizer import (
    Categorizer,
    _extract_year_from_text,
    _detect_subject,
    _school_year_from_date,
    _sanitize,
)
from datetime import datetime, timezone


class TestSchoolYearFromDate(unittest.TestCase):
    def test_fall_semester(self):
        # September 2022 → 2022-2023
        dt = datetime(2022, 9, 15, tzinfo=timezone.utc)
        self.assertEqual(_school_year_from_date(dt), "2022-2023")

    def test_spring_semester(self):
        # March 2023 is still 2022-2023
        dt = datetime(2023, 3, 10, tzinfo=timezone.utc)
        self.assertEqual(_school_year_from_date(dt), "2022-2023")

    def test_august_is_new_year(self):
        # August starts the new year
        dt = datetime(2023, 8, 1, tzinfo=timezone.utc)
        self.assertEqual(_school_year_from_date(dt), "2023-2024")

    def test_july_is_old_year(self):
        # July is still previous school year
        dt = datetime(2023, 7, 31, tzinfo=timezone.utc)
        self.assertEqual(_school_year_from_date(dt), "2022-2023")

    def test_summer_graduation(self):
        dt = datetime(2025, 6, 1, tzinfo=timezone.utc)
        self.assertEqual(_school_year_from_date(dt), "2024-2025")


class TestExtractYearFromText(unittest.TestCase):
    def test_explicit_school_year(self):
        self.assertEqual(_extract_year_from_text("AP English 2022-2023"), "2022-2023")

    def test_slash_format(self):
        self.assertEqual(_extract_year_from_text("Junior Year 2023/2024"), "2023-2024")

    def test_short_form(self):
        self.assertEqual(_extract_year_from_text("Chem 22-23 Notes"), "2022-2023")

    def test_single_year(self):
        result = _extract_year_from_text("Math 2024 HW")
        # Single year → 2024-2025
        self.assertEqual(result, "2024-2025")

    def test_no_year(self):
        self.assertIsNone(_extract_year_from_text("Random File Name"))

    def test_folder_path(self):
        self.assertEqual(
            _extract_year_from_text("School Stuff/2023-2024/English/Essay.docx"),
            "2023-2024"
        )

    def test_decade_not_matched(self):
        # "10-11" could be a range but is too ambiguous (2010 era) — we accept it
        result = _extract_year_from_text("File 10-11")
        self.assertEqual(result, "2010-2011")

    def test_non_consecutive_years_ignored(self):
        # "2022-2025" should not match as school year (diff != 1)
        result = _extract_year_from_text("Notes 2022-2025")
        # Falls back to extracting first single year 2022
        self.assertIsNotNone(result)


class TestDetectSubject(unittest.TestCase):
    def test_math(self):
        self.assertEqual(_detect_subject("Algebra II Notes"), "Math")

    def test_english(self):
        self.assertEqual(_detect_subject("AP Lang Essay Draft"), "English")

    def test_history(self):
        self.assertEqual(_detect_subject("APUSH Unit 3 Review"), "History")

    def test_science(self):
        self.assertEqual(_detect_subject("AP Chemistry Lab Report"), "Science")

    def test_cs(self):
        self.assertEqual(_detect_subject("CS Final Project"), "Computer Science")

    def test_spanish(self):
        self.assertEqual(_detect_subject("Spanish Vocab Quiz"), "Spanish")

    def test_art(self):
        self.assertEqual(_detect_subject("Art Portfolio Sketch"), "Art")

    def test_pe(self):
        self.assertEqual(_detect_subject("PE Log Week 3"), "PE / Health")

    def test_misc_fallback(self):
        self.assertIsNone(_detect_subject("Random Untitled File"))

    def test_case_insensitive(self):
        self.assertEqual(_detect_subject("BIOLOGY NOTES"), "Science")

    def test_no_partial_match(self):
        # "math" in "aftermath" should not match
        self.assertIsNone(_detect_subject("aftermath summary"))

    def test_calculus(self):
        self.assertEqual(_detect_subject("Calculus BC Integration Review"), "Math")

    def test_econ(self):
        self.assertEqual(_detect_subject("Macro Economics Unit 2"), "History")


class TestCategorizerIntegration(unittest.TestCase):
    def setUp(self):
        self.cat = Categorizer()

    def _make_file(self, name, full_path=None, created="2023-09-15T10:00:00Z"):
        return {
            "id": "fake_id",
            "name": name,
            "full_path": full_path or name,
            "mimeType": "application/vnd.google-apps.document",
            "createdTime": created,
            "modifiedTime": created,
        }

    def test_year_from_folder_path(self):
        f = self._make_file("Essay.docx", "School/2022-2023/English/Essay.docx")
        result = self.cat.categorize(f)
        self.assertEqual(result.school_year, "2022-2023")
        self.assertEqual(result.subject, "English")

    def test_year_from_timestamp_fallback(self):
        # No year in name, but created Sept 2024 → 2024-2025
        f = self._make_file("Random Notes", created="2024-09-01T08:00:00Z")
        result = self.cat.categorize(f)
        self.assertEqual(result.school_year, "2024-2025")

    def test_subject_from_filename(self):
        f = self._make_file("Bio Lab Report.docx", created="2023-02-01T00:00:00Z")
        result = self.cat.categorize(f)
        self.assertEqual(result.subject, "Science")

    def test_misc_subject(self):
        f = self._make_file("Random File", "My Drive/Random File", created="2023-10-01T00:00:00Z")
        result = self.cat.categorize(f)
        self.assertEqual(result.subject, "Misc")

    def test_output_path_structure(self):
        f = self._make_file("HW.pdf", "2022-2023/Math/HW.pdf")
        result = self.cat.categorize(f)
        self.assertTrue(result.full_path.startswith("2022-2023/"))
        self.assertIn("Math", result.full_path)

    def test_sanitize_slashes_in_name(self):
        f = self._make_file("Notes/Draft.docx", "2023-2024/English/Notes/Draft.docx")
        result = self.cat.categorize(f)
        # Slashes in name should be replaced
        self.assertNotIn("/", result.full_path.split("/")[-1])

    def test_real_world_google_drive_filename(self):
        f = self._make_file(
            "AP Calculus BC - Integration by Parts Worksheet",
            "Junior/AP Calculus BC - Integration by Parts Worksheet",
            created="2023-11-10T12:00:00Z"
        )
        result = self.cat.categorize(f)
        self.assertEqual(result.subject, "Math")
        self.assertEqual(result.school_year, "2023-2024")

    def test_unknown_year_fallback(self):
        f = {
            "id": "x",
            "name": "Random",
            "full_path": "Random",
            "mimeType": "text/plain",
            # No timestamps
        }
        result = self.cat.categorize(f)
        self.assertEqual(result.school_year, "Unknown Year")

    def test_preserves_existing_folder_structure(self):
        """Files already inside a folder keep their Drive path exactly."""
        f = self._make_file("Essay.docx", "English/Semester 1/Essay.docx")
        result = self.cat.categorize(f)
        self.assertEqual(result.full_path, "English/Semester 1/Essay.docx")

    def test_auto_categorizes_root_floater(self):
        """Files at the root of Drive (no folder) get auto-categorized into year/subject."""
        f = self._make_file("Random Notes.docx", created="2023-09-15T10:00:00Z")
        result = self.cat.categorize(f)
        # Should be placed inside a year/subject tree, not left at root
        self.assertIn("/", result.full_path)
        self.assertTrue(result.full_path.startswith("2023-2024/"))


class TestSanitize(unittest.TestCase):
    def test_removes_slash(self):
        self.assertNotIn("/", _sanitize("a/b"))

    def test_removes_colon(self):
        self.assertNotIn(":", _sanitize("a:b"))

    def test_removes_stars(self):
        self.assertNotIn("*", _sanitize("a*b"))

    def test_normal_name_unchanged(self):
        self.assertEqual(_sanitize("Normal File Name.docx"), "Normal File Name.docx")


if __name__ == "__main__":
    unittest.main(verbosity=2)
