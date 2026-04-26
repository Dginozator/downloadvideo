import asyncio
import re
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import yt_dlp

app = FastAPI()

RUTUBE_DOMAINS = ("rutube.ru", "www.rutube.ru")
RUTUBE_VIDEO_RE = re.compile(r"^/video/[0-9a-f]{32}/?\$")


def validate_rutube_url(url: str) -> None:
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Invalid URL scheme")
    if parsed.netloc not in RUTUBE_DOMAINS:
        raise HTTPException(status_code=400, detail="URL must be from rutube.ru")
    if RUTUBE_VIDEO_RE.match(parsed.path) is None:
        raise HTTPException(status_code=400, detail="Invalid Rutube video URL")


async def get_stream_url(video_url: str) -> dict:
    loop = asyncio.get_running_loop()

    def _extract() -> dict:
        ydl_opts = {
            "format": "bv*+ba/b",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        if "requested_formats" in info:
            video_fmt = next(f for f in info["requested_formats"] if f.get("vcodec") != "none")
            audio_fmt = next(f for f in info["requested_formats"] if f.get("acodec") != "none" and f.get("vcodec") == "none")
            return {
                "video": video_fmt["url"],
                "audio": audio_fmt["url"],
                "headers": info.get("http_headers") or video_fmt.get("http_headers", {}),
            }

        return {
            "video": info["url"],
            "audio": None,
            "headers": info.get("http_headers", {}),
        }

    return await loop.run_in_executor(None, _extract)


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


@app.get("/stream")
async def stream_video(url: str, request: Request):
    validate_rutube_url(url)

    streams = await get_stream_url(url)
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

    async def iter_stdout():
        assert proc.stdout is not None
        try:
            while True:
                if await request.is_disconnected():
                    break
                chunk = await proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            await stderr_task

    return StreamingResponse(iter_stdout(), media_type="video/mp4")
