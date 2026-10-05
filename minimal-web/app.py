"""A small, local-network print interface. CUPS owns drivers and the queue."""
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import cups
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

import documents
import printers

STATE = Path(os.environ.get("STATE_DIR", "/data"))
UPLOADS = STATE / "uploads"
JOBS = STATE / "jobs"
for directory in (STATE, UPLOADS, JOBS):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
password_path = STATE / "admin-password"
if not password_path.exists():
    password_path.write_text(secrets.token_urlsafe(24) + "\n")
    password_path.chmod(0o600)
ADMIN_PASSWORD = password_path.read_text().strip()
RETENTION = int(os.environ.get("RETENTION_DAYS", 7)) * 86400
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# Only these options change PDF geometry. Bump the version when rendering changes.
RENDER_VERSION = 1
LAYOUT_DEFAULTS = {"number-up": "1", "number-up-layout": "lrtb", "page-set": "all",
                   "outputorder": "normal", "page-border": "none", "page-ranges": "",
                   "orientation-requested": "3"}


def write_json(path, value):
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    temp.write_text(json.dumps(value))
    temp.replace(path)


def settings():
    path = STATE / "layouts.json"
    return json.loads(path.read_text()) if path.exists() else {}


def admin():
    auth = request.authorization
    if not auth or auth.username != "admin" or not hmac.compare_digest(auth.password or "", ADMIN_PASSWORD):
        raise PermissionError("Enter the administrator password to change printer defaults or manage other users' jobs.")


@app.before_request
def same_origin():
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin")
        if request.headers.get("X-Print-UI") != "1" or request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(error="Use this page to submit changes."), 403
        if origin and urlparse(origin).netloc != request.host:
            return jsonify(error="Cross-origin requests are not permitted."), 403


@app.after_request
def response_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(Exception)
def error(exc):
    if isinstance(exc, HTTPException):
        return jsonify(error=exc.description), exc.code
    if isinstance(exc, PermissionError):
        return jsonify(error=str(exc)), 401
    if isinstance(exc, (ValueError, TypeError)):
        return jsonify(error=str(exc)), 400
    if isinstance(exc, cups.IPPError):
        return jsonify(error="CUPS: " + str(exc.args[-1])), 502
    if isinstance(exc, cups.HTTPError):
        return jsonify(error="CUPS is unavailable or refused this operation."), 502
    app.logger.exception("Request failed")
    return jsonify(error="The request failed. Check the server log."), 500


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    printers.connection().getPrinters()
    return jsonify(status="ok")


@app.get("/api/printers")
def printer_list():
    conn = printers.connection()
    return jsonify(default=conn.getDefault(), printers=[{"name": name, "info": attrs.get("printer-info", name),
                   "state": attrs.get("printer-state", 3), "accepting": attrs.get("printer-is-accepting-jobs", True)}
                   for name, attrs in sorted(conn.getPrinters().items())])


@app.get("/api/printers/<name>")
def printer_controls(name):
    result = printers.capabilities(name)
    result["layout"] = documents.layout_settings(settings().get(name, {}))
    return jsonify(result)


@app.post("/api/uploads")
def upload():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        raise ValueError("Select a document first.")
    identifier = secrets.token_hex(16)
    folder = UPLOADS / identifier
    folder.mkdir(mode=0o700)
    path = folder / "input"
    uploaded.save(path)
    name = uploaded.filename.replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if ord(c) >= 32)[:200] or "Document"
    try:
        meta = documents.normalize(path, name)
    except (RuntimeError, OSError) as exc:
        shutil.rmtree(folder)
        raise ValueError("This file could not be read as a PDF or image.") from exc
    except Exception:
        shutil.rmtree(folder)
        raise
    path.unlink()
    meta.update(id=identifier, name=name, created=time.time())
    write_json(folder / "meta.json", meta)
    # A small MVP needs no database or background retention service.
    for old in UPLOADS.iterdir():
        old_meta = old / "meta.json"
        if old_meta.exists() and json.loads(old_meta.read_text()).get("created", time.time()) < time.time() - RETENTION:
            shutil.rmtree(old, ignore_errors=True)
    for old in JOBS.glob("*.json"):
        if old.stat().st_mtime < time.time() - RETENTION:
            old.unlink(missing_ok=True)
    return jsonify(meta), 201


def uploaded_document(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Invalid upload identifier.")
    folder = UPLOADS / identifier
    if not (folder / "meta.json").exists():
        raise ValueError("This upload has expired. Upload the document again.")
    return folder, json.loads((folder / "meta.json").read_text())


def preparation_key(size, options, layout):
    geometry = {key: options.get(key, default) for key, default in LAYOUT_DEFAULTS.items()}
    return hashlib.sha256(json.dumps([RENDER_VERSION, size, geometry, layout], sort_keys=True).encode()).hexdigest()


def print_document(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a document and print options.")
    folder, meta = uploaded_document(payload.get("upload"))
    caps, options = printers.validated_options(payload.get("printer"), payload.get("options", {}))
    layout = documents.layout_settings(payload.get("layout", {}))
    size = printers.paper_size(caps, options)
    digest = hashlib.sha256(json.dumps([caps["name"], options, layout, size], sort_keys=True).encode()).hexdigest()
    path = folder / (digest + ".pdf")
    if not path.exists():
        key = preparation_key(size, options, layout)
        prepared = folder / ("prepared-" + key + ".pdf")
        with (folder / ("prepared-" + key + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not prepared.exists():
                temp = folder / (secrets.token_hex(8) + ".tmp.pdf")
                try:
                    documents.prepare(folder / "source.pdf", temp, size, options, layout)
                    temp.replace(prepared)
                finally:
                    temp.unlink(missing_ok=True)
            try:
                os.link(prepared, path)
            except FileExistsError:
                pass
    return caps, options, layout, meta, path, digest


@app.post("/api/preview")
def preview():
    payload = request.get_json()
    caps, options, layout, meta, path, digest = print_document(payload)
    page = int(payload.get("page", 1))
    mode = printers.preview_color_mode(caps, options)
    prepared = preparation_key(printers.paper_size(caps, options), options, layout)
    key = hashlib.sha256(json.dumps([prepared, page, mode]).encode()).hexdigest()
    cached = path.parent / ("preview-" + key + ".json")
    with cached.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if cached.exists():
            result = json.loads(cached.read_text())
        else:
            image, count = documents.preview(path, page, mode)
            result = {"image": "data:image/png;base64," + base64.b64encode(image).decode(), "pages": count}
            write_json(cached, result)
    return jsonify(**result, document=f"/api/files/{meta['id']}/{digest}.pdf")


@app.get("/api/files/<identifier>/<digest>.pdf")
def download(identifier, digest):
    folder, _ = uploaded_document(identifier)
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or not (folder / (digest + ".pdf")).is_file():
        raise ValueError("The print PDF is no longer available.")
    return send_file(folder / (digest + ".pdf"), mimetype="application/pdf", as_attachment=True, download_name="print.pdf")


@app.post("/api/print")
def print_job():
    payload = request.get_json()
    caps, options, layout, meta, path, digest = print_document(payload)
    submission = payload.get("submission")
    if not isinstance(submission, str) or not re.fullmatch(r"[a-f0-9]{32}", submission):
        raise ValueError("A submission identifier is required.")
    record_path = JOBS / (submission + ".json")
    with (STATE / "print.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if record_path.exists():
            record = json.loads(record_path.read_text())
            if record["digest"] != digest or record["upload"] != meta["id"]:
                raise ValueError("That submission identifier belongs to another document.")
            return jsonify(record)
        # The PDF already includes scaling, rotation, cropping and N-up layout.
        native = {k: v for k, v in options.items() if k not in {"page-ranges", "page-set", "outputorder", "number-up-layout", "page-border"}}
        native.update({"print-scaling": "none", "number-up": "1", "orientation-requested": "3", "outputorder": "normal"})
        job_id = printers.connection().printFile(caps["name"], str(path), meta["name"], native)
        record = {"id": job_id, "printer": caps["name"], "title": meta["name"], "upload": meta["id"],
                  "digest": digest, "options": options, "layout": layout, "created": time.time()}
        write_json(record_path, record)
    return jsonify(record), 201


@app.get("/api/jobs")
def jobs():
    result = []
    recorded = {record["id"]: record for record in (json.loads(p.read_text()) for p in JOBS.glob("*.json"))}
    attributes = ["job-id", "job-name", "job-state", "job-printer-uri", "time-at-creation", "job-printer-state-message"]
    for identifier, job in printers.connection().getJobs(which_jobs="all", my_jobs=False, limit=100, requested_attributes=attributes).items():
        own = recorded.get(identifier, {})
        result.append({"id": identifier, "title": job.get("job-name", own.get("title", "Document")), "state": job.get("job-state", 3),
                       "printer": job.get("job-printer-uri", "").split("/")[-1] or own.get("printer", ""), "created": job.get("time-at-creation", own.get("created", 0)),
                       "message": job.get("job-printer-state-message", "")})
    return jsonify(jobs=sorted(result, key=lambda j: j["id"], reverse=True))


@app.post("/api/jobs/<int:identifier>/<action>")
def job_action(identifier, action):
    payload = request.get_json() or {}
    token = payload.get("upload")
    owns_job = any(record.get("id") == identifier and record.get("upload") == token
                   for record in (json.loads(p.read_text()) for p in JOBS.glob("*.json"))) if token else False
    if not owns_job:
        admin()
    conn = printers.connection()
    if action == "cancel":
        conn.cancelJob(identifier)
    elif action == "hold":
        conn.setJobHoldUntil(identifier, "indefinite")
    elif action == "release":
        conn.setJobHoldUntil(identifier, "no-hold")
    else:
        raise ValueError("Unknown job action.")
    return jsonify(ok=True)


@app.post("/api/printers/<name>/defaults")
def save_defaults(name):
    admin()
    payload = request.get_json()
    caps, options = printers.validated_options(name, payload.get("options", {}))
    layout = documents.layout_settings(payload.get("layout", {}))
    controls = {o["key"]: o for o in caps["options"]}
    command = ["lpadmin", "-h", cups.getServer(), "-p", name]
    for key, value in options.items():
        if key in {"page-ranges", "job-hold-until", "job-password", "job-password-encryption"}:
            continue
        keyword = key if key in controls and controls[key]["source"] == "driver" else key + "-default"
        command.extend(["-o", keyword + "=" + value])
    result = subprocess.run(command, text=True, capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "CUPS refused the printer defaults.")
    with (STATE / "settings.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = settings()
        saved[name] = layout
        write_json(STATE / "layouts.json", saved)
    return jsonify(ok=True)


@app.post("/api/printers/<name>/<action>")
def printer_action(name, action):
    admin()
    conn = printers.connection()
    if name not in conn.getPrinters():
        raise ValueError("Printer is not available.")
    methods = {"pause": conn.disablePrinter, "resume": conn.enablePrinter, "reject": conn.rejectJobs,
               "accept": conn.acceptJobs, "make-default": conn.setDefault}
    if action not in methods:
        raise ValueError("Unknown printer action.")
    methods[action](name)
    return jsonify(ok=True)
