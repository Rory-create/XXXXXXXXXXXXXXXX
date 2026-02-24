# Google Drive School Archiver

Downloads and organizes your entire school Google Drive, sorting files by school year, subject, and misc.

## Output Structure

```
archive_output/
  2022-2023/
    English/
      Essay1.docx
      ReadingLog.xlsx
    Math/
      Unit4Notes.pdf
    Misc/
      ...
  2023-2024/
    Science/
      LabReport.docx
    History/
      Essay_APUSH.docx
  Unknown Year/
    Misc/
      ...
  archive_report.json   ← full summary
  manifest.csv          ← one row per file
```

## Setup (one-time)

### 1. Get Google Drive API credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Create a project (or pick an existing one)
3. Enable the **Google Drive API**: APIs & Services → Library → search "Drive"
4. Create credentials: APIs & Services → Credentials → **Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Desktop App**
5. Download the JSON → rename it **`credentials.json`** → put it in this folder

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. First run — authorize

```bash
python main.py --dry-run
```

A browser window will open asking you to sign in with your school Google account and grant access. After authorizing, a `token.json` is saved so you won't need to do this again.

`--dry-run` walks all your files and categorizes them **without downloading anything** — great to preview the breakdown before committing to a full download.

## Running a full archive

```bash
# Download everything
python main.py

# Limit size (safe for testing)
python main.py --max-files 100 --max-mb 500

# Custom output folder
python main.py --output-dir ~/my_school_archive

# Verbose logging
python main.py --log-level DEBUG
```

## Categorization logic

### School year
Files are assigned to a school year (`YYYY-YYYY`) using this priority:
1. Year explicitly in the folder path (`2022-2023/` or `22-23/`)
2. Year in the file name
3. File creation date (Aug–Jul window)
4. File modification date
5. `Unknown Year` if nothing matches

### Subject/class
Folder and file names are checked against a keyword list (configurable in `config.py`):
Math, English, History, Science, Computer Science, Spanish, French, Art, Music, PE/Health, Elective.
Anything that doesn't match → **Misc**.

### Google Workspace files
| Drive Type | Downloaded As |
|------------|--------------|
| Google Doc | `.docx` |
| Google Sheet | `.xlsx` |
| Google Slides | `.pptx` |
| Google Drawing / Form | `.pdf` |
| Google Apps Script | `.json` |
| Everything else | original format |

## Files

```
main.py            Entry point / CLI
config.py          All tuneable settings
src/
  auth.py          OAuth2 authentication
  drive_walker.py  Recursive Drive file lister
  categorizer.py   Year + subject detection
  downloader.py    File download / export
  archiver.py      Orchestrator + report writer
tests/
  test_categorizer.py   38 unit tests (no credentials needed)
  test_mock_drive.py    20 integration tests (mocked Drive API)
```

## Running tests

```bash
python -m pytest tests/ -v
```

No credentials or internet needed — tests use a fully mocked Drive.

## Tips

- Run with `--dry-run` first to see the breakdown by year/subject before downloading
- The `manifest.csv` has every file's Drive ID, detected year, subject, and local path — useful if you want to manually re-categorize anything
- Add custom class names to `SUBJECT_KEYWORDS` in `config.py` for better matching
- Files are **never overwritten** on re-run — safe to run multiple times
- Ctrl-C gracefully stops the run and writes a partial report
