"""Minimal working version."""

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from ..state import StreamSession, state

router = APIRouter(tags=["stream"])

TRANSCODE_DIR = Path("/tmp/tablo_transcode")
TRANSCODE_DIR.mkdir(exist_ok=True)

MAX_TRANSCODE_SESSIONS = 4

# Stop a transcode this long after its last segment request. Mobile browsers
# that get backgrounded or killed never send the stop request, so without this
# their FFmpeg keeps running and starves whoever is still watching.
TRANSCODE_IDLE_TIMEOUT = 90
REAPER_INTERVAL = 30

VAAPI_DEVICE = Path("/dev/dri/renderD128")


class Transcode:
    """A running FFmpeg process serving one channel to everyone watching it."""

    def __init__(self, proc: subprocess.Popen, session: StreamSession) -> None:
        self.proc = proc
        self.session = session
        self.last_access = time.monotonic()


transcodes: dict[str, Transcode] = {}  # transcode_id → Transcode


# Kill any FFmpeg processes left over from a previous run and wipe stale dirs.
# After a container restart transcodes is empty but old FFmpeg processes
# may still be alive (or their directories still on disk), which exhaust CPU
# and cause new sessions to time out waiting for their first playlist segment.
def _startup_cleanup():
    try:
        import signal
        result = subprocess.run(["pgrep", "-f", "tablo_transcode"], capture_output=True, text=True)
        for pid in result.stdout.split():
            try:
                import os; os.kill(int(pid), signal.SIGKILL)
            except Exception:
                pass
    except Exception:
        pass
    for d in TRANSCODE_DIR.iterdir():
        try:
            shutil.rmtree(d)
        except Exception:
            pass

_startup_cleanup()


def _probe_vaapi() -> bool:
    """Check once whether the iGPU can actually encode H.264.

    The device node existing is not enough — the driver may be missing or the
    GPU may not expose an encoder — and a broken VAAPI command line fails every
    stream, so fall back to libx264 unless a real encode succeeds.
    """
    if not VAAPI_DEVICE.exists():
        return False
    try:
        probe = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-vaapi_device", str(VAAPI_DEVICE),
                "-f", "lavfi", "-i", "testsrc=size=640x480:rate=30:duration=0.2",
                "-vf", "format=nv12,hwupload",
                "-c:v", "h264_vaapi",
                "-f", "null", "-",
            ],
            capture_output=True,
            timeout=30,
        )
        return probe.returncode == 0
    except Exception:
        return False


VAAPI_ENABLED = _probe_vaapi()
print(f"[transcode] hardware encoding: {'VAAPI' if VAAPI_ENABLED else 'unavailable, using libx264'}")


# ─────────────────────────────────────────────────────────────────────────────
# TRANSCODED
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/transcoded/{session_id}/{path:path}")
async def transcoded_stream(session_id: str, path: str, request: Request):
    # Security: Ensure session_id is a valid hex string to prevent path traversal
    if not all(c in "0123456789abcdefABCDEF" for c in session_id):
        raise HTTPException(400, "Invalid session ID")

    sess = state.get_session(session_id)
    transcode_id = sess.transcode_id if sess else None
    if transcode_id is None:
        raise HTTPException(404, "No transcode running for this session")

    tc = transcodes.get(transcode_id)
    if tc:
        tc.last_access = time.monotonic()

    session_dir = (TRANSCODE_DIR / transcode_id).resolve()
    # Security: Normalize path and prevent traversing out of session_dir
    try:
        file_path = (session_dir / path).resolve()
        if not file_path.is_relative_to(session_dir):
            raise ValueError("Traversal attempt")
    except Exception:
        raise HTTPException(400, "Invalid path")

    # Wait up to 10 seconds (async) for the manifest to appear if it's the playlist
    if path == "playlist.m3u8" and not file_path.exists():
        for _ in range(20):
            if file_path.exists():
                break
            await asyncio.sleep(0.5)

    if not file_path.exists() or not file_path.is_file():
        # Check if FFmpeg is still alive to provide a better error
        status = "unknown"
        if tc:
            status = "alive" if tc.proc.poll() is None else f"exited with {tc.proc.returncode}"
        raise HTTPException(404, f"File not found: {path} (Transcoder: {status})")

    # Official HLS media type
    hls_type = "application/vnd.apple.mpegurl"

    if path.endswith(".m3u8"):
        # Return 200 (not 206) — HLS players expect 200 for playlists
        return Response(
            content=file_path.read_bytes(),
            media_type=hls_type,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache, no-store",
                "Content-Disposition": "inline",
            },
        )
    elif path.endswith(".ts"):
        return FileResponse(
            file_path,
            media_type="video/mp2t",
            headers={"Access-Control-Allow-Origin": "*"}
        )

    return FileResponse(file_path, headers={"Access-Control-Allow-Origin": "*"})


# ---------------------------------------------------------------------------
# Start stream (with smart transcoding)
# ---------------------------------------------------------------------------

@router.post("/stream/{identifier}")
async def start_stream(
    identifier: str,
    request: Request,
    transcode: bool = Query(default=False)
):
    if not state.is_authenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        session_id, sess = await state.start_stream(identifier)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stream error: {e}")

    # NOTE: We use root-relative paths for the frontend so it works through the proxy
    if transcode:
        await start_transcoder(session_id, sess)
        stream_url = f"/api/transcoded/{session_id}/playlist.m3u8"
    else:
        stream_url = f"/api/hls/{session_id}/playlist.m3u8"

    # AirPlay hands the URL to the Apple TV, which fetches the stream itself, so
    # it needs an absolute address rather than one relative to the page.
    base = str(request.base_url).rstrip("/")

    return {
        "session_id": session_id,
        "proxy_url": f"/api/hls/{session_id}/playlist.m3u8",
        "stream_url": stream_url,
        "remote_url": f"{base}{stream_url}",
        "transcoded": transcode
    }


# ---------------------------------------------------------------------------
# Stop stream + cleanup
# ---------------------------------------------------------------------------

@router.delete("/stream/{session_id}")
async def stop_stream(session_id: str):
    if not state.is_authenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Returns the session only once the last viewer of the channel has left,
    # so one viewer closing the player doesn't cut off the others.
    sess = state.stop_session(session_id)
    if sess is not None and sess.transcode_id:
        kill_transcode(sess.transcode_id)

    return {"ok": True}


def kill_transcode(transcode_id: str) -> None:
    """Stop a transcode, release its tuner, and remove its segments."""
    tc = transcodes.pop(transcode_id, None)
    if tc is None:
        return

    tc.proc.kill()
    try:
        tc.proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        tc.proc.terminate()

    tc.session.transcode_id = None
    state.drop_stream(tc.session)
    shutil.rmtree(TRANSCODE_DIR / transcode_id, ignore_errors=True)


async def reap_idle_transcodes() -> None:
    """Stop transcodes that have exited or that nobody is watching any more."""
    while True:
        await asyncio.sleep(REAPER_INTERVAL)
        now = time.monotonic()
        for transcode_id, tc in list(transcodes.items()):
            if tc.proc.poll() is not None:
                print(f"[reaper] transcode {transcode_id} exited with {tc.proc.returncode}")
                kill_transcode(transcode_id)
            elif now - tc.last_access > TRANSCODE_IDLE_TIMEOUT:
                print(f"[reaper] transcode {transcode_id} idle for {TRANSCODE_IDLE_TIMEOUT}s, stopping")
                kill_transcode(transcode_id)


# ---------------------------------------------------------------------------
# Raw HLS proxy
# ---------------------------------------------------------------------------

@router.get("/hls/{session_id}/{path:path}")
async def hls_proxy(session_id: str, path: str, request: Request):
    sess = state.get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Stream session not found")

    if path == "playlist.m3u8":
        target_url = sess.stream.playlist_url
    else:
        # Preserve tokens if passed as query params
        query = str(request.url.query)
        target_url = sess.base_url + "/" + path.lstrip("/")
        if query:
            target_url += "?" + query

    # Forward relevant headers (like Range)
    headers = {}
    if range_header := request.headers.get("range"):
        headers["Range"] = range_header

    try:
        # Use a generator to keep the httpx response context alive while streaming
        async def stream_generator():
            async with state.http.stream("GET", target_url, headers=headers, follow_redirects=True) as resp:
                content_type = resp.headers.get("content-type", "application/octet-stream")

                # Special handling for manifests
                if "mpegurl" in content_type.lower() or path.endswith(".m3u8"):
                    body = await resp.aread()
                    rewritten = _rewrite_manifest(body.decode("utf-8", errors="ignore"), session_id, target_url)
                    yield rewritten.encode("utf-8")
                    return

                # Stream segments
                async for chunk in resp.aiter_bytes():
                    yield chunk

        # We need a first pass to get the headers/status without closing the stream
        # This is tricky with StreamingResponse. Let's do a simple request for headers first
        # OR just use a more robust streaming pattern.

        # Optimized: Start the stream, grab headers, then return the StreamingResponse
        # utilizing the same context.
        resp = await state.http.send(
            state.http.build_request("GET", target_url, headers=headers),
            stream=True,
            follow_redirects=True
        )

        content_type = resp.headers.get("content-type", "application/octet-stream")
        hls_type = "application/vnd.apple.mpegurl"

        if "mpegurl" in content_type.lower() or path.endswith(".m3u8"):
            try:
                body = await resp.aread()
                rewritten = _rewrite_manifest(body.decode("utf-8", errors="ignore"), session_id, target_url)
                return Response(
                    content=rewritten, 
                    media_type=hls_type,
                    headers={
                        "Cache-Control": "no-cache", 
                        "Access-Control-Allow-Origin": "*",
                        "Content-Disposition": "inline"
                    }
                )
            finally:
                await resp.aclose()

        response_headers = {
            "Cache-Control": resp.headers.get("cache-control", "max-age=30"),
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Range, If-Range",
            "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges",
            "Accept-Ranges": "bytes",
        }

        # Force correct MIME type for HLS segments on iOS
        if path.endswith(".ts"):
            response_headers["Content-Type"] = "video/mp2t"
        else:
            response_headers["Content-Type"] = content_type

        if "content-range" in resp.headers:
            response_headers["Content-Range"] = resp.headers["content-range"]
        if "content-length" in resp.headers:
            response_headers["Content-Length"] = resp.headers["content-length"]

        return StreamingResponse(
            resp.aiter_bytes(),
            status_code=resp.status_code,
            headers=response_headers,
            background=BackgroundTask(resp.aclose)
        )

    except Exception as e:
        print(f"Proxy error for {target_url}: {e}")
        raise HTTPException(status_code=502, detail=f"Proxy error: {e}")


def _rewrite_manifest(manifest: str, session_id: str, playlist_url: str) -> str:
    # Use the playlist URL's directory as the base for relative paths
    playlist_base = playlist_url.rsplit("/", 1)[0] + "/"
    
    lines = []
    for line in manifest.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped == "":
            lines.append(line)
            continue

        # Preserve the entire path AND query string (for tokens)
        if stripped.startswith(("http://", "https://")):
            parsed = urlparse(stripped)
            # path including query
            rel = parsed.path.lstrip("/")
            if parsed.query:
                rel += "?" + parsed.query
        else:
            # It's a relative path on the Tablo, resolve against playlist_base
            full_url = urljoin(playlist_base, stripped)
            parsed = urlparse(full_url)
            rel = parsed.path.lstrip("/")
            if parsed.query:
                rel += "?" + parsed.query

        lines.append(f"/api/hls/{session_id}/{rel}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Status check
# ---------------------------------------------------------------------------

@router.get("/transcode/status/{session_id}")
async def transcode_status(session_id: str):
    if not state.is_authenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")

    sess = state.get_session(session_id)
    transcode_id = sess.transcode_id if sess else None
    tc = transcodes.get(transcode_id) if transcode_id else None

    log_content = ""
    if transcode_id:
        log_file = TRANSCODE_DIR / transcode_id / "ffmpeg.log"
        if log_file.exists():
            try:
                # Get last 20 lines of log
                lines = log_file.read_text().splitlines()
                log_content = "\n".join(lines[-20:])
            except Exception:
                pass

    if not tc:
        return {"status": "inactive", "log": log_content}

    session_dir = TRANSCODE_DIR / transcode_id
    return {
        "status": "active" if tc.proc.poll() is None else "stopped",
        "return_code": tc.proc.returncode,
        "files": [f.name for f in session_dir.glob("*") if f.is_file()],
        "log": log_content
    }


# ---------------------------------------------------------------------------
# Start FFmpeg
# ---------------------------------------------------------------------------

def _ffmpeg_cmd(input_url: str) -> list[str]:
    """Build the FFmpeg command for a live channel, preferring the iGPU."""

    # Without the reconnect flags a single dropped connection to the Tablo ends
    # the transcode for good instead of resuming where it left off.
    input_args = [
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", input_url,
    ]

    # Shorter segments than the default 6s get the first frame on screen sooner;
    # 12 of them still leaves a 48s window for a phone to fall behind in.
    output_args = [
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-f", "hls",
        "-hls_time", "4",
        "-hls_list_size", "12",
        "-hls_segment_filename", "%03d.ts",
        "-hls_flags", "delete_segments+independent_segments",
        "-loglevel", "info",
        "playlist.m3u8",
    ]

    if VAAPI_ENABLED:
        return [
            "ffmpeg", "-y",
            "-hwaccel", "vaapi",
            "-hwaccel_output_format", "vaapi",
            "-vaapi_device", str(VAAPI_DEVICE),
            *input_args,
            # auto=1 passes progressive channels (720p60) through untouched
            "-vf", "deinterlace_vaapi=auto=1",
            "-c:v", "h264_vaapi",
            "-b:v", "3000k", "-maxrate", "3500k", "-bufsize", "7000k",
            "-g", "60",
            *output_args,
        ]

    return [
        "ffmpeg", "-y",
        *input_args,
        # yadif deinterlaces 1080i OTA broadcast so browsers can render video;
        # deint=interlaced leaves progressive channels alone rather than
        # softening them for no reason.
        "-vf", "yadif=deint=interlaced",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "28",
        "-maxrate", "2000k", "-bufsize", "4000k",
        "-pix_fmt", "yuv420p", "-g", "60", "-sc_threshold", "0",
        *output_args,
    ]


def _evict_if_full() -> None:
    """Drop the least recently watched transcode to stay under the cap."""
    while len(transcodes) >= MAX_TRANSCODE_SESSIONS:
        stalest = min(transcodes, key=lambda tid: transcodes[tid].last_access)
        print(f"[transcode] at capacity, evicting least recently watched session {stalest}")
        kill_transcode(stalest)


async def start_transcoder(session_id: str, sess: StreamSession) -> None:
    """Start FFmpeg for this channel, or attach to the one already running."""
    running = transcodes.get(sess.transcode_id) if sess.transcode_id else None
    if running and running.proc.poll() is None:
        running.last_access = time.monotonic()
        return

    _evict_if_full()

    transcode_id = session_id
    session_dir = TRANSCODE_DIR / transcode_id
    session_dir.mkdir(exist_ok=True, parents=True)

    log_file = session_dir / "ffmpeg.log"
    cmd = _ffmpeg_cmd(sess.stream.playlist_url)

    with open(log_file, "w") as f:
        f.write(f"Starting FFmpeg for session {transcode_id}\n")
        f.write(f"Input: {sess.stream.playlist_url}\n")
        f.write(f"Command: {' '.join(cmd)}\n\n")
        f.flush()

        proc = subprocess.Popen(
            cmd,
            stdout=f,
            stderr=subprocess.STDOUT,
            cwd=session_dir
        )

    transcodes[transcode_id] = Transcode(proc, sess)
    sess.transcode_id = transcode_id
