"""
Configuration for the Google Drive School Archiver.

Before running:
  1. Go to https://console.cloud.google.com/
  2. Create a project, enable the Google Drive API
  3. Create OAuth 2.0 credentials (Desktop App) and download as credentials.json
  4. Place credentials.json in this directory
"""

import os

# --- Auth ---
CREDENTIALS_FILE = os.getenv("GDRIVE_CREDENTIALS", "credentials.json")
TOKEN_FILE = os.getenv("GDRIVE_TOKEN", "token.json")

# Scopes: readonly is enough for archival
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

# --- Output ---
OUTPUT_DIR = os.getenv("ARCHIVE_OUTPUT_DIR", "archive_output")

# --- School Calendar ---
# School year starts in August/September. Adjust if yours differs.
SCHOOL_YEAR_START_MONTH = 8   # August

# How far back to look (years before current). Set to None for all time.
MAX_YEARS_BACK = 6

# --- Subjects / Class Keywords ---
# Each entry: (canonical_label, [keywords...])
# Matching is case-insensitive, substring match on folder/file names.
SUBJECT_KEYWORDS = [
    ("Math",         ["math", "algebra", "geometry", "calculus", "precalc",
                      "pre-calc", "statistics", "trig", "trigonometry",
                      "linear algebra", "prob and stats", "ap calc"]),
    ("English",      ["english", "ela", "lit", "literature", "writing",
                      "reading", "grammar", "ap lang", "ap lit", "comp",
                      "creative writing", "rhetoric"]),
    ("History",      ["history", "hist", "social studies", "ss", "apush",
                      "ap us history", "world history", "us history",
                      "government", "gov", "civics", "economics", "econ",
                      "ap gov", "ap econ", "ap human geo", "geography"]),
    ("Science",      ["science", "bio", "biology", "chem", "chemistry",
                      "physics", "ap bio", "ap chem", "ap physics",
                      "environmental", "earth science", "anatomy"]),
    ("Computer Science", ["cs", "computer science", "compsci", "coding",
                           "programming", "ap csp", "ap cs", "java",
                           "python class"]),
    ("Spanish",      ["spanish", "espanol", "español", "ap spanish"]),
    ("French",       ["french", "français", "ap french"]),
    ("Art",          ["art", "drawing", "painting", "sculpture", "design",
                      "photography", "ceramics", "ap art"]),
    ("Music",        ["music", "band", "orchestra", "choir", "chorus",
                      "theory", "ap music"]),
    ("PE / Health",  ["pe", "p.e.", "physical education", "health", "gym"]),
    ("Elective",     ["elective", "film", "drama", "theater", "theatre",
                      "psychology", "psych", "philosophy", "sociology"]),
]

# --- Download Formats ---
# Google Workspace MIME types → export format
GOOGLE_EXPORT_FORMATS = {
    "application/vnd.google-apps.document":     ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":  ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing":      ("application/pdf", ".pdf"),
    "application/vnd.google-apps.form":         ("application/pdf", ".pdf"),
    "application/vnd.google-apps.script":       ("application/vnd.google-apps.script+json", ".json"),
}

# These Google types are containers (folders) — skip downloading
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"

# --- Rate Limiting / Safety ---
# Delay (seconds) between API calls to avoid quota exhaustion
API_DELAY_SECONDS = 0.1

# Max files to process in one run (None = unlimited)
MAX_FILES = None

# Max total download size in MB (None = unlimited)
MAX_DOWNLOAD_MB = None

# Max API pages to fetch for file listing (None = unlimited, ~1000 files/page)
MAX_LIST_PAGES = None

# --- Logging ---
LOG_FILE = "archive_run.log"
