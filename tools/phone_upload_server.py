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
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse


UPLOAD_DIR = Path(os.getenv("AGENTZERO_UPLOAD_DIR", "/app/work_dir/assets/agentzero_uploads/shorts_test"))
OUTPUT_ROOT = Path(os.getenv("AGENTZERO_OUTPUT_DIR", "/app/work_dir/assets/agentzero_outputs"))
RENDER_STATUS_FILE = OUTPUT_ROOT / "render_status.json"
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
render_process: subprocess.Popen | None = None


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


def read_render_status() -> dict:
    if not RENDER_STATUS_FILE.exists():
        return {"state": "idle"}
    try:
        return json.loads(RENDER_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"state": "unknown", "error": str(exc)}


def write_render_status(status: dict) -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    status["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    tmp = RENDER_STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=True), encoding="utf-8")
    tmp.replace(RENDER_STATUS_FILE)


def page(message: str = "") -> bytes:
    message_html = f"<p class='message'>{html.escape(message)}</p>" if message else ""
    render_status = read_render_status()
    render_state = html.escape(str(render_status.get("state", "idle")))
    render_step = html.escape(str(render_status.get("step", "")))
    render_final = html.escape(str(render_status.get("final", "")))
    render_thumbnail = html.escape(str(render_status.get("thumbnail", "")))
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
    .render-card {{
      margin: 24px 0;
      padding: 20px;
      border: 1px solid #333;
      border-radius: 8px;
      background: #181818;
    }}
    .render-card button {{
      background: #f7f7f7;
      color: #111;
    }}
    .render-output {{
      margin-top: 12px;
      padding: 12px;
      border-radius: 6px;
      background: #0d0d0d;
      color: #d8d8d8;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 13px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
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
    <section class="render-card">
      <h2>Render Test</h2>
      <p>Start a server-side render from the newest completed upload. Partial files are ignored and the selected file must pass ffprobe first.</p>
      <button id="render-button" type="button">Render Latest Valid Upload</button>
      <div id="render-output" class="render-output">State: {render_state}
Step: {render_step}
Final: {render_final}
Thumbnail: {render_thumbnail}</div>
    </section>
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
    const renderButton = document.getElementById("render-button");
    const renderOutput = document.getElementById("render-output");

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

    function renderStatusText(data) {{
      const lines = [
        "State: " + (data.state || "idle"),
        "Step: " + (data.step || ""),
        "Input: " + (data.input || ""),
        "Final: " + (data.final || ""),
        "Thumbnail: " + (data.thumbnail || ""),
        "Duration: " + (data.duration || ""),
        "Cuts: " + (data.cut_count || ""),
        "Error: " + (data.error || "")
      ];
      return lines.join("\\n");
    }}

    async function loadRenderStatus() {{
      try {{
        const response = await fetch("/render-status", {{ cache: "no-store" }});
        const data = await response.json();
        renderOutput.textContent = renderStatusText(data);
        renderButton.disabled = data.state === "running" || data.state === "queued";
      }} catch (_err) {{
        renderOutput.textContent = "Could not load render status.";
      }}
    }}

    renderButton.addEventListener("click", async () => {{
      renderButton.disabled = true;
      renderOutput.textContent = "Starting server render...";
      try {{
        const response = await fetch("/render-latest", {{ method: "POST" }});
        const data = await response.json();
        renderOutput.textContent = renderStatusText(data);
      }} catch (_err) {{
        renderOutput.textContent = "Render start failed.";
        renderButton.disabled = false;
      }}
    }});

    window.setInterval(loadRenderStatus, 5000);
    loadRenderStatus();
  </script>
</body>
</html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/render-status":
            self.send_json(current_render_status())
            return
        if path not in ("", "/"):
            self.send_error(404, "Not found")
            return

        payload = page()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/render-latest":
            self.start_render()
            return

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

    def send_json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def start_render(self) -> None:
        global render_process
        if render_process and render_process.poll() is None:
            self.send_json(current_render_status() | {"ok": True, "message": "Render already running"}, 202)
            return

        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        log_path = OUTPUT_ROOT / "render-launch.log"
        write_render_status(
            {
                "state": "queued",
                "step": "launch",
                "upload_dir": str(UPLOAD_DIR),
                "output_root": str(OUTPUT_ROOT),
                "log": str(log_path),
            }
        )

        script = Path(__file__).with_name("phone_render_worker.py")
        env = os.environ.copy()
        log_file = log_path.open("ab", buffering=0)
        render_process = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(script.parent),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            close_fds=True,
        )
        self.send_json(current_render_status() | {"ok": True, "pid": render_process.pid}, 202)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[phone-upload] {self.address_string()} - {fmt % args}", flush=True)


def current_render_status() -> dict:
    status = read_render_status()
    if render_process and render_process.poll() is None:
        status["process"] = "running"
    elif render_process:
        status["process"] = "exited"
        status["process_exit_code"] = render_process.poll()
    return status


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
