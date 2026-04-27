import asyncio
import json
import os
import re
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

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


def safe_filename(video_url: str, start: float | None, duration: float | None) -> str:
    parsed = urlparse(video_url)
    vid = ""
    if parsed.netloc in YOUTUBE_DOMAINS:
        if parsed.netloc == "youtu.be":
            vid = parsed.path.strip("/").split("/")[0]
        else:
            from urllib.parse import parse_qs
            vid = (parse_qs(parsed.query).get("v") or [""])[0]
    if not vid:
        path = parsed.path.rstrip("/")
        vid = path.split("/")[-1] if path else "video"
    vid = re.sub(r"[^A-Za-z0-9_-]", "_", vid)[:64] or "video"
    suffix = ""
    if start is not None:
        suffix += f"_t{int(start)}"
    if duration is not None:
        suffix += f"_d{int(duration)}"
    return f"{vid}{suffix}.mp4"


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
        }
    return {
        "video": info["url"],
        "audio": None,
        "headers": info.get("http_headers", {}),
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

    filename = safe_filename(video_url, start, duration)
    return StreamingResponse(
        generate(),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "none",
            "Cache-Control": "no-cache",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/")
async def stream(
    request: Request,
    v: str,
    auth: str,
    t: float | None = Query(None, ge=0, le=MAX_START),
    duration: float | None = Query(None, gt=0, le=MAX_DURATION),
):
    source = detect_source(v)
    if source is None:
        raise HTTPException(status_code=400, detail="Unsupported URL (rutube/youtube only)")
    if not verify_token(auth):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return await stream_video(v, source, request, t, duration)


@app.get("/health")
async def health():
    return {"status": "ok"}
