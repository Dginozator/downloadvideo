# Video Stream Proxy

FastAPI service that proxies video streams from Rutube and YouTube through ffmpeg, producing a fragmented MP4 stream on the fly. Supports partial downloads via start time and duration parameters.

## How it works

1. Client requests a video URL with an auth token.
2. `yt-dlp` resolves the best video+audio formats and extracts direct CDN URLs.
3. `ffmpeg` pulls video and audio streams, remuxes video (copy) and re-encodes audio to AAC, outputs fragmented MP4 to stdout.
4. Output is streamed to the client as `video/mp4`.

Video is copied without re-encoding (fast, no CPU cost). Audio is transcoded to AAC for container compatibility.

## Requirements

- Python 3.10+
- `ffmpeg` in PATH
- `yt-dlp` in PATH (install separately, keep it updated)
- Python packages: `fastapi`, `uvicorn`

```bash
pip install fastapi uvicorn
pip install -U yt-dlp
apt install ffmpeg  # or equivalent
```

## Configuration

Environment variables:

| Variable | Required | Description |
|---|---|---|
| `API_TOKENS` | yes | Comma-separated list of valid auth tokens. If empty, all requests are rejected. |
| `YT_COOKIES_FILE` | no | Path to Netscape-format cookies file for YouTube. Needed for age-restricted, region-locked, or bot-challenged videos. |

Example:

```bash
export API_TOKENS="token1,token2,token3"
export YT_COOKIES_FILE="/etc/secrets/yt_cookies.txt"
```

## Running

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Production:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

## API

### `GET /`

Stream a video.

Query parameters:

| Name | Type | Required | Description |
|---|---|---|---|
| `v` | string | yes | Video URL (Rutube or YouTube) |
| `auth` | string | yes | API token from `API_TOKENS` |
| `t` | float | no | Start time in seconds (0 to 86400) |
| `duration` | float | no | Fragment duration in seconds (up to 14400) |

Response: `video/mp4` stream (fragmented MP4, no seeking support).

Examples:

```
GET /?v=https://rutube.ru/video/abc123/&auth=token1
GET /?v=https://youtu.be/dQw4w9WgXcQ&auth=token1&t=60&duration=30
GET /?v=https://www.youtube.com/watch?v=XXX&auth=token1&t=120
```

### `GET /health`

Health check. Returns `{"status": "ok"}`.

## Supported sources

- Rutube: `rutube.ru`, `www.rutube.ru`
- YouTube: `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`, `music.youtube.com`

## Limitations

**Fragmented MP4 output.** Output is streamed live, so the `moov` atom is written at the start without duration metadata. Many desktop players (Windows Media Player, some versions of VLC) cannot seek within such files. Streaming playback works everywhere.

**Keyframe-aligned seeking.** When `t` is used, the actual start position snaps to the nearest keyframe before the requested time (typically within 2-5 seconds). Frame-accurate seeking would require video re-encoding, which is intentionally avoided.

**No Range requests.** `Accept-Ranges: none` is set. Clients cannot resume interrupted downloads or seek by byte range.

**YouTube on datacenter IPs.** YouTube aggressively blocks requests from known datacenter ranges (AWS, DigitalOcean, Hetzner, etc.) with bot challenges. Workarounds:
- Provide a cookies file via `YT_COOKIES_FILE` exported from a logged-in browser session.
- Route `yt-dlp` through a residential proxy.
- Run the service from a residential IP.

**Signed URL IP binding.** YouTube signed URLs are tied to the IP that requested them. The `yt-dlp` resolver and `ffmpeg` must run on the same machine (or behind the same egress IP).

**yt-dlp cold start.** Each request spawns `yt-dlp` (~1-2s overhead). For repeated access to the same video, add a cache layer keyed by video ID.

## Architecture notes

- `yt-dlp` runs with a 30-second timeout. Hung processes are killed.
- `ffmpeg` uses input seek (`-ss` before `-i`) for fast seeking over HLS/DASH segments.
- `-avoid_negative_ts make_zero` and `-fflags +genpts` prevent A/V drift after mid-stream cuts.
- On client disconnect, `ffmpeg` is terminated (SIGTERM, then SIGKILL after 5s).
- stderr from `ffmpeg` is logged line-by-line to stdout.

## Legal

This service proxies third-party video content. You are responsible for compliance with the terms of service of the source platforms and applicable copyright law in your jurisdiction. Do not deploy publicly without considering these implications.

## Example systemd unit

```ini
[Unit]
Description=Video Stream Proxy
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/video-proxy
Environment="API_TOKENS=change_me"
Environment="YT_COOKIES_FILE=/etc/secrets/yt_cookies.txt"
ExecStart=/opt/video-proxy/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=on-failure

[Install]
WantedBy=multi-user.target
```
