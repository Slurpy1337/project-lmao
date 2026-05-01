#!/usr/bin/env python3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, quote
from pathlib import Path
import html
import mimetypes
import os
import shutil

HOST = os.environ.get("FILE_APP_HOST", "0.0.0.0")
PORT = int(os.environ.get("FILE_APP_PORT", "8080"))
STORAGE = Path(os.environ.get("FILE_APP_STORAGE", "storage")).resolve()
STORAGE.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.environ.get("FILE_APP_MAX_UPLOAD_MB", "1024"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


def safe_target(name: str) -> Path:
    candidate = (STORAGE / Path(name).name).resolve()
    if STORAGE not in candidate.parents and candidate != STORAGE:
        raise ValueError("Unsafe file path")
    return candidate


class FileAppHandler(BaseHTTPRequestHandler):
    server_version = "LocalFileApp/1.0"

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
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            self.upload_file()
        elif parsed.path == "/delete":
            self.delete_file()
        else:
            self.send_error(404, "Not found")

    def show_index(self, query: str = ""):
        params = parse_qs(query)
        message = html.escape(params.get("msg", [""])[0])

        files = []
        for p in STORAGE.iterdir():
            if p.is_file():
                files.append((p.name, p.stat().st_size))
        files.sort(key=lambda x: x[0].lower())

        rows = ""
        for name, size in files:
            escaped = html.escape(name)
            rows += (
                "<tr>"
                f"<td>{escaped}</td>"
                f"<td>{size:,} bytes</td>"
                f"<td><a href='/download?name={quote(name)}'>Download</a></td>"
                "<td>"
                "<form method='post' action='/delete' onsubmit=\"return confirm('Delete this file?');\">"
                f"<input type='hidden' name='name' value='{escaped}'>"
                "<button type='submit'>Delete</button>"
                "</form>"
                "</td>"
                "</tr>"
            )

        body = f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Self-Hosted File App</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
    th, td {{ border-bottom: 1px solid #ddd; text-align: left; padding: .6rem; }}
    .msg {{ background: #ecfdf3; border: 1px solid #8dd9aa; padding: .7rem; margin: 1rem 0; border-radius: .3rem; }}
    .warn {{ background: #fff7e6; border: 1px solid #ffd48a; padding: .7rem; margin: 1rem 0; border-radius: .3rem; }}
    form.inline {{ display: inline; }}
  </style>
</head>
<body>
  <h1>Self-Hosted File App</h1>
  <p>Upload, download, and delete files from <code>{html.escape(str(STORAGE))}</code>.</p>
  <div class='warn'>Security note: this app has no authentication. Put it behind a VPN, reverse proxy auth, or only share with trusted people.</div>
  {f"<div class='msg'>{message}</div>" if message else ""}

  <h2>Upload a file</h2>
  <form method='post' action='/upload' enctype='multipart/form-data'>
    <input type='file' name='file' required>
    <button type='submit'>Upload</button>
  </form>

  <h2>Files ({len(files)})</h2>
  <table>
    <thead><tr><th>Name</th><th>Size</th><th colspan='2'>Actions</th></tr></thead>
    <tbody>{rows if rows else "<tr><td colspan='4'>No files yet.</td></tr>"}</tbody>
  </table>
</body>
</html>"""
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
            self.redirect(f"/?msg={quote('Upload blocked: invalid size or exceeds limit')}" )
            return

        boundary = ctype.split("boundary=")[-1].encode()
        raw = self.rfile.read(length)
        parts = raw.split(b"--" + boundary)

        uploaded = False
        for part in parts:
            if b'Content-Disposition:' not in part or b'name="file"' not in part:
                continue
            header, _, rest = part.partition(b"\r\n\r\n")
            if not rest:
                continue
            disposition_line = header.decode("utf-8", errors="ignore")
            marker = 'filename="'
            if marker not in disposition_line:
                continue
            filename = disposition_line.split(marker, 1)[1].split('"', 1)[0].strip()
            if not filename:
                continue
            content = rest.rsplit(b"\r\n", 1)[0]
            target = safe_target(filename)
            with target.open("wb") as f:
                f.write(content)
            uploaded = True
            break

        if uploaded:
            self.redirect(f"/?msg={quote('Upload successful')}" )
        else:
            self.redirect(f"/?msg={quote('Upload failed')}" )

    def download_file(self, name: str):
        try:
            target = safe_target(name)
        except ValueError:
            self.send_error(400, "Invalid file name")
            return

        if not target.exists() or not target.is_file():
            self.send_error(404, "File not found")
            return

        ctype, _ = mimetypes.guess_type(target.name)
        ctype = ctype or "application/octet-stream"
        size = target.stat().st_size

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(target.name)}")
        self.end_headers()

        with target.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def delete_file(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        params = parse_qs(body)
        name = params.get("name", [""])[0]
        if not name:
            self.redirect(f"/?msg={quote('No file selected')}" )
            return
        try:
            target = safe_target(name)
            if target.exists() and target.is_file():
                target.unlink()
                self.redirect(f"/?msg={quote('Deleted')}" )
            else:
                self.redirect(f"/?msg={quote('File not found')}" )
        except ValueError:
            self.redirect(f"/?msg={quote('Invalid file name')}" )

    def redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()


if __name__ == "__main__":
    print(f"Serving file app on http://{HOST}:{PORT}")
    print(f"Storage directory: {STORAGE}")
    print(f"Max upload size: {MAX_UPLOAD_MB} MB")
    server = ThreadingHTTPServer((HOST, PORT), FileAppHandler)
    server.serve_forever()
