import os
import signal
import subprocess
from urllib.parse import quote, urlparse

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


def build_command(video_url: str) -> list[str]:
    return [
        "yt-dlp",
        "-f", "bestvideo+bestaudio/best",
        "--no-playlist",
        "--no-warnings",
        "-o", "-",
        video_url,
    ]


def build_ffmpeg_command() -> list[str]:
    return [
        "ffmpeg",
        "-i", "pipe:0",
        "-c", "copy",
        "-movflags", "faststart",
        "-f", "mp4",
        "pipe:1",
    ]


async def stream_pipeline(video_url: str, request: Request):
    yt_proc = None
    ff_proc = None

    try:
        yt_proc = subprocess.Popen(
            build_command(video_url),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=65536,
        )

        ff_proc = subprocess.Popen(
            build_ffmpeg_command(),
            stdin=yt_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=65536,
        )

        yt_proc.stdout.close()

        def kill_tree(proc):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

        async def generate():
            try:
                while True:
                    if await request.is_disconnected():
                        kill_tree(ff_proc)
                        kill_tree(yt_proc)
                        break

                    chunk = ff_proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk

            finally:
                kill_tree(ff_proc)
                kill_tree(yt_proc)
                if ff_proc:
                    ff_proc.wait(timeout=5)
                if yt_proc:
                    yt_proc.wait(timeout=5)

        return StreamingResponse(
            generate(),
            media_type="video/mp4",
            headers={
                "Accept-Ranges": "none",
                "Cache-Control": "no-cache",
            },
        )

    except subprocess.SubprocessError as e:
        if yt_proc:
            yt_proc.kill()
        if ff_proc:
            ff_proc.kill()
        raise HTTPException(status_code=502, detail=f"Pipeline error: {e}")
    except Exception as e:
        if yt_proc:
            yt_proc.kill()
        if ff_proc:
            ff_proc.kill()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def stream(v: str, auth: str, request: Request):
    if not validate_rutube_url(v):
        raise HTTPException(status_code=400, detail="Invalid Rutube URL")
    
    if not verify_token(auth):
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    return await stream_pipeline(v, request)

@app.get("/health")
async def health():
    return {"status": "ok"}
