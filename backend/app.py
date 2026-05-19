#!/usr/bin/env python3
from __future__ import annotations

import http.server
import socketserver
import os
import uuid
import cgi
import time
import shutil
import json
import datetime
import logging
import threading
from html import escape
import ipaddress
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass
from typing import NewType

logger = logging.getLogger("temp-file-share")

IPAddress = NewType("IPAddress", str)

PORT = 54000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class Config:
    upload_dir: str
    max_storage_gb: int
    max_age_hours: int
    ip_limit_gb: int
    files_db: str
    rate_limit_seconds: int
    cleanup_interval_seconds: int
    public_base_url: str


@dataclass(frozen=True)
class FileEntry:
    filename: str
    size: int
    time: float


def _config_str(val: object, default: str) -> str:
    return str(val) if val is not None else default


def _config_int(val: object, default: int) -> int:
    if val is None:
        return default
    return int(val)


def load_config() -> Config:
    config_path = os.path.join(BASE_DIR, "config.json")
    try:
        with open(config_path) as f:
            raw: dict[str, object] = json.load(f)
    except json.JSONDecodeError as err:
        raise ValueError("failed to parse config.json") from err
    return Config(
        upload_dir=_config_str(raw.get("UPLOAD_DIR"), "uploads"),
        max_storage_gb=_config_int(raw.get("MAX_STORAGE_GB"), 50),
        max_age_hours=_config_int(raw.get("MAX_AGE_HOURS"), 5),
        ip_limit_gb=_config_int(raw.get("IP_LIMIT_GB"), 10),
        files_db=_config_str(raw.get("FILES_DB"), "data/files_db.json"),
        rate_limit_seconds=_config_int(raw.get("RATE_LIMIT_SECONDS"), 0),
        cleanup_interval_seconds=_config_int(raw.get("CLEANUP_INTERVAL_SECONDS"), 300),
        public_base_url=_config_str(raw.get("PUBLIC_BASE_URL"), ""),
    )


config = load_config()

UPLOAD_DIR = os.path.join(BASE_DIR, config.upload_dir)
MAX_STORAGE_GB = config.max_storage_gb
MAX_AGE_SECONDS = config.max_age_hours * 3600
IP_LIMIT_GB = config.ip_limit_gb
FILES_DB = os.path.join(BASE_DIR, config.files_db)
RATE_LIMIT_SECONDS = config.rate_limit_seconds
CLEANUP_INTERVAL_SECONDS = config.cleanup_interval_seconds
PUBLIC_BASE_URL = config.public_base_url

TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "index.html")
UPLOADS_TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "uploads.html")
ROBOTS_TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "robots.txt")
SITEMAP_TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "sitemap.xml")
UPLOAD_SCRIPT_PATH = os.path.join(BASE_DIR, "scripts", "upload.sh")
STATIC_DIR = os.path.join(BASE_DIR, "static")

with open(TEMPLATE_PATH) as f:
    INDEX_TEMPLATE: str = f.read()

with open(UPLOADS_TEMPLATE_PATH) as f:
    UPLOADS_TEMPLATE: str = f.read()

with open(ROBOTS_TEMPLATE_PATH) as f:
    ROBOTS_TEMPLATE: str = f.read()

with open(SITEMAP_TEMPLATE_PATH) as f:
    SITEMAP_TEMPLATE: str = f.read()

with open(UPLOAD_SCRIPT_PATH) as f:
    UPLOAD_SCRIPT: str = f.read()

FileDb = dict[IPAddress, list[FileEntry]]


def load_db() -> FileDb:
    if not os.path.exists(FILES_DB):
        return {}
    try:
        with open(FILES_DB) as f:
            raw: dict[str, list[dict[str, object]]] = json.load(f)
    except json.JSONDecodeError as err:
        logger.warning("corrupt db, resetting: %s", err)
        return {}
    result: FileDb = {}
    for ip, entries in raw.items():
        parsed: list[FileEntry] = []
        for e in entries:
            parsed.append(
                FileEntry(
                    filename=str(e.get("filename", "")),
                    size=_config_int(e.get("size"), 0),
                    time=float(e.get("time", 0.0)),
                )
            )
        result[IPAddress(ip)] = parsed
    return result


def save_db(db: FileDb) -> None:
    raw: dict[str, list[dict[str, object]]] = {}
    for ip, entries in db.items():
        raw[ip] = [
            {"filename": e.filename, "size": e.size, "time": e.time}
            for e in entries
        ]
    with open(FILES_DB, "w") as f:
        json.dump(raw, f)


def cleanup_old_files() -> None:
    db = load_db()
    now = time.time()
    updated = False
    for ip, files in list(db.items()):
        new_files: list[FileEntry] = []
        for entry in files:
            filepath = os.path.join(UPLOAD_DIR, entry.filename)
            if os.path.exists(filepath) and now - entry.time > MAX_AGE_SECONDS:
                os.remove(filepath)
                updated = True
            else:
                new_files.append(entry)
        if new_files:
            db[ip] = new_files
        else:
            del db[ip]
    if updated:
        save_db(db)


def get_current_used() -> int:
    total = 0
    for fname in os.listdir(UPLOAD_DIR):
        fpath = os.path.join(UPLOAD_DIR, fname)
        if os.path.isfile(fpath):
            total += os.path.getsize(fpath)
    return total


def get_client_ip(handler: http.server.BaseHTTPRequestHandler) -> IPAddress:
    x_real_ip = handler.headers.get("X-Real-IP")
    if x_real_ip:
        return IPAddress(x_real_ip.strip())
    xff = handler.headers.get("X-Forwarded-For")
    if xff:
        return IPAddress(xff.split(",")[0].strip())
    return IPAddress(handler.client_address[0])


def is_private_ip(ip_value: str) -> bool:
    try:
        return ipaddress.ip_address(ip_value).is_private
    except ValueError:
        return False


def get_recent_uploads(limit: int | None = None) -> str:
    db = load_db()
    all_files: list[dict[str, object]] = []
    for ip, files in db.items():
        for entry in files:
            all_files.append(
                {
                    "filename": entry.filename,
                    "size": entry.size,
                    "time": entry.time,
                    "ip": ip,
                }
            )
    all_files.sort(key=lambda x: float(x.get("time", 0)), reverse=True)
    if limit is not None:
        all_files = all_files[:limit]
    if not all_files:
        return '<tr><td colspan="5">No uploads yet</td></tr>'
    items: list[str] = []
    for entry in all_files:
        filename = str(entry.get("filename", "unknown"))
        display_name = clean_display_name(filename)
        size_mb = int(entry.get("size", 0)) / 1024**2
        ts = float(entry.get("time", 0))
        ts_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        exp_ts = ts + MAX_AGE_SECONDS
        exp_str = datetime.datetime.fromtimestamp(exp_ts).strftime("%Y-%m-%d %H:%M:%S")
        ip_value = str(entry.get("ip", "unknown"))
        country_html = get_country_display(ip_value)
        items.append(
            "<tr>"
            f'<td>{escape(display_name)}</td>'
            f"<td>{size_mb:.2f} MB</td>"
            f"<td>{ts_str}</td>"
            f"<td>{exp_str}</td>"
            f"<td>{escape(ip_value)}</td>"
            f"<td>{country_html}</td>"
            "</tr>"
        )
    return "".join(items)


def clean_display_name(filename: str) -> str:
    if (
        len(filename) > 33
        and filename[32] == "_"
        and all(c in "0123456789abcdef" for c in filename[:32])
    ):
        return filename[33:]
    return filename


_geo_cache: dict[str, str] = {}


def get_country_display(ip_value: str) -> str:
    if not ip_value:
        return ""
    if is_private_ip(ip_value):
        return "LAN"
    cached = _geo_cache.get(ip_value)
    if cached is not None:
        return cached
    code = lookup_country_code(ip_value)
    if not code:
        _geo_cache[ip_value] = ""
        return ""
    flag = country_code_to_flag(code)
    text = f"{code.upper()} {flag}" if flag else f"{code.upper()}"
    _geo_cache[ip_value] = text
    return text


def lookup_country_code(ip_value: str) -> str | None:
    url = f"http://ip-api.com/json/{ip_value}?fields=status,countryCode"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            data: dict[str, object] = json.loads(resp.read().decode("utf-8"))
        if data.get("status") == "success":
            code = data.get("countryCode")
            return str(code) if code else None
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None
    return None


def country_code_to_flag(code: str) -> str:
    if not code or len(code) != 2:
        return ""
    return (
        f'<img class="flag-img" src="https://flagcdn.com/w20/{code.lower()}.png"'
        f' alt="{code.upper()} flag">'
    )


def get_public_base_url(handler: http.server.BaseHTTPRequestHandler) -> str:
    configured = PUBLIC_BASE_URL.rstrip("/")
    if configured:
        return configured
    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
    host = handler.headers.get("Host", f"localhost:{PORT}")
    return f"{forwarded_proto}://{host}"


def start_cleanup_thread() -> None:
    def _loop() -> None:
        while True:
            time.sleep(CLEANUP_INTERVAL_SECONDS)
            cleanup_old_files()

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


last_upload_time: dict[str, float] = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def safe_write(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    def do_POST(self) -> None:
        cleanup_old_files()
        client_ip = get_client_ip(self)

        if self.path not in ("/upload", "/clear"):
            self.send_error(404)
            return

        if RATE_LIMIT_SECONDS and self.path == "/upload":
            last_time = last_upload_time.get(client_ip, 0.0)
            if time.time() - last_time < RATE_LIMIT_SECONDS:
                self.send_error(
                    429, f"Rate limit: wait {RATE_LIMIT_SECONDS} seconds between uploads."
                )
                return

        if self.path == "/clear":
            db = load_db()
            ip_files = db.get(client_ip, [])
            freed_bytes = 0
            for entry in ip_files:
                filepath = os.path.join(UPLOAD_DIR, entry.filename)
                if os.path.exists(filepath):
                    try:
                        size = os.path.getsize(filepath)
                    except OSError:
                        size = 0
                    os.remove(filepath)
                    freed_bytes += size
            if client_ip in db:
                del db[client_ip]
                save_db(db)
            logger.info("Clear: IP=%s, Freed=%d", client_ip, freed_bytes)
            self.send_response(200)
            self.end_headers()
            freed_mb = freed_bytes / 1024**2
            self.safe_write(f"Cleared files for IP {client_ip}. Freed {freed_mb:.2f} MB".encode())
            return

        db = load_db()
        ip_files = db.get(client_ip, [])
        content_type = self.headers.get("Content-Type", "")

        form: cgi.FieldStorage | None = None
        data: bytes = b""
        if "multipart/form-data" in content_type:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type},
            )
            if "file" in form:
                fileitem = form["file"]
                data = fileitem.file.read()
            else:
                self.send_error(400)
                return
        else:
            content_length = int(self.headers["Content-Length"])
            data = self.rfile.read(content_length)

        file_size = len(data)
        current_ip_size = sum(f.size for f in ip_files)
        if current_ip_size + file_size > IP_LIMIT_GB * 1024**3:
            self.send_error(413, "IP limit exceeded. Run ./upload.sh --clear and retry.")
            return

        current_used = get_current_used()
        if current_used + file_size > MAX_STORAGE_GB * 1024**3:
            self.send_error(413, "Not enough allocated space")
            return

        if form:
            fileitem = form["file"]
            if fileitem.filename:
                orig_name = fileitem.filename
                filename = f"{uuid.uuid4().hex}_{orig_name}"
            else:
                filename = str(uuid.uuid4()) + ".bin"
        else:
            filename = str(uuid.uuid4()) + ".bin"

        filepath = os.path.join(UPLOAD_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(data)

        ip_files.append(FileEntry(filename=filename, size=file_size, time=time.time()))
        db[client_ip] = ip_files
        save_db(db)
        last_upload_time[client_ip] = time.time()
        logger.info("Upload: IP=%s, File=%s, Size=%d", client_ip, filename, file_size)

        disk_free = shutil.disk_usage(UPLOAD_DIR).free
        allocated_remaining = MAX_STORAGE_GB * 1024**3 - current_used - file_size
        ip_remaining = IP_LIMIT_GB * 1024**3 - sum(f.size for f in ip_files)
        expire_time = time.time() + MAX_AGE_SECONDS
        expire_str = datetime.datetime.fromtimestamp(expire_time).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        base_url = PUBLIC_BASE_URL.rstrip("/")
        encoded_filename = urllib.parse.quote(filename, safe="")
        public_download = (
            f"{base_url}/download/{encoded_filename}"
            if base_url
            else f"/download/{encoded_filename}"
        )
        response = (
            f"{public_download}\n"
            f"Your IP: {client_ip}\n"
            f"File size: {file_size / 1024**2:.2f} MB\n"
            f"Expires: {expire_str}\n"
            f"Disk space left: {disk_free / 1024**3:.2f} GB\n"
            f"Allocated space remaining: {allocated_remaining / 1024**3:.2f} GB\n"
            f"IP limit remaining: {ip_remaining / 1024**3:.2f} GB"
        )
        self.send_response(200)
        self.end_headers()
        self.safe_write(response.encode())

    def do_GET(self) -> None:
        cleanup_old_files()
        if self.path in ("/", "/index.html"):
            used_bytes = get_current_used()
            used_gb = used_bytes / 1024**3
            total_gb = MAX_STORAGE_GB
            percentage = (used_gb / total_gb) * 100 if total_gb > 0 else 0
            client_ip = get_client_ip(self)
            db = load_db()
            ip_files = db.get(client_ip, [])
            ip_used_bytes = sum(entry.size for entry in ip_files)
            ip_used_gb = ip_used_bytes / 1024**3
            ip_percentage = (ip_used_gb / IP_LIMIT_GB) * 100 if IP_LIMIT_GB > 0 else 0
            recent_uploads_html = get_recent_uploads()
            base_url = get_public_base_url(self)
            html = INDEX_TEMPLATE.format(
                used_gb=used_gb,
                total_gb=total_gb,
                percentage=percentage,
                ip_used_gb=ip_used_gb,
                ip_percentage=ip_percentage,
                max_age_hours=config.max_age_hours,
                ip_limit_gb=config.ip_limit_gb,
                public_base_url=base_url,
                base_url=base_url,
                recent_uploads_html=recent_uploads_html,
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.safe_write(html.encode())
        elif self.path in ("/uploads", "/uploads.html"):
            base_url = get_public_base_url(self)
            recent_uploads_html = get_recent_uploads()
            html = UPLOADS_TEMPLATE.format(
                base_url=base_url,
                recent_uploads_html=recent_uploads_html,
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.safe_write(html.encode())
        elif self.path == "/robots.txt":
            base_url = get_public_base_url(self)
            content = ROBOTS_TEMPLATE.format(base_url=base_url)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.safe_write(content.encode("utf-8"))
        elif self.path == "/sitemap.xml":
            base_url = get_public_base_url(self)
            lastmod = datetime.datetime.utcnow().date().isoformat()
            content = SITEMAP_TEMPLATE.format(base_url=base_url, lastmod=lastmod)
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.end_headers()
            self.safe_write(content.encode("utf-8"))
        elif self.path == "/upload.sh":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Disposition", 'attachment; filename="upload.sh"')
            self.end_headers()
            self.safe_write(UPLOAD_SCRIPT.encode())
        elif self.path.startswith("/static/"):
            rel_path = self.path[len("/static/"):]
            if rel_path not in ("styles.css", "app.js"):
                self.send_error(404)
                return
            file_path = os.path.join(STATIC_DIR, rel_path)
            if not os.path.exists(file_path):
                self.send_error(404)
                return
            content_type = "text/css" if rel_path.endswith(".css") else "application/javascript"
            with open(file_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.safe_write(data)
        elif self.path.startswith("/download/"):
            raw_name = self.path[len("/download/"):].split("?")[0]
            filename = urllib.parse.unquote(raw_name)
            filepath = os.path.join(UPLOAD_DIR, filename)
            if os.path.exists(filepath):
                logger.info("Download: IP=%s, File=%s", get_client_ip(self), filename)
                download_name = clean_display_name(filename)
                download_name_safe = download_name.replace('"', "")
                download_name_encoded = urllib.parse.quote(download_name_safe, safe="")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{download_name_safe}"; filename*=UTF-8\'\'{download_name_encoded}',
                )
                self.end_headers()
                with open(filepath, "rb") as f:
                    self.safe_write(f.read())
            else:
                self.send_error(404)
        else:
            self.send_error(404)


def run() -> None:
    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    with ReusableTCPServer(("", PORT), Handler) as httpd:
        start_cleanup_thread()
        print(f"Serving on port {PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    run()
