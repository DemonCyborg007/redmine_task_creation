"""
Web UI for bulk-creating Redmine issues from a CSV (uses Test.run_bulk_upload).

Run (from repo root, with .env present):
  pip install flask
  python redmine_bulk_web.py

Env:
  BULK_UPLOAD_HOST  default 0.0.0.0
  BULK_UPLOAD_PORT  default 5050
  REDMINE_BULK_UPLOAD_TOKEN  optional; if set, require header X-Upload-Token or form field token
"""

import io
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

from Test import REDMINE_URL, run_bulk_upload

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

_UPLOAD_TOKEN = os.getenv("REDMINE_BULK_UPLOAD_TOKEN", "").strip()


def _check_upload_token() -> bool:
    if not _UPLOAD_TOKEN:
        return True
    return (
        request.headers.get("X-Upload-Token") == _UPLOAD_TOKEN
        or request.form.get("token") == _UPLOAD_TOKEN
    )


@app.route("/")
def index():
    return render_template(
        "bulk_upload.html",
        redmine_url=REDMINE_URL,
        token_required=bool(_UPLOAD_TOKEN),
    )


@app.post("/api/upload")
def api_upload():
    if not _check_upload_token():
        return jsonify({"error": "Invalid or missing upload token"}), 401

    if "file" not in request.files:
        return jsonify({"error": 'No file uploaded (use field name "file")'}), 400

    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "Empty file"}), 400

    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "Please upload a .csv file"}), 400

    raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "File must be UTF-8 encoded"}), 400

    stream = io.StringIO(text)
    stream.name = f.filename

    result_out: dict = {}
    log_lines: list[str] = []

    try:
        ok = run_bulk_upload(stream, result_out=result_out, log_lines=log_lines)
    except Exception as e:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(e),
                    "logs": log_lines,
                }
            ),
            500,
        )

    payload = {
        "ok": ok,
        "completed": result_out.get("completed", False),
        "success": result_out.get("success", 0),
        "failed": result_out.get("failed", 0),
        "skipped": result_out.get("skipped", 0),
        "total_rows": result_out.get("total_rows", 0),
        "logs": log_lines,
    }
    if result_out.get("error"):
        payload["error"] = result_out["error"]

    status = 200 if ok and result_out.get("completed") else 422
    return jsonify(payload), status


def main():
    host = os.getenv("BULK_UPLOAD_HOST", "0.0.0.0")
    port = int(os.getenv("BULK_UPLOAD_PORT", "5050"))
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
