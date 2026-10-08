"""
services/optimizer.py — Turns an uploaded video into a much smaller one, in the background.

Flow (VIDEO_OPTIMIZE_ENABLED, needs ffmpeg + ffprobe):
  1. The upload is received into the *staging* folder on this server's disk, never into the
     media storage (R2). When it is complete the video is a draft with
     `optimization: {state: "queued"}` and `file.staged: true`.
  2. A worker takes queued videos one at a time (VIDEO_OPTIMIZE_CONCURRENCY), re-encodes them
     with ffmpeg — same resolution and frame rate, H.264, quality VIDEO_OPTIMIZE_CRF, AAC audio,
     fast-start — and checks the result (same length, same size in pixels, audio kept, smaller).
  3. Only the finished file is stored in the media storage; the original is then deleted from
     the staging folder. The video record keeps its id, title, thumbnail, document, order, ….
  4. If the upload was already efficient (H.264 at or below VIDEO_OPTIMIZE_SKIP_BELOW_KBPS) or the
     encode isn't clearly smaller, the upload itself is stored instead ("kept"): re-encoding it
     would only lose quality. This is the one case in which an uploaded file reaches the storage.
  5. If anything fails, the upload stays in the staging folder, the video shows "failed" and the
     admin can retry. Nothing is ever sent to the storage as a fallback.
  6. A video waiting to be published (publish_when_ready) goes live now; the file of a
     replacement is swapped into its video.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings
from app.providers.storage import StorageError, get_staging_storage, get_storage
from app.repositories import video_repo

logger = logging.getLogger("nutriblend.optimizer")


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def active() -> bool:
    """New uploads are staged and optimized."""
    return settings.VIDEO_OPTIMIZE_ENABLED and available()


class OptimizeError(Exception):
    """A problem with this video (shown to the admin)."""


# ── Looking at a file ────────────────────────────────────────────────────────

async def probe(path: Path) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        raise OptimizeError("The video could not be read (timed out).")
    if proc.returncode != 0:
        raise OptimizeError("The video could not be read: " + (err.decode(errors="replace").strip()[-200:] or "unknown error"))
    info = json.loads(out or b"{}")
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        raise OptimizeError("The file has no video track.")
    fmt = info.get("format", {})
    try:
        duration = float(fmt.get("duration") or video.get("duration"))
    except (TypeError, ValueError):
        raise OptimizeError("The video's length could not be read.")
    total_bps = int(fmt.get("bit_rate") or 0)
    audio_bps = int(audio.get("bit_rate") or 0) if audio else 0
    video_bps = int(video.get("bit_rate") or 0) or max(total_bps - audio_bps, 0)
    return {
        "codec": video.get("codec_name"), "pix_fmt": video.get("pix_fmt"),
        "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
        "duration": duration, "video_bps": video_bps, "has_audio": audio is not None,
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# ── Encoding ─────────────────────────────────────────────────────────────────

def ffmpeg_args(src: Path, dst: Path) -> list[str]:
    return [
        shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-c:v", "libx264", "-preset", settings.VIDEO_OPTIMIZE_PRESET, "-crf", str(settings.VIDEO_OPTIMIZE_CRF),
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-threads", str(settings.VIDEO_OPTIMIZE_THREADS),
        "-c:a", "aac", "-b:a", f"{settings.VIDEO_OPTIMIZE_AUDIO_KBPS}k", "-ac", "2",
        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(dst),
    ]


async def _encode(src: Path, dst: Path, duration: float, on_progress) -> None:
    def _lower_priority() -> None:
        try:
            os.nice(10)
        except (OSError, AttributeError):
            pass

    proc = await asyncio.create_subprocess_exec(
        *ffmpeg_args(src, dst), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        preexec_fn=_lower_priority if os.name == "posix" else None,
    )
    tail: list[bytes] = []

    async def _drain_stderr() -> None:
        assert proc.stderr
        async for line in proc.stderr:
            tail.append(line)
            del tail[:-20]

    async def _read_progress() -> None:
        assert proc.stdout
        last = 0.0
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                fraction = min(max(seconds / duration, 0.0), 0.99) if duration > 0 else 0.0
                if fraction - last >= 0.02:
                    last = fraction
                    await on_progress(fraction)

    limit = settings.VIDEO_OPTIMIZE_TIMEOUT_FACTOR * duration + 600
    try:
        await asyncio.wait_for(asyncio.gather(_drain_stderr(), _read_progress(), proc.wait()), timeout=limit)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise OptimizeError(f"Optimizing took longer than {int(limit // 60)} minutes and was stopped.")
    except asyncio.CancelledError:
        proc.kill()
        raise
    if proc.returncode != 0:
        message = b"".join(tail).decode(errors="replace").strip()[-300:]
        raise OptimizeError("The video could not be converted: " + (message or f"ffmpeg exited with {proc.returncode}"))


def _check_output(src: dict, out: dict) -> None:
    """The encoded file must be the same video: same length and picture size, sound kept."""
    if abs(out["duration"] - src["duration"]) > max(1.0, src["duration"] * 0.01):
        raise OptimizeError(
            f"The converted video has a different length ({out['duration']:.1f}s instead of {src['duration']:.1f}s)."
        )
    if (out["width"], out["height"]) != (src["width"], src["height"]):
        raise OptimizeError("The converted video has a different picture size.")
    if src["has_audio"] and not out["has_audio"]:
        raise OptimizeError("The converted video lost its sound.")
    if out["codec"] != "h264":
        raise OptimizeError("The converted video is not H.264.")


# ── One job ──────────────────────────────────────────────────────────────────

async def _set(video_id, fields: dict[str, Any]) -> None:
    await video_repo.update(video_id, {f"optimization.{k}": v for k, v in fields.items()})


async def _replacement_state(doc: dict, state: Optional[str]) -> None:
    from app.services import video_service

    await video_service.set_replacement_state(doc, state)


async def run_job(doc: dict) -> None:
    """Optimize one claimed (running) video. Never raises: failures are recorded on the video."""
    video_id = doc["_id"]
    try:
        await _run_job(doc)
    except asyncio.CancelledError:
        await video_repo.requeue_running_optimizations()   # the server is stopping: it starts again next time
        raise
    except Exception as exc:  # noqa: BLE001 — recorded, shown to the admin, retry possible
        message = str(exc) if isinstance(exc, (OptimizeError, StorageError)) else f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, OptimizeError):
            logger.exception("Optimizing video %s failed", video_id)
        else:
            logger.warning("Optimizing video %s failed: %s", video_id, message)
        if await video_repo.find_by_id(str(video_id)):
            await _set(video_id, {"state": "failed", "error": message[:400], "finished_at": datetime.now(timezone.utc)})
            await _replacement_state(doc, "failed")


async def _run_job(doc: dict) -> None:
    from app.services import video_service

    video_id = doc["_id"]
    staging = get_staging_storage()
    key = doc["file"]["key"]
    src_path = staging.local_path(key)
    if src_path is None or not src_path.is_file():
        raise OptimizeError("The uploaded file is missing from the server's staging folder. Upload the video again.")
    src_size = src_path.stat().st_size
    await _replacement_state(doc, "running")

    info = await probe(src_path)
    needs_h264 = info["codec"] != "h264" or info["pix_fmt"] not in ("yuv420p", "yuvj420p")
    already_small = (not needs_h264) and 0 < info["video_bps"] <= settings.VIDEO_OPTIMIZE_SKIP_BELOW_KBPS * 1000

    out_path = staging.staging_dir() / f"optimized-{video_id}.mp4"
    try:
        mode, reason, final_path, out_info = "kept", "Already efficient — stored as it is.", src_path, None
        if not already_small:
            async def progress(fraction: float) -> None:
                await _set(video_id, {"progress": round(fraction, 3)})

            await _encode(src_path, out_path, info["duration"], progress)
            out_info = await probe(out_path)
            _check_output(info, out_info)
            out_size = out_path.stat().st_size
            saving = 100 * (1 - out_size / src_size)
            if needs_h264:
                mode, reason, final_path = "converted", "Converted to H.264 so it plays on every phone.", out_path
            elif saving >= settings.VIDEO_OPTIMIZE_MIN_SAVING_PERCENT:
                mode, reason, final_path = "compressed", None, out_path
            else:
                mode, reason, out_info = "kept", "The smaller version was not clearly smaller — stored as it is.", None

        if not await video_repo.find_by_id(str(video_id)):
            return  # deleted while it was being optimized

        final_info = out_info or info
        if final_path == src_path:
            # "kept": store a link to the upload, so the original stays in staging until everything succeeded
            final_path = staging.staging_dir() / f"keep-{video_id}.mp4"
            try:
                os.link(src_path, final_path)
            except OSError:
                await asyncio.to_thread(shutil.copyfile, src_path, final_path)
        sha = await asyncio.to_thread(_sha256_file, final_path)
        final_size = final_path.stat().st_size
        new_key = f"videos/{video_id}/{uuid.uuid4().hex}.mp4"
        storage = get_storage()
        await storage.put_file(new_key, final_path, "video/mp4")      # the file is consumed
        stored = await storage.object_size(new_key)
        if stored != final_size:
            await storage.delete(new_key)
            raise OptimizeError("The finished video did not arrive in storage intact. It will be tried again on retry.")

        now = datetime.now(timezone.utc)
        file = dict(doc["file"])
        file.pop("staged", None)
        file.update(
            key=new_key, size=final_size, sha256=sha, content_type="video/mp4",
            duration_seconds=round(final_info["duration"], 2), video_codec=final_info["codec"],
            integrity_verified=True,
        )
        if doc["file"].get("sha256") and doc["file"]["sha256"] != sha:
            file["source_sha256"] = doc["file"]["sha256"]          # the upload's fingerprint, for duplicate checks
        file["source_size"] = src_size
        optimization = {
            **(doc.get("optimization") or {}),
            "state": "done", "progress": 1.0, "source_size": src_size, "output_size": final_size,
            "saved_percent": max(round(100 * (1 - final_size / src_size)), 0), "mode": mode, "reason": reason,
            "finished_at": now,
        }
        optimization.pop("error", None)
        updated = await video_repo.update(video_id, {"file": file, "optimization": optimization, "updated_at": now})
        if updated is None:
            await storage.delete(new_key)          # deleted just now
            return
    finally:
        out_path.unlink(missing_ok=True)
        (staging.staging_dir() / f"keep-{video_id}.mp4").unlink(missing_ok=True)

    try:
        await staging.delete(key)
    except StorageError as exc:  # noqa: BLE001 — only wasted space
        logger.warning("Could not delete the staged original of video %s: %s", video_id, exc)
    logger.info("Video %s optimized: %s %d -> %d bytes", video_id, mode, src_size, final_size)

    if updated.get("replaces"):
        await video_service.swap_replacement(updated, "")
    else:
        await video_repo.publish_if_waiting(video_id)


# ── The queue ────────────────────────────────────────────────────────────────

async def run_once() -> bool:
    """Take the oldest queued video and optimize it. False when the queue is empty."""
    doc = await video_repo.claim_optimization()
    if not doc:
        return False
    await run_job(doc)
    return True


async def worker_loop() -> None:
    n = await video_repo.requeue_running_optimizations()
    if n:
        logger.info("Optimization: %d interrupted video(s) queued again", n)
    logger.info(
        "Video optimization on: crf %d, preset %s, %d job(s) at a time, staging %s",
        settings.VIDEO_OPTIMIZE_CRF, settings.VIDEO_OPTIMIZE_PRESET, settings.VIDEO_OPTIMIZE_CONCURRENCY,
        getattr(get_staging_storage(), "root", ""),
    )

    async def lane() -> None:
        while True:
            try:
                busy = await run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("Optimization worker error")
                busy = False
            if not busy:
                await asyncio.sleep(settings.VIDEO_OPTIMIZE_POLL_SECONDS)

    await asyncio.gather(*(lane() for _ in range(max(settings.VIDEO_OPTIMIZE_CONCURRENCY, 1))))
