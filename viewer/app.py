import io
import os
import re
import time
import uuid as _uuid
import threading
from typing import Dict, Tuple

from flask import Flask, jsonify, render_template, request, send_file, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)

# pdfs store: pdf_id → (bytes, created_at, safe_filename)
pdfs: Dict[str, Tuple[bytes, float, str]] = {}
pdfs_lock = threading.Lock()

MAX_PDF_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
PDF_TTL = 1800  # 30 minutes
PDF_MAGIC = b"%PDF-"


def _cleanup_old_pdfs() -> None:
    """Daemon thread: remove PDFs older than PDF_TTL every 5 minutes."""
    while True:
        time.sleep(300)
        cutoff = time.time() - PDF_TTL
        with pdfs_lock:
            stale = [pid for pid, (_, created_at, _) in pdfs.items() if created_at < cutoff]
            for pid in stale:
                pdfs.pop(pid, None)


threading.Thread(target=_cleanup_old_pdfs, daemon=True).start()


def _safe_filename(raw: str) -> str:
    """Sanitize uploaded filename — strip path components, keep extension."""
    name = secure_filename(raw or "upload.pdf")
    return os.path.basename(name) or "upload.pdf"


def _valid_uuid(val: str) -> bool:
    try:
        _uuid.UUID(val)
        return True
    except ValueError:
        return False


@app.route("/")
def index():
    return render_template("viewer.html")


@app.route("/api/upload", methods=["POST"])
def upload_pdf():
    # Reject oversized uploads before reading the body
    content_length = request.content_length
    if content_length is not None and content_length > MAX_PDF_SIZE:
        return jsonify({"error": "File too large (max 2 GB)"}), 400

    f = request.files.get("file")
    raw_name = f.filename if f and f.filename else ""
    if not f or not raw_name.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file"}), 400

    # Read with hard cap to guard against missing Content-Length
    data = f.read(MAX_PDF_SIZE + 1)
    if len(data) > MAX_PDF_SIZE:
        return jsonify({"error": "File too large (max 2 GB)"}), 400

    # Validate PDF magic bytes — reject files that aren't actually PDFs
    if not data[:5] == PDF_MAGIC:
        return jsonify({"error": "File does not appear to be a valid PDF"}), 400

    safe_name = _safe_filename(raw_name)
    pdf_id = str(_uuid.uuid4())
    with pdfs_lock:
        pdfs[pdf_id] = (data, time.time(), safe_name)

    return jsonify({"pdf_id": pdf_id, "size": len(data), "name": safe_name})


@app.route("/api/pdf/<pdf_id>")
def serve_pdf(pdf_id: str):
    """Serve the PDF bytes with range request support for PDF.js."""
    if not _valid_uuid(pdf_id):
        return jsonify({"error": "Invalid ID"}), 400

    with pdfs_lock:
        entry = pdfs.get(pdf_id)
    if entry is None:
        return jsonify({"error": "PDF not found"}), 404

    data, _, _ = entry
    range_header = request.headers.get("Range")
    if range_header:
        byte_start, byte_end = 0, len(data) - 1
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            byte_start = int(m.group(1))
            if m.group(2):
                byte_end = min(int(m.group(2)), len(data) - 1)
        # Clamp to valid range
        byte_start = max(0, min(byte_start, len(data) - 1))
        byte_end = max(byte_start, min(byte_end, len(data) - 1))
        chunk = data[byte_start: byte_end + 1]
        return Response(
            chunk,
            status=206,
            mimetype="application/pdf",
            headers={
                "Content-Range": f"bytes {byte_start}-{byte_end}/{len(data)}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(chunk)),
            },
        )

    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=False)


@app.route("/api/pdf/<pdf_id>/delete", methods=["POST"])
def delete_pdf(pdf_id: str):
    if not _valid_uuid(pdf_id):
        return jsonify({"error": "Invalid ID"}), 400
    with pdfs_lock:
        pdfs.pop(pdf_id, None)
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("\n  Manhwa PDF Viewer running at http://localhost:5056\n")
    app.run(port=5056, debug=False)
