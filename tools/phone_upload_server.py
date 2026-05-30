#!/usr/bin/env python3
"""
Tiny phone-friendly upload page for Agent Zero assets.

Runs inside the Agent Zero container and writes uploaded files to the mounted
assets volume, so files immediately appear under /app/work_dir/assets.
"""

from __future__ import annotations

import cgi
import html
import json
import os
import shutil
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote


UPLOAD_DIR = Path(os.getenv("AGENTZERO_UPLOAD_DIR", "/app/work_dir/assets/agentzero_uploads/shorts_test"))
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}


def safe_filename(filename: str) -> str:
    original = Path(filename or "upload.mov").name
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        suffix = ".mov"

    stem = Path(original).stem or "upload"
    clean_stem = "".join(ch if ch.isalnum() else "-" for ch in stem).strip("-") or "upload"
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{clean_stem[:64]}{suffix}"


def list_uploads() -> str:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(UPLOAD_DIR.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)[:20]:
        if not path.is_file():
            continue
        size_mb = path.stat().st_size / (1024 * 1024)
        rows.append(
            "<tr>"
            f"<td>{html.escape(path.name)}</td>"
            f"<td>{size_mb:.1f} MB</td>"
            "</tr>"
        )
    return "".join(rows) or "<tr><td colspan='2'>No uploads yet.</td></tr>"


def page(message: str = "") -> bytes:
    message_html = f"<p class='message'>{html.escape(message)}</p>" if message else ""
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Zero Upload</title>
  <style>
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #111;
      color: #f7f7f7;
    }}
    main {{
      max-width: 760px;
      margin: 0 auto;
      padding: 36px 18px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 30px;
    }}
    p {{
      color: #cfcfcf;
      line-height: 1.45;
    }}
    form {{
      margin: 24px 0;
      padding: 20px;
      border: 1px solid #333;
      border-radius: 8px;
      background: #1b1b1b;
    }}
    input[type=file] {{
      box-sizing: border-box;
      width: 100%;
      padding: 16px;
      border: 1px dashed #555;
      border-radius: 6px;
      background: #0d0d0d;
      color: #fff;
    }}
    button {{
      width: 100%;
      margin-top: 14px;
      padding: 14px 16px;
      border: 0;
      border-radius: 6px;
      background: #ffd400;
      color: #111;
      font-weight: 800;
      font-size: 16px;
    }}
    button:disabled {{
      opacity: 0.55;
    }}
    .progress-wrap {{
      display: none;
      margin-top: 18px;
    }}
    .progress-meta {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 8px;
      color: #f7f7f7;
      font-weight: 800;
    }}
    .progress-track {{
      height: 16px;
      overflow: hidden;
      border-radius: 999px;
      background: #2b2b2b;
      border: 1px solid #444;
    }}
    .progress-bar {{
      width: 0%;
      height: 100%;
      background: #ffd400;
      transition: width 0.18s ease;
    }}
    .status {{
      min-height: 22px;
      margin-top: 10px;
      color: #cfcfcf;
      font-weight: 700;
    }}
    .status.ok {{
      color: #5af28a;
    }}
    .status.error {{
      color: #ff6464;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 14px;
    }}
    th, td {{
      padding: 10px 4px;
      border-bottom: 1px solid #2c2c2c;
      text-align: left;
      font-size: 14px;
      overflow-wrap: anywhere;
    }}
    .path {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      color: #aaa;
      overflow-wrap: anywhere;
    }}
    .message {{
      color: #ffd400;
      font-weight: 800;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Agent Zero Upload</h1>
    <p>Upload one iPhone clip. The file will be saved into Agent Zero's mounted assets folder.</p>
    <p class="path">{html.escape(str(UPLOAD_DIR))}</p>
    {message_html}
    <form id="upload-form" method="post" enctype="multipart/form-data">
      <input id="video-input" type="file" name="video" accept="video/*" required>
      <button id="upload-button" type="submit">Upload Video</button>
      <div id="progress-wrap" class="progress-wrap" aria-live="polite">
        <div class="progress-meta">
          <span id="progress-label">Waiting</span>
          <span id="progress-percent">0%</span>
        </div>
        <div class="progress-track">
          <div id="progress-bar" class="progress-bar"></div>
        </div>
        <div id="upload-status" class="status"></div>
      </div>
    </form>
    <h2>Recent Uploads</h2>
    <table>
      <thead><tr><th>File</th><th>Size</th></tr></thead>
      <tbody>{list_uploads()}</tbody>
    </table>
  </main>
  <script>
    const form = document.getElementById("upload-form");
    const input = document.getElementById("video-input");
    const button = document.getElementById("upload-button");
    const progressWrap = document.getElementById("progress-wrap");
    const progressBar = document.getElementById("progress-bar");
    const progressPercent = document.getElementById("progress-percent");
    const progressLabel = document.getElementById("progress-label");
    const status = document.getElementById("upload-status");

    function setProgress(percent, label) {{
      const clean = Math.max(0, Math.min(100, Math.round(percent)));
      progressWrap.style.display = "block";
      progressBar.style.width = clean + "%";
      progressPercent.textContent = clean + "%";
      progressLabel.textContent = label;
    }}

    function setStatus(message, kind) {{
      status.textContent = message;
      status.className = "status" + (kind ? " " + kind : "");
    }}

    form.addEventListener("submit", (event) => {{
      event.preventDefault();
      if (!input.files || input.files.length === 0) {{
        setStatus("Choose a video first.", "error");
        return;
      }}

      const file = input.files[0];
      const formData = new FormData();
      formData.append("video", file);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", window.location.href, true);
      xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");

      button.disabled = true;
      input.disabled = true;
      setProgress(0, "Uploading");
      setStatus(file.name + " selected. Keep this page open.", "");

      xhr.upload.addEventListener("progress", (event) => {{
        if (event.lengthComputable) {{
          setProgress((event.loaded / event.total) * 100, "Uploading");
        }} else {{
          progressWrap.style.display = "block";
          progressLabel.textContent = "Uploading";
          progressPercent.textContent = "Working...";
        }}
      }});

      xhr.addEventListener("load", () => {{
        if (xhr.status >= 200 && xhr.status < 300) {{
          setProgress(100, "Uploaded");
          try {{
            const data = JSON.parse(xhr.responseText);
            setStatus("Saved as " + data.filename + " (" + data.size_mb + " MB). Refreshing list...", "ok");
          }} catch (_err) {{
            setStatus("Upload complete. Refreshing list...", "ok");
          }}
          window.setTimeout(() => window.location.reload(), 1200);
        }} else {{
          setStatus("Upload failed with status " + xhr.status + ".", "error");
          button.disabled = false;
          input.disabled = false;
        }}
      }});

      xhr.addEventListener("error", () => {{
        setStatus("Upload failed. Check connection and try again.", "error");
        button.disabled = false;
        input.disabled = false;
      }});

      xhr.addEventListener("abort", () => {{
        setStatus("Upload cancelled.", "error");
        button.disabled = false;
        input.disabled = false;
      }});

      xhr.send(formData);
    }});
  </script>
</body>
</html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        payload = page()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        content_type = self.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_error(400, "Expected multipart/form-data")
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type},
        )
        field = form["video"] if "video" in form else None
        if isinstance(field, list):
            field = field[0] if field else None
        if field is None or not getattr(field, "file", None):
            self.send_error(400, "Missing video field")
            return

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        filename = safe_filename(getattr(field, "filename", "upload.mov"))
        destination = UPLOAD_DIR / filename
        partial = UPLOAD_DIR / f"{filename}.part"

        with partial.open("wb") as out_file:
            shutil.copyfileobj(field.file, out_file, length=1024 * 1024)
        partial.rename(destination)

        if self.headers.get("X-Requested-With") == "XMLHttpRequest":
            payload = json.dumps(
                {
                    "ok": True,
                    "filename": filename,
                    "size_mb": round(destination.stat().st_size / (1024 * 1024), 1),
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(303)
        self.send_header("Location", f"/?uploaded={quote(filename)}")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        print(f"[phone-upload] {self.address_string()} - {fmt % args}", flush=True)


def main() -> int:
    host = os.getenv("AGENTZERO_UPLOAD_HOST", "0.0.0.0")
    port = int(os.getenv("AGENTZERO_UPLOAD_PORT", "8080"))
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[phone-upload] Listening on http://{host}:{port}", flush=True)
    print(f"[phone-upload] Saving uploads to {UPLOAD_DIR}", flush=True)
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
