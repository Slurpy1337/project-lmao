#!/usr/bin/env python3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse
from pathlib import Path
import html
import json
import mimetypes
import os
import platform
import socket
import sys
import threading
import time

HOST = os.environ.get("FILE_APP_HOST", "0.0.0.0")
PORT = int(os.environ.get("FILE_APP_PORT", "8080"))
STORAGE = Path(os.environ.get("FILE_APP_STORAGE", "storage")).resolve()
STORAGE.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.environ.get("FILE_APP_MAX_UPLOAD_MB", "1024"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
REFERENCE_MB_PER_SEC = float(os.environ.get("FILE_APP_REFERENCE_MB_PER_SEC", "8"))
REFERENCE_BPS = max(1.0, REFERENCE_MB_PER_SEC * 1024 * 1024)
CHUNK_SIZE = 4 * 1024 * 1024

EVENT_LOCK = threading.Lock()
EVENTS = []
THROUGHPUT_LOCK = threading.Lock()
BYTES_WINDOW = []


def safe_target(name: str) -> Path:
    candidate = (STORAGE / Path(name).name).resolve()
    if STORAGE not in candidate.parents and candidate != STORAGE:
        raise ValueError("Unsafe file path")
    return candidate


def get_client_name(handler) -> str:
    params = parse_qs(urlparse(handler.path).query)
    qname = params.get("user", [""])[0].strip()
    if qname:
        return qname[:80]
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("fileapp_user="):
            return part.split("=", 1)[1][:80]
    return "anonymous"


def log_event(kind: str, user: str, detail: str):
    with EVENT_LOCK:
        EVENTS.append({"ts": int(time.time()), "kind": kind, "user": user or "anonymous", "detail": detail})
        if len(EVENTS) > 250:
            del EVENTS[:-250]


def mark_sent(num_bytes: int):
    now = time.time()
    with THROUGHPUT_LOCK:
        BYTES_WINDOW.append((now, num_bytes))
        cutoff = now - 5.0
        while BYTES_WINDOW and BYTES_WINDOW[0][0] < cutoff:
            BYTES_WINDOW.pop(0)


def get_speed_stats():
    now = time.time()
    with THROUGHPUT_LOCK:
        cutoff = now - 5.0
        while BYTES_WINDOW and BYTES_WINDOW[0][0] < cutoff:
            BYTES_WINDOW.pop(0)
        total = sum(v for _, v in BYTES_WINDOW)
    bps = total / 5.0
    utilization = min(100.0, (bps / REFERENCE_BPS) * 100.0)
    return bps, utilization


def human_size(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{int(v)} {u}" if u == "B" else f"{v:.1f} {u}"
        v /= 1024.0


def file_icon(name: str) -> str:
    ext = Path(name).suffix.lower()
    mapping = {
        ".png": "🖼️", ".jpg": "🖼️", ".jpeg": "🖼️", ".gif": "🖼️", ".webp": "🖼️", ".svg": "🖼️",
        ".pdf": "📕", ".zip": "🗜️", ".rar": "🗜️", ".7z": "🗜️", ".tar": "🗜️", ".gz": "🗜️",
        ".mp4": "🎬", ".mov": "🎬", ".mkv": "🎬", ".mp3": "🎵", ".wav": "🎵",
        ".txt": "📄", ".md": "📝", ".doc": "📘", ".docx": "📘", ".xls": "📗", ".xlsx": "📗",
        ".py": "🐍", ".js": "🟨", ".json": "🧩",
    }
    return mapping.get(ext, "📁")


class FileAppHandler(BaseHTTPRequestHandler):
    server_version = "LocalFileApp/1.0"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        except OSError:
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.show_index(parsed.query)
        elif parsed.path == "/download":
            params = parse_qs(parsed.query)
            if "name" not in params:
                self.send_error(400, "Missing file name")
                return
            self.download_file(params["name"][0])
        elif parsed.path == "/stats":
            self.stats()
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            self.upload_file()
        elif parsed.path == "/comment":
            self.comment()
        else:
            self.send_error(404, "Not found")

    def show_index(self, query: str = ""):
        message = html.escape(parse_qs(query).get("msg", [""])[0])
        files = sorted([(p.name, p.stat().st_size) for p in STORAGE.iterdir() if p.is_file()], key=lambda x: x[0].lower())

        cards = ""
        for name, size in files:
            escaped = html.escape(name)
            ext = html.escape((Path(name).suffix or ".file").lstrip(".").upper())
            cards += (
                f"<article class='file-card'><div class='preview'>{file_icon(name)}</div>"
                f"<div><div class='file-name' title='{escaped}'>{escaped}</div><div class='file-sub'>{ext} • {human_size(size)}</div></div>"
                f"<div class='file-actions'><a class='btn btn-primary' href='/download?name={quote(name)}'>Download</a></div></article>"
            )

        event_items = ""
        with EVENT_LOCK:
            for ev in reversed(EVENTS[-20:]):
                when = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
                event_items += f"<li><strong>{html.escape(ev['user'])}</strong> [{when}] {html.escape(ev['kind'])}: {html.escape(ev['detail'])}</li>"

        body = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Self-Hosted File App</title><style>
body {{font-family:Inter,system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;background:radial-gradient(circle at top left,#f0f7ff,#f7f6ff 40%,#fff 80%);}}
.shell{{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:1.1rem;box-shadow:0 10px 28px rgba(15,23,42,.08);}}
.msg{{background:#ecfdf3;border:1px solid #8dd9aa;padding:.7rem;margin:1rem 0;border-radius:.3rem}} .warn{{background:#fff7e6;border:1px solid #ffd48a;padding:.7rem;margin:1rem 0;border-radius:.3rem}}
.file-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:.85rem}} .file-card{{display:flex;align-items:center;gap:.75rem;border:1px solid #e5e7eb;border-radius:12px;padding:.7rem;background:linear-gradient(180deg,#fff,#f9fbff)}}
.preview{{width:56px;height:56px;border-radius:10px;display:grid;place-items:center;font-size:1.55rem;background:linear-gradient(180deg,#eef2ff,#eff6ff)}} .file-name{{font-weight:600;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}} .file-sub{{color:#6b7280;font-size:.86rem}}
.file-actions{{margin-left:auto;display:flex;gap:.5rem;align-items:center}} .btn{{border:0;border-radius:10px;padding:.45rem .65rem;font-size:.86rem;text-decoration:none;cursor:pointer}} .btn-primary{{background:#2563eb;color:#fff}}
#speed-widget{{position:fixed;right:16px;bottom:16px;width:300px;border:1px solid #ccc;border-radius:8px;background:#fff;padding:.6rem;box-shadow:0 2px 10px rgba(0,0,0,.12)}} .progress{{width:100%;height:12px;background:#eee;border-radius:999px;overflow:hidden;margin-top:.4rem}} .progress>div{{height:100%;width:0%;background:linear-gradient(90deg,#3b82f6,#22c55e)}}
</style></head><body>
<h1>Self-Hosted File App</h1>
<p>Upload, comment, and download files from <code>{html.escape(str(STORAGE))}</code>.</p>
<div class='warn'>Security note: this app has no authentication.</div>
{f"<div class='msg'>{message}</div>" if message else ''}
<div class='shell'>
<h2>Upload a file</h2><form method='post' action='/upload' enctype='multipart/form-data'><input type='file' name='file' required><button class='btn btn-primary' type='submit'>Upload</button></form>
<h2>Add comment</h2><form method='post' action='/comment'><input type='text' name='comment' maxlength='300' placeholder='Write a comment' required style='width:70%'><button class='btn btn-primary' type='submit'>Post</button></form>
<h2>Activity log</h2><ul>{event_items if event_items else '<li>No activity yet.</li>'}</ul>
<h2>Files ({len(files)})</h2><div class='file-grid'>{cards if cards else '<p>No files yet.</p>'}</div></div>
<div id='speed-widget'><div><strong>Download Throughput</strong></div><div id='speed-current'>Current: 0.00 MB/s</div><div id='speed-ref'>Reference: {REFERENCE_MB_PER_SEC:g} MB/s</div><div class='progress'><div id='speed-fill'></div></div><small id='speed-pct'>0%</small></div>
<script>(function(){{const key='fileapp_user';let user=localStorage.getItem(key);if(!user){{user=prompt('Enter your name for activity logging:')||'anonymous';localStorage.setItem(key,user.trim()||'anonymous');}}document.cookie=`fileapp_user=${{encodeURIComponent(localStorage.getItem(key)||'anonymous')}}; path=/; max-age=31536000`;document.querySelectorAll('form').forEach((f)=>{{const u=encodeURIComponent(localStorage.getItem(key)||'anonymous');if(!f.action.includes('user='))f.action+=(f.action.includes('?')?'&':'?')+'user='+u;}});document.querySelectorAll("a[href^='/download']").forEach((a)=>{{const u=encodeURIComponent(localStorage.getItem(key)||'anonymous');if(!a.href.includes('user='))a.href+=(a.href.includes('?')?'&':'?')+'user='+u;}});async function refresh(){{try{{const r=await fetch('/stats');const d=await r.json();document.getElementById('speed-fill').style.width=d.utilization_pct+'%';document.getElementById('speed-pct').textContent=`${{d.utilization_pct}}%`;document.getElementById('speed-current').textContent=`Current: ${{d.current_mb_s.toFixed(2)}} MB/s`;document.getElementById('speed-ref').textContent=`Reference: ${{d.reference_mb_s.toFixed(2)}} MB/s`;}}catch(_ ){{}}}}refresh();setInterval(refresh,1000);}})();</script>
</body></html>"""
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def upload_file(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            self.send_error(400, "Expected multipart form-data")
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.redirect(f"/?msg={quote('Upload blocked: invalid size or exceeds limit')}")
            return

        boundary = ctype.split("boundary=")[-1].encode()
        raw = self.rfile.read(length)
        parts = raw.split(b"--" + boundary)

        uploaded = False
        filename = ""
        for part in parts:
            if b'Content-Disposition:' not in part or b'name="file"' not in part:
                continue
            header, _, rest = part.partition(b"\r\n\r\n")
            if not rest:
                continue
            line = header.decode("utf-8", errors="ignore")
            if 'filename="' not in line:
                continue
            filename = line.split('filename="', 1)[1].split('"', 1)[0].strip()
            if not filename:
                continue
            with safe_target(filename).open("wb") as f:
                f.write(rest.rsplit(b"\r\n", 1)[0])
            uploaded = True
            break

        if uploaded:
            log_event("upload", get_client_name(self), filename)
            self.redirect(f"/?msg={quote('Upload successful')}")
        else:
            self.redirect(f"/?msg={quote('Upload failed')}")

    def download_file(self, name: str):
        try:
            target = safe_target(name)
        except ValueError:
            self.send_error(400, "Invalid file name")
            return

        if not target.exists() or not target.is_file():
            self.send_error(404, "File not found")
            return

        file_size = target.stat().st_size
        ctype, _ = mimetypes.guess_type(target.name)
        ctype = ctype or "application/octet-stream"

        start = 0
        end = file_size - 1
        range_header = self.headers.get("Range")

        if range_header:
            if not range_header.startswith("bytes="):
                self.send_error(416, "Range Not Satisfiable")
                return
            raw_range = range_header[len("bytes="):].strip()
            if "," in raw_range:
                self.send_error(416, "Range Not Satisfiable")
                return
            start_str, _, end_str = raw_range.partition("-")
            try:
                if start_str == "":
                    suffix = int(end_str)
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, file_size - suffix)
                    end = file_size - 1
                else:
                    start = int(start_str)
                    end = int(end_str) if end_str else file_size - 1
            except ValueError:
                self.send_error(416, "Range Not Satisfiable")
                return
            if start < 0 or end < start or start >= file_size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
            end = min(end, file_size - 1)
            content_length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        else:
            content_length = file_size
            self.send_response(200)

        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(target.name)}")
        self.end_headers()

        sent = 0
        with target.open("rb", buffering=0) as f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                read_size = CHUNK_SIZE if remaining > CHUNK_SIZE else remaining
                block = f.read(read_size)
                if not block:
                    break
                view = memoryview(block)
                self.wfile.write(view)
                n = len(view)
                sent += n
                remaining -= n

        mark_sent(sent)
        log_event("download", get_client_name(self), f"{target.name} ({human_size(sent)})")

    def comment(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        params = parse_qs(body)
        text = params.get("comment", [""])[0].strip()
        if text:
            log_event("comment", get_client_name(self), text[:300])
            self.redirect(f"/?msg={quote('Comment posted')}")
        else:
            self.redirect(f"/?msg={quote('Comment cannot be empty')}")

    def stats(self):
        bps, utilization = get_speed_stats()
        payload = json.dumps({
            "bytes_per_second": round(bps, 2),
            "current_mb_s": round(bps / (1024 * 1024), 4),
            "reference_mb_s": REFERENCE_MB_PER_SEC,
            "utilization_pct": round(utilization, 1),
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()


if __name__ == "__main__":
    print(f"Serving file app on http://{HOST}:{PORT}")
    print(f"Storage directory: {STORAGE}")
    print(f"Max upload size: {MAX_UPLOAD_MB} MB")
    print("Artificial throttling: OFF")
    print("Delete endpoint/button: OFF")
    print(f"Reference speed only: {REFERENCE_MB_PER_SEC:g} MB/s")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Platform: {platform.platform()}")
    print("Warning: real speed depends on ISP/router/Wi-Fi/disk/client performance.")
    server = ThreadingHTTPServer((HOST, PORT), FileAppHandler)
    server.serve_forever()
