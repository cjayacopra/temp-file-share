<div align="center">
  <img src="./images/logo.svg" width="360" alt="temp-file-share logo" />

# Temp File Share

**Upload files and folders from your terminal and get a download link.**

[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white&style=flat-square)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-20.10%2B-2496ED?logo=docker&logoColor=white&style=flat-square)](https://www.docker.com/)
[![License: AGPL](https://img.shields.io/badge/license-AGPL--3.0-blue.svg?style=flat-square)](http://www.gnu.org/licenses/agpl-3.0)
[![Built with](https://img.shields.io/badge/zero-dependencies-brightgreen?style=flat-square)](backend/requirements.txt)

[Overview](#overview) • [Quick Start](#quick-start) • [Usage](#usage) • [API](#api) • [Configuration](#configuration) • [Deployment](#deployment)

</div>

## Overview

Temp File Share is a lightweight, zero-dependency file sharing server. Upload files from your terminal (or the web UI), get a shareable download link, and the files expire automatically. Folders and multiple files are zipped on the client side before upload.

<p align="center">
  <img src="./images/ui.png" alt="Temp File Share UI" width="600" style="border-radius: 8px;">
</p>

### Features

- **One-command upload** — Upload files and folders with a single bash command
- **Auto-zip** — Folders and multiple files are automatically compressed into a tarball before upload
- **Automatic expiry** — Old files are cleaned up on a background schedule
- **Per-IP limits** — Storage quotas and rate limiting prevent abuse
- **Geo-location** — IP addresses are resolved to country flags in the web UI
- **Web UI** — Browse recent uploads with expiry times
- **Proxy-aware** — Respects `X-Real-IP` and `X-Forwarded-For` headers behind a reverse proxy
- **Zero dependencies** — Pure Python stdlib, no requirements to install

## Quick Start

### Run with Docker

```bash
docker run -d \
  -p 54000:54000 \
  -v uploads:/app/uploads \
  -v data:/app/data \
  --restart unless-stopped \
  ghcr.io/nooblk-98/temp-file-share:latest
```

### Run from source

```bash
git clone https://github.com/nooblk-98/temp-file-share.git
cd temp-file-share/backend
mkdir uploads data
python3 backend.py
```

> [!TIP]
> Override config settings by editing `backend/config.json` before starting the server.

## Usage

### Upload files

The `upload.sh` script handles single files, multiple files, and directories:

```bash
# Upload a single file
./upload.sh document.pdf

# Upload multiple files and folders
./upload.sh photos/ documents/ notes.txt

# Upload a folder (auto-zipped)
./upload.sh my-project/
```

Sample output:

```
Single file detected, uploading directly: document.pdf
Uploading...
https://dl.itsnooblk.com/download/a64c76df94664379815186a6cf9c55e7_document.pdf
Your IP: 80.225.221.245
File size: 0.03 MB
Expires: 2026-01-25 22:37:16
Disk space left: 117.48 GB
Allocated space remaining: 50.00 GB
IP limit remaining: 10.00 GB
```

### Clear your uploads

Remove all files uploaded from your IP address:

```bash
./upload.sh --clear
```

### Remote server

Set the `BACKEND_URL` environment variable to point to a remote instance:

```bash
export BACKEND_URL=https://dl.itsnooblk.com
./upload.sh document.pdf
```

> [!NOTE]
> The bash script (`upload.sh`) is also served directly by the server at `GET /upload.sh`, making it easy to distribute.

## API

The server exposes a simple HTTP API:

### `POST /upload`

Upload a file. Accepts `multipart/form-data` (with a `file` field) or raw binary body.

```bash
# Multipart upload
curl -X POST -F "file=@photo.jpg" http://localhost:54000/upload

# Raw binary upload
curl -X POST -H "Content-Type: application/octet-stream" \
  --data-binary @photo.jpg http://localhost:54000/upload
```

**Response:** Plain text with download URL, metadata, and remaining quotas.

### `GET /download/<filename>`

Download a previously uploaded file.

```bash
curl -O http://localhost:54000/download/<filename>
```

### `POST /clear`

Delete all files uploaded from your IP address.

```bash
curl -X POST http://localhost:54000/clear
```

### `GET /` or `GET /index.html`

Serves the main web UI showing storage usage and recent uploads.

### `GET /uploads` or `GET /uploads.html`

Serves the upload listing page with file details, expiry times, and country flags.

## Configuration

Settings are defined in `backend/config.json`:

```json
{
  "UPLOAD_DIR": "uploads",
  "MAX_STORAGE_GB": 50,
  "MAX_AGE_HOURS": 5,
  "IP_LIMIT_GB": 10,
  "PUBLIC_BASE_URL": "https://dl.itsnooblk.com",
  "FILES_DB": "data/files_db.json",
  "RATE_LIMIT_SECONDS": 2,
  "CLEANUP_INTERVAL_SECONDS": 300
}
```

| Key | Default | Description |
|---|---|---|
| `UPLOAD_DIR` | `uploads` | Directory for stored files |
| `MAX_STORAGE_GB` | `50` | Total storage limit in GB |
| `MAX_AGE_HOURS` | `5` | File expiry in hours |
| `IP_LIMIT_GB` | `10` | Storage limit per IP address |
| `PUBLIC_BASE_URL` | `""` | Public-facing base URL for download links |
| `FILES_DB` | `data/files_db.json` | Path to the file metadata database |
| `RATE_LIMIT_SECONDS` | `0` | Cooldown between uploads (0 = disabled) |
| `CLEANUP_INTERVAL_SECONDS` | `300` | Expired file cleanup interval |

## Deployment

### Docker Compose

A `docker-compose.yaml` is included for production deployments:

```yaml
services:
  backend:
    build: ./backend
    network_mode: host
    volumes:
      - /opt/temp-file-share/uploads:/app/uploads
      - /opt/temp-file-share/data:/app/data
    restart: unless-stopped
```

```bash
docker-compose up -d
```

### Reverse proxy

The server respects `X-Real-IP` and `X-Forwarded-For` headers. When behind nginx:

```nginx
server {
    listen 443 ssl;
    server_name dl.example.com;

    location / {
        proxy_pass http://127.0.0.1:54000;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 0;
    }
}
```

> [!IMPORTANT]
> Set `PUBLIC_BASE_URL` in config to match your public domain so download links resolve correctly.

## Project Structure

```
├── backend/
│   ├── app.py              # HTTP server (stdlib only)
│   ├── backend.py          # Entry point
│   ├── config.json         # Server configuration
│   ├── Dockerfile          # Container image
│   ├── templates/          # HTML templates
│   ├── static/             # CSS and JS assets
│   └── scripts/
│       └── upload.sh       # Client upload script
├── docker-compose.yaml     # Production deployment
├── images/                 # Logo and screenshots
└── upload.sh               # Client upload script (root convenience copy)
```
