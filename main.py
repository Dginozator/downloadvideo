import asyncio
import json
import os
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

RUTUBE_DOMAINS = ("rutube.ru", "www.rutube.ru")
VALID_TOKENS = set(t.strip() for t in os.getenv("API_TOKENS", "").split(",") if t.strip())


def validate_rutube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.netloc in RUTUBE_DOMAINS and parsed.scheme in ("http", "https")
    except Exception:
        return False


def verify_token(token: str) -> bool:
    return token in VALID_TOKENS if VALID_TOKENS else False


async def get_stream_url(video_url: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "-f", "bv*+ba/b",
        "-j",
        "--no-playlist",
        "--no-warnings",
        video_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
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


def build_ffmpeg_cmd(video_url: str, audio_url: str | None, headers: dict) -> list[str]:
    ua = headers.get("User-Agent", "Mozilla/5.0")
    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-user_agent", ua,
        "-referer", "https://rutube.ru/",
        "-i", video_url,
    ]
    if audio_url:
        cmd += [
            "-user_agent", ua,
            "-referer", "https://rutube.ru/",
            "-i", audio_url,
            "-map", "0:v", "-map", "1:a",
        ]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]

    cmd += [
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]
    return cmd


async def stream_video(video_url: str, request: Request):
    streams = await get_stream_url(video_url)
    cmd = build_ffmpeg_cmd(streams["video"], streams["audio"], streams["headers"])

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

    return StreamingResponse(
        generate(),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "none",
            "Cache-Control": "no-cache",
        },
    )


@app.get("/")
async def stream(v: str, auth: str, request: Request):
    if not validate_rutube_url(v):
        raise HTTPException(status_code=400, detail="Invalid Rutube URL")
    if not verify_token(auth):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return await stream_video(v, request)


@app.get("/health")
async def health():
    return {"status": "ok"}
