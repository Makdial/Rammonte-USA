"""Automatic MP3 generation for videos.

Uploads already extract the audio inline, but videos can still end up without
an MP3 (older uploads, an extraction that failed, videos imported from another
storage provider, ...). This module can turn any video's audio into an R2-hosted
MP3 on demand — either in the background when a video is played, or in bulk via
the ``generateaudio`` management command.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .audio import AudioExtractionError, extract_mp3_from_url
from .models import Video
from .r2_storage import StorageError, upload_audio

logger = logging.getLogger(__name__)


def stale_minutes() -> int:
    """How long a job may stay "processing" before it counts as abandoned."""
    return int(getattr(settings, "JOB_STALE_MINUTES", 30))


def clear_stale_jobs() -> int:
    """Release job flags left behind by a worker that was killed mid-job.

    Free PaaS hosts (Render in particular) spin the web process down while a
    background ``ffmpeg`` or transcription is still running. Without this the
    video would stay "processing" forever and never be retried.

    Each status is cleared only if that particular job was the stuck one, so a
    finished subtitle track is not marked failed by an unrelated audio job.
    """
    cutoff = timezone.now() - timedelta(minutes=stale_minutes())
    stale = {"job_started_at__lt": cutoff}

    released = Video.objects.filter(**stale, audio_status="processing").update(
        audio_status="failed",
        audio_error="The conversion was interrupted. Please try again.",
        job_started_at=None,
    )
    released += Video.objects.filter(
        **stale, auto_subtitle_status="processing"
    ).exclude(job_started_at__isnull=True).update(
        auto_subtitle_status="failed",
        auto_subtitle_error="The conversion was interrupted. Please try again.",
        job_started_at=None,
    )
    if released:
        logger.info("Released %s abandoned background job(s)", released)
    return released


def _remove(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def generate_audio(video_id: int) -> bool:
    """Create the MP3 for ``video_id`` and store it in R2.

    The caller is expected to have set ``audio_status`` to ``"processing"``
    first (which also acts as a lock, see :func:`schedule_audio_generation`).
    Returns ``True`` once the video has an MP3.
    """
    video = Video.objects.filter(id=video_id).first()
    if video is None:
        return False

    if video.has_audio:
        Video.objects.filter(id=video_id).update(
            audio_status="ready", audio_error="", job_started_at=None
        )
        return True

    source = video.playback_url
    if not source:
        Video.objects.filter(id=video_id).update(
            audio_status="failed",
            audio_error="No playable video URL is available.",
            job_started_at=None,
        )
        return False

    mp3_path = ""
    try:
        handle_fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
        os.close(handle_fd)

        if not extract_mp3_from_url(source, mp3_path):
            # The video genuinely has no audio stream; remember that so we do
            # not try again on every page view.
            Video.objects.filter(id=video_id).update(
                audio_status="none", audio_error="", job_started_at=None
            )
            return False

        file_name = (Path(video.file_id).stem or "audio") + ".mp3"
        with open(mp3_path, "rb") as mp3_file:
            result = upload_audio(mp3_file, file_name)

        Video.objects.filter(id=video_id).update(
            audio_file_id=result["path"],
            audio_url=result["url"],
            audio_status="ready",
            audio_error="",
            job_started_at=None,
        )
        return True
    except (AudioExtractionError, StorageError, OSError) as exc:
        logger.warning("MP3 generation failed for video %s", video_id, exc_info=True)
        Video.objects.filter(id=video_id).update(
            audio_status="failed",
            audio_error=str(exc)[:500],
            job_started_at=None,
        )
        return False
    finally:
        _remove(mp3_path)


def _worker(video_id: int) -> None:
    # A background thread gets its own database connection; tidy it up so SQLite
    # does not keep a stale handle (or, in tests, leak a connection).
    close_old_connections()
    try:
        generate_audio(video_id)
    finally:
        close_old_connections()


def schedule_audio_generation(video_id: int, retry: bool = False) -> bool:
    """Start generating a video's MP3 in a background thread.

    ``audio_status`` is used as a lock, so a given video is only processed by
    one worker at a time. With ``retry=False`` (the default) only untouched
    videos are queued; ``retry=True`` also allows previously failed ones.
    """
    queryset = Video.objects.filter(id=video_id, audio_file_id="")
    if retry:
        queryset = queryset.exclude(audio_status__in=["processing", "none"])
    else:
        queryset = queryset.filter(audio_status="")

    if not queryset.update(audio_status="processing", job_started_at=timezone.now()):
        return False

    threading.Thread(target=_worker, args=(video_id,), daemon=True).start()
    return True


def eligible_audio_queryset():
    """Videos that still need an MP3 and could plausibly have one.

    Excludes videos already known to have no audio stream (``none``) so a bulk
    run does not waste time re-checking them; failures (``failed``) and
    untouched videos (``""``) are included.
    """
    return Video.objects.filter(audio_file_id="").exclude(audio_status="none")


# Progress of the dashboard's "Generate missing MP3s" sweep. Shared across
# threads in the process; the database column is still the real source of truth.
_SWEEP_STATE = {
    "running": False,
    "total": 0,
    "done": 0,
    "succeeded": 0,
    "failed": 0,
}
_SWEEP_LOCK = threading.Lock()


def audio_sweep_status() -> dict:
    """Return a snapshot of the bulk-conversion progress."""
    with _SWEEP_LOCK:
        return dict(_SWEEP_STATE)


def _sweep_videos() -> None:
    """Convert every eligible video, one at a time, updating the progress.

    Kept separate from :func:`_run_sweep` so it can be tested without the
    thread/connection juggling (and without closing the test transaction).
    """
    video_ids = list(
        eligible_audio_queryset().order_by("id").values_list("id", flat=True)
    )
    with _SWEEP_LOCK:
        _SWEEP_STATE.update(
            running=True,
            total=len(video_ids),
            done=0,
            succeeded=0,
            failed=0,
        )

    for video_id in video_ids:
        # Same lock as the on-play path: skip a video that was handled (or
        # removed) while the sweep was queued.
        if not Video.objects.filter(id=video_id, audio_file_id="").update(
            audio_status="processing"
        ):
            continue
        succeeded = generate_audio(video_id)
        with _SWEEP_LOCK:
            _SWEEP_STATE["done"] += 1
            _SWEEP_STATE["succeeded" if succeeded else "failed"] += 1


def _run_sweep() -> None:
    # A background thread gets its own database connection; tidy it up.
    close_old_connections()
    try:
        _sweep_videos()
    finally:
        with _SWEEP_LOCK:
            _SWEEP_STATE["running"] = False
        close_old_connections()


def start_audio_sweep() -> bool:
    """Kick off a background sweep over every video that is missing an MP3.

    Returns ``False`` when a sweep is already running.
    """
    with _SWEEP_LOCK:
        if _SWEEP_STATE["running"]:
            return False
        # Reserve the slot before the thread starts so two quick clicks cannot
        # launch two sweeps.
        _SWEEP_STATE["running"] = True

    threading.Thread(target=_run_sweep, daemon=True).start()
    return True
