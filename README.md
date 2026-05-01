# Self-Hosted File App

A tiny local file hosting app you can run on your own machine/server and expose with port-forwarding.

## Features

- Upload files from browser
- Download files via links
- Delete files
- Stores files in a local folder (`./storage` by default)

## Run

```bash
python3 app.py
```

Open: `http://localhost:8080`

## Environment variables

- `FILE_APP_HOST` (default `0.0.0.0`)
- `FILE_APP_PORT` (default `8080`)
- `FILE_APP_STORAGE` (default `storage`)
- `FILE_APP_MAX_UPLOAD_MB` (default `1024`)

## Port forwarding tips

- Router NAT: forward external port to this host's `8080`.
- SSH tunnel example: `ssh -R 8080:localhost:8080 user@public-server`
- Safer approach: run behind a reverse proxy with auth.

## Warning

This app has **no authentication**. Do not expose it publicly without protection.
