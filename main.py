import asyncio
import json
import os
import re
from urllib.parse import urlparse, quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, HTMLResponse

app = FastAPI()

RUTUBE_DOMAINS = ("rutube.ru", "www.rutube.ru")
YOUTUBE_DOMAINS = (
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "youtu.be", "music.youtube.com",
)

VALID_TOKENS = set(t.strip() for t in os.getenv("API_TOKENS", "").split(",") if t.strip())
YT_COOKIES = os.getenv("YT_COOKIES_FILE", "").strip()

MAX_DURATION = 4 * 60 * 60
MAX_START = 24 * 60 * 60


def detect_source(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        if parsed.netloc in RUTUBE_DOMAINS:
            return "rutube"
        if parsed.netloc in YOUTUBE_DOMAINS:
            return "youtube"
        return None
    except Exception:
        return None


def verify_token(token: str) -> bool:
    return token in VALID_TOKENS if VALID_TOKENS else False


def referer_for(source: str) -> str:
    return "https://www.youtube.com/" if source == "youtube" else "https://rutube.ru/"


def sanitize_title(title: str) -> str:
    t = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "", title)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip(". ")
    return t[:120]


def build_filenames(
    title: str, vid_id: str, start: float | None, duration: float | None
) -> tuple[str, str]:
    base = sanitize_title(title)
    if not base:
        base = re.sub(r"[^A-Za-z0-9_-]", "_", vid_id)[:64] or "video"

    suffix = ""
    if start is not None:
        suffix += f"_t{int(start)}"
    if duration is not None:
        suffix += f"_d{int(duration)}"

    utf8_name = f"{base}{suffix}.mp4"
    ascii_base = re.sub(r"[^A-Za-z0-9 ._-]", "_", base).strip("_ ") or (vid_id or "video")
    ascii_name = f"{ascii_base}{suffix}.mp4"
    return ascii_name, utf8_name


async def get_stream_url(video_url: str, source: str) -> dict:
    args = [
        "yt-dlp",
        "-f", "bv*+ba/b",
        "-j",
        "--no-playlist",
        "--no-warnings",
    ]
    if source == "youtube" and YT_COOKIES:
        args += ["--cookies", YT_COOKIES]
    args.append(video_url)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(504, "yt-dlp timeout")

    if proc.returncode != 0:
        raise HTTPException(502, f"yt-dlp: {stderr.decode()[-500:]}")

    info = json.loads(stdout)
    title = info.get("title") or ""
    vid_id = info.get("id") or ""

    if "requested_formats" in info:
        fmts = info["requested_formats"]
        video = next((f for f in fmts if f.get("vcodec") != "none"), None)
        audio = next(
            (f for f in fmts if f.get("acodec") != "none" and f.get("vcodec") == "none"),
            None,
        )
        return {
            "video": video["url"],
            "audio": audio["url"] if audio else None,
            "headers": video.get("http_headers", {}),
            "title": title,
            "id": vid_id,
        }
    return {
        "video": info["url"],
        "audio": None,
        "headers": info.get("http_headers", {}),
        "title": title,
        "id": vid_id,
    }


def build_ffmpeg_cmd(
    video_url: str,
    audio_url: str | None,
    headers: dict,
    referer: str,
    start: float | None,
    duration: float | None,
) -> list[str]:
    ua = headers.get("User-Agent", "Mozilla/5.0")
    cmd = ["ffmpeg", "-loglevel", "warning"]

    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += [
        "-user_agent", ua,
        "-referer", referer,
        "-i", video_url,
    ]

    if audio_url:
        if start is not None:
            cmd += ["-ss", str(start)]
        cmd += [
            "-user_agent", ua,
            "-referer", referer,
            "-i", audio_url,
            "-map", "0:v", "-map", "1:a",
        ]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]

    if duration is not None:
        cmd += ["-t", str(duration)]

    cmd += [
        "-c:v", "copy",
        "-c:a", "aac",
        "-avoid_negative_ts", "make_zero",
        "-fflags", "+genpts",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]
    return cmd


async def stream_video(
    video_url: str,
    source: str,
    request: Request,
    start: float | None,
    duration: float | None,
    as_attachment: bool,
):
    streams = await get_stream_url(video_url, source)
    cmd = build_ffmpeg_cmd(
        streams["video"],
        streams["audio"],
        streams["headers"],
        referer_for(source),
        start,
        duration,
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def log_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            print(f"[ffmpeg] {line.decode(errors='replace').rstrip()}", flush=True)

    stderr_task = asyncio.create_task(log_stderr())

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            await stderr_task

    ascii_name, utf8_name = build_filenames(
        streams.get("title", ""), streams.get("id", ""), start, duration
    )
    disposition_type = "attachment" if as_attachment else "inline"
    disposition = (
        f'{disposition_type}; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(utf8_name)}"
    )

    return StreamingResponse(
        generate(),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "none",
            "Cache-Control": "no-cache",
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
        },
    )


def validate_request(v: str, auth: str) -> str:
    source = detect_source(v)
    if source is None:
        raise HTTPException(status_code=400, detail="Unsupported URL (rutube/youtube only)")
    if not verify_token(auth):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return source


UI_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Proxy</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.3rem; margin-bottom: 1rem; }
  form { display: grid; gap: .6rem; grid-template-columns: 1fr 1fr; }
  form .full { grid-column: 1 / -1; }
  label { display: block; font-size: .85rem; margin-bottom: .2rem; opacity: .8; }
  input { width: 100%; padding: .5rem; font-size: .95rem; box-sizing: border-box; }
  button { padding: .6rem 1.2rem; font-size: 1rem; cursor: pointer; }
  .row { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; margin-top: 1rem; }
  .row a { word-break: break-all; }
  video { width: 100%; margin-top: 1rem; background: #000; }
  .hint { font-size: .8rem; opacity: .7; margin-top: .3rem; }
</style>
</head>
<body>
<h1>Video Proxy</h1>
<form id="f" onsubmit="return go(event)">
  <div class="full">
    <label>URL видео (YouTube / Rutube)</label>
    <input name="v" required placeholder="https://...">
  </div>
  <div class="full">
    <label>Токен</label>
    <input name="auth" required type="password">
  </div>
  <div>
    <label>Начало, сек</label>
    <input name="t" type="number" min="0" step="1" placeholder="0">
  </div>
  <div>
    <label>Длительность, сек</label>
    <input name="duration" type="number" min="1" step="1" placeholder="до конца">
  </div>
  <div class="full">
    <button type="submit">Показать</button>
  </div>
</form>

<div id="out"></div>

<script>
function buildQuery(data) {
  const p = new URLSearchParams();
  p.set("v", data.v);
  p.set("auth", data.auth);
  if (data.t) p.set("t", data.t);
  if (data.duration) p.set("duration", data.duration);
  return p.toString();
}

function go(e) {
  e.preventDefault();
  const fd = new FormData(document.getElementById("f"));
  const data = Object.fromEntries(fd.entries());
  if (!data.v || !data.auth) return false;

  const qs = buildQuery(data) + "&_=" + Date.now();
  const streamUrl = "/stream?" + qs;
  const downloadUrl = "/download?" + qs;

  const out = document.getElementById("out");
  out.innerHTML =
    '<video id="vid" controls autoplay src="' + streamUrl + '"></video>' +
    '<div class="row">' +
      '<a href="' + downloadUrl + '" download>Скачать MP4</a> ' +
      '<a href="' + streamUrl + '" target="_blank">Открыть поток в новой вкладке</a>' +
    '</div>' +
    '<div id="verr" class="hint"></div>';

  document.getElementById("vid").addEventListener("error", async () => {
    try {
      const r = await fetch(streamUrl);
      const text = await r.text();
      document.getElementById("verr").textContent =
        "Ошибка " + r.status + ": " + text.slice(0, 500);
    } catch (err) {
      document.getElementById("verr").textContent = "Fetch failed: " + err;
    }
  });
  return false;
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def ui():
    return UI_HTML


@app.get("/stream")
async def stream_inline(
    request: Request,
    v: str,
    auth: str,
    t: float | None = Query(None, ge=0, le=MAX_START),
    duration: float | None = Query(None, gt=0, le=MAX_DURATION),
):
    source = validate_request(v, auth)
    return await stream_video(v, source, request, t, duration, as_attachment=False)


@app.get("/download")
async def download(
    request: Request,
    v: str,
    auth: str,
    t: float | None = Query(None, ge=0, le=MAX_START),
    duration: float | None = Query(None, gt=0, le=MAX_DURATION),
):
    source = validate_request(v, auth)
    return await stream_video(v, source, request, t, duration, as_attachment=True)


@app.get("/health")
async def health():
    return {"status": "ok"}
