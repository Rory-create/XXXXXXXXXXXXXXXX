"""
Categorize Google Drive files into:
  - School year  (e.g. "2022-2023")
  - Class/subject (e.g. "English", "Math")
  - Misc bucket

Uses:
  - File/folder path components
  - File name keywords
  - Creation/modification timestamps
"""

import re
import logging
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)

# Regex patterns for year detection in names/paths
_YEAR_4D = re.compile(r"\b(20[12]\d)\b")                       # bare 4-digit year
_SCHOOL_YEAR_SLASH = re.compile(r"\b(20[12]\d)[/-](20[12]\d)\b")  # 2022-2023 or 2022/2023
_YEAR_SHORT = re.compile(r"\b(\d{2})[/-](\d{2})\b")            # 22-23 style


def _school_year_from_date(dt: datetime) -> str:
    """
    Convert a datetime to a 'YYYY-YYYY' school-year string.
    School year starts in August (config.SCHOOL_YEAR_START_MONTH).
    Example: August 2022 → June 2023 → "2022-2023"
    """
    if dt.month >= config.SCHOOL_YEAR_START_MONTH:
        return f"{dt.year}-{dt.year + 1}"
    return f"{dt.year - 1}-{dt.year}"


def _extract_year_from_text(text: str) -> Optional[str]:
    """Try to extract a school-year string from arbitrary text."""
    # Explicit school year e.g. "2022-2023"
    m = _SCHOOL_YEAR_SLASH.search(text)
    if m:
        y1, y2 = int(m.group(1)), int(m.group(2))
        if abs(y2 - y1) == 1:
            return f"{y1}-{y2}"

    # Short form e.g. "22-23"
    m = _YEAR_SHORT.search(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if abs(b - a) == 1:
            base = 2000 + a
            return f"{base}-{base + 1}"

    # Single 4-digit year → infer school year
    m = _YEAR_4D.search(text)
    if m:
        year = int(m.group(1))
        # Ambiguous: pick year as start of school year
        return f"{year}-{year + 1}"

    return None


def _detect_subject(text: str) -> Optional[str]:
    """
    Return the canonical subject label if any keyword matches (case-insensitive).
    Checks each word boundary so 'math' doesn't match 'aftermath'.
    """
    lower = text.lower()
    for label, keywords in config.SUBJECT_KEYWORDS:
        for kw in keywords:
            # Word-boundary match: wrap keyword in \\b only if it's all word chars
            if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", lower):
                return label
    return None


class FileCategory:
    """Holds the categorization result for a single file."""
    __slots__ = ("school_year", "subject", "full_path")

    def __init__(self, school_year: str, subject: str, full_path: str):
        self.school_year = school_year
        self.subject = subject
        self.full_path = full_path   # relative output path

    def __repr__(self):
        return f"FileCategory(year={self.school_year!r}, subject={self.subject!r})"


class Categorizer:
    """
    Assigns each Drive file metadata dict a FileCategory.
    """

    def categorize(self, file_meta: dict) -> FileCategory:
        """
        Determine school_year and subject from file metadata.

        Priority for school year:
          1. Explicit year in folder path
          2. Explicit year in file name
          3. Creation date
          4. Modification date
          5. Fallback: "Unknown Year"

        Priority for subject:
          1. Folder path keywords
          2. File name keywords
          3. Fallback: "Misc"

        Output path:
          - Files already inside a folder: preserve their existing Drive path exactly.
          - Files floating at the root of Drive: auto-categorize into year/subject/.
        """
        name = file_meta.get("name", "")
        full_path = file_meta.get("full_path", name)

        # --- School Year ---
        school_year = (
            _extract_year_from_text(full_path)
            or self._year_from_timestamps(file_meta)
            or "Unknown Year"
        )

        # --- Subject ---
        subject = (
            _detect_subject(full_path)
            or _detect_subject(name)
            or "Misc"
        )

        # --- Build output relative path ---
        # A file with more than one path component is already inside a folder.
        # Preserve that structure; only sanitize the filename component.
        # A file with a single-component path is floating at root — auto-categorize.
        path_parts = [p for p in full_path.replace("\\", "/").split("/") if p]
        if len(path_parts) > 1:
            safe_name = _sanitize(path_parts[-1])
            out_path = "/".join(path_parts[:-1]) + "/" + safe_name
        else:
            safe_name = _sanitize(name)
            out_path = f"{school_year}/{subject}/{safe_name}"

        return FileCategory(school_year=school_year, subject=subject, full_path=out_path)

    # ------------------------------------------------------------------

    @staticmethod
    def _year_from_timestamps(file_meta: dict) -> Optional[str]:
        for key in ("createdTime", "modifiedTime"):
            raw = file_meta.get(key)
            if raw:
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    return _school_year_from_date(dt)
                except ValueError:
                    pass
        return None


def _sanitize(name: str) -> str:
    """Remove/replace filesystem-unsafe characters from a name."""
    # Replace slashes, null bytes, colons
    return re.sub(r'[/\\:*?"<>|\x00]', "_", name).strip()
