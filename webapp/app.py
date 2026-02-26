"""
webapp/app.py — Flask web app for the School Drive Archiver.

Routes
------
GET  /              Landing page: enter access code + pick destination
POST /auth/start    Validate code → redirect to Google OAuth
GET  /auth/callback OAuth callback → kick off background archive worker
GET  /scanning      Progress page (shown while worker runs)
GET  /progress      SSE stream of worker progress events
GET  /done          Result page (Drive link or ZIP download button)
GET  /download      Stream the finished ZIP to the browser
GET  /logout        Clear session and start over
"""

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile

from flask import (
    Flask,
    Response,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import google.oauth2.credentials
from werkzeug.middleware.proxy_fix import ProxyFix

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.categorizer import Categorizer
from src.downloader import Downloader
from src.drive_walker import DriveWalker
from src.sync import SyncState

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
# Trust X-Forwarded-Proto from Railway's reverse proxy so request.url is https://
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ["SECRET_KEY"]

SCOPES = ["https://www.googleapis.com/auth/drive"]

CLIENT_CONFIG = {
    "web": {
        "client_id": os.environ.get("CLIENT_ID", ""),
        "client_secret": os.environ.get("CLIENT_SECRET", ""),
        "redirect_uris": [os.environ.get("REDIRECT_URI", "http://localhost:5000/auth/callback")],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

# ── Access codes ──────────────────────────────────────────────────────────────
CODES_FILE = os.path.join(ROOT, "codes.json")
_codes_lock = threading.Lock()


def _load_codes() -> dict:
    if not os.path.exists(CODES_FILE):
        return {"unused": [], "used": []}
    with open(CODES_FILE) as f:
        return json.load(f)


def _save_codes(data: dict) -> None:
    with open(CODES_FILE, "w") as f:
        json.dump(data, f, indent=2)


def validate_and_use_code(code: str) -> bool:
    """Atomically move code from unused → used. Returns True if valid."""
    with _codes_lock:
        data = _load_codes()
        if code in data.get("unused", []):
            data["unused"].remove(code)
            data.setdefault("used", []).append(code)
            _save_codes(data)
            return True
    return False


# ── Per-session progress queues and results ───────────────────────────────────
_progress: dict[str, queue.Queue] = {}
_results: dict[str, dict] = {}
_store_lock = threading.Lock()


def _make_queue(sid: str) -> queue.Queue:
    q: queue.Queue = queue.Queue()
    with _store_lock:
        _progress[sid] = q
    return q


def _get_queue(sid: str) -> "queue.Queue | None":
    with _store_lock:
        return _progress.get(sid)


def _drop_queue(sid: str) -> None:
    with _store_lock:
        _progress.pop(sid, None)


def _store_result(sid: str, result: dict) -> None:
    with _store_lock:
        _results[sid] = result


def _get_result(sid: str) -> dict:
    with _store_lock:
        return _results.get(sid, {})


# ── OAuth helpers ─────────────────────────────────────────────────────────────
def _make_flow(state=None) -> Flow:
    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, state=state)
    flow.redirect_uri = os.environ.get("REDIRECT_URI", "http://localhost:5000/auth/callback")
    return flow


def _creds_to_dict(creds) -> dict:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }


def _dict_to_creds(d: dict):
    return google.oauth2.credentials.Credentials(
        token=d["token"],
        refresh_token=d.get("refresh_token"),
        token_uri=d["token_uri"],
        client_id=d["client_id"],
        client_secret=d["client_secret"],
        scopes=d.get("scopes"),
    )


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/auth/start", methods=["POST"])
def auth_start():
    code = request.form.get("access_code", "").strip()
    destination = request.form.get("destination", "drive")

    if not validate_and_use_code(code):
        return render_template(
            "index.html",
            error="That code is invalid or has already been used. Double-check and try again.",
        )

    session["destination"] = destination
    flow = _make_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    state = session.get("oauth_state")
    try:
        flow = _make_flow(state=state)
        flow.fetch_token(authorization_response=request.url)
    except Exception as exc:
        return render_template("error.html", message=f"Google sign-in failed: {exc}")

    creds = flow.credentials
    session["credentials"] = _creds_to_dict(creds)

    sid = str(uuid.uuid4())
    session["sid"] = sid

    q = _make_queue(sid)
    destination = session.get("destination", "drive")
    creds_dict = dict(session["credentials"])

    t = threading.Thread(
        target=_archive_worker,
        args=(sid, creds_dict, destination, q),
        daemon=True,
    )
    t.start()
    return redirect(url_for("scanning"))


@app.route("/scanning")
def scanning():
    if "sid" not in session:
        return redirect(url_for("index"))
    destination = session.get("destination", "drive")
    return render_template("scanning.html", destination=destination)


@app.route("/progress")
def progress():
    sid = session.get("sid")

    def generate():
        if not sid:
            yield "data: {}\n\n"
            return
        q = _get_queue(sid)
        if q is None:
            # Worker may have already finished
            result = _get_result(sid)
            if result:
                yield f"data: {json.dumps(result)}\n\n"
            return
        while True:
            try:
                event = q.get(timeout=25)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("phase") in ("done", "error"):
                _drop_queue(sid)
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering on Railway
        },
    )


@app.route("/done")
def done():
    sid = session.get("sid")
    if not sid:
        return redirect(url_for("index"))
    result = _get_result(sid)
    destination = session.get("destination", "drive")
    if result.get("phase") == "error":
        return render_template("error.html", message=result.get("msg", "An unknown error occurred."))
    if destination == "zip":
        return render_template("done_zip.html")
    else:
        return render_template("done_drive.html", folder_url=result.get("folder_url", "#"))


@app.route("/download")
def download():
    sid = session.get("sid")
    result = _get_result(sid) if sid else {}
    zip_path = result.get("zip_path")
    tmpdir = result.get("tmpdir")

    if not zip_path or not os.path.exists(zip_path):
        return render_template("error.html", message="Archive file not found. Please start over.")

    def stream_and_cleanup():
        try:
            with open(zip_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            if tmpdir and os.path.isdir(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
            with _store_lock:
                _results.pop(sid, None)

    return Response(
        stream_with_context(stream_and_cleanup()),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=school_archive.zip"},
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ── Background worker ─────────────────────────────────────────────────────────
def _archive_worker(sid: str, creds_dict: dict, destination: str, q: "queue.Queue") -> None:
    try:
        creds = _dict_to_creds(creds_dict)
        service = build("drive", "v3", credentials=creds)
        walker = DriveWalker(service)
        cat = Categorizer()

        # Phase 1 — walk Drive
        q.put({"phase": "walking", "pct": 5, "msg": "Scanning your Google Drive…"})
        all_files: list[dict] = []
        for fm in walker.walk_all():
            all_files.append(fm)
            if len(all_files) % 50 == 0:
                q.put({"phase": "walking", "pct": 5, "msg": f"Found {len(all_files)} files so far…"})

        total = len(all_files)
        q.put({"phase": "downloading", "pct": 10, "msg": f"Organizing {total} files…"})

        if destination == "zip":
            _worker_zip(service, cat, all_files, total, sid, q)
        else:
            _worker_drive(service, cat, all_files, total, sid, q)

    except Exception as exc:
        result = {"phase": "error", "pct": 0, "msg": str(exc)}
        _store_result(sid, result)
        q.put(result)


def _worker_zip(service, cat, all_files, total, sid, q):
    tmpdir = tempfile.mkdtemp(prefix="schoolarchive_")
    try:
        sync_state = SyncState(tmpdir)
        downloader = Downloader(service, tmpdir, sync_state)

        for i, fm in enumerate(all_files):
            category = cat.categorize(fm)
            try:
                downloader.download(fm, category.full_path)
            except Exception:
                pass
            pct = 10 + int((i + 1) / total * 78) if total else 88
            q.put({"phase": "downloading", "pct": pct, "msg": f"Downloading: {fm['name'][:55]}"})

        q.put({"phase": "zipping", "pct": 90, "msg": "Bundling everything into a ZIP…"})
        zip_path = os.path.join(tmpdir, "school_archive.zip")
        _build_zip(tmpdir, zip_path)

        result = {"phase": "done", "pct": 100, "msg": "Your archive is ready!", "zip_path": zip_path, "tmpdir": tmpdir}
        _store_result(sid, result)
        q.put({"phase": "done", "pct": 100, "msg": "Your archive is ready!"})

    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        result = {"phase": "error", "pct": 0, "msg": str(exc)}
        _store_result(sid, result)
        q.put(result)


def _worker_drive(service, cat, all_files, total, sid, q):
    archive_date = time.strftime("%Y-%m-%d")
    folder_name = f"School Archive — {archive_date}"

    # Create the root archive folder in the user's Drive
    root_meta = service.files().create(
        body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id,webViewLink",
    ).execute()
    root_id = root_meta["id"]
    folder_url = root_meta["webViewLink"]
    folder_cache: dict[str, str] = {}

    for i, fm in enumerate(all_files):
        category = cat.categorize(fm)
        pct = 10 + int((i + 1) / total * 85) if total else 95
        try:
            dir_path = "/".join(category.full_path.split("/")[:-1])
            parent_id = _ensure_drive_folder(service, root_id, dir_path, folder_cache)
            service.files().copy(
                fileId=fm["id"],
                body={"name": fm["name"], "parents": [parent_id]},
            ).execute()
        except Exception:
            pass  # Skip files we can't copy (viewer-only shared files, shortcuts, etc.)
        q.put({"phase": "downloading", "pct": pct, "msg": f"Saving: {fm['name'][:55]}"})

    result = {"phase": "done", "pct": 100, "msg": "Saved to Google Drive!", "folder_url": folder_url}
    _store_result(sid, result)
    q.put({"phase": "done", "pct": 100, "msg": "Saved to Google Drive!", "folder_url": folder_url})


def _ensure_drive_folder(service, root_id: str, path: str, cache: dict) -> str:
    """Recursively ensure all folders in `path` exist under root_id. Returns leaf folder ID."""
    if not path:
        return root_id
    if path in cache:
        return cache[path]

    parts = [p for p in path.split("/") if p]
    current_id = root_id
    built = ""
    for part in parts:
        built = (built + "/" + part).lstrip("/")
        if built in cache:
            current_id = cache[built]
            continue
        meta = service.files().create(
            body={
                "name": part,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [current_id],
            },
            fields="id",
        ).execute()
        current_id = meta["id"]
        cache[built] = current_id
    return current_id


def _build_zip(source_dir: str, zip_path: str) -> None:
    skip = {os.path.basename(zip_path), "sync_state.json"}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _, filenames in os.walk(source_dir):
            for fname in filenames:
                if fname in skip:
                    continue
                fpath = os.path.join(dirpath, fname)
                arcname = os.path.relpath(fpath, source_dir)
                zf.write(fpath, arcname)


# ── Dev entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    app.run(debug=True, threaded=True, port=5000)
