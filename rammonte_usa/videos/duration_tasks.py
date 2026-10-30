"""Fill in the length of videos that were uploaded before it was recorded.

New uploads measure their own duration, and the ``generatedurations`` command
does the whole library in one go. This module is the hands-free middle ground:
whenever a page shows a video that still has no length, its measurement is
queued on a background thread and saved, so old videos fill themselves in the
first time somebody looks at them.

Only videos whose media lives in the configured object storage (R2) are
touched, so a development machine or a test run without storage never reaches
out to the network.
"""
from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.db import close_old_connections

from .audio import get_duration_from_url
from .models import Video
from .r2_storage import is_configured

logger = logging.getLogger(__name__)

_QUEUE_LOCK = threading.Lock()
_QUEUE: list[int] = []
_QUEUED: set[int] = set()
# Videos we already tried and could not measure (missing media, unreadable
# file). Kept in memory so a broken video is not re-probed on every page view;
# a process restart clears it and gives them another chance.
_FAILED: set[int] = set()
_WORKER_RUNNING = False


def fill_duration(video_id: int) -> float:
    """Measure one video's length and store it. Returns the seconds (0 = none).

    Safe to call directly (the management command does its own thing); does
    nothing when the video already has a length or has no playable media.
    """
    video = Video.objects.filter(id=video_id).first()
    if video is None or video.duration_seconds:
        return video.duration_seconds if video else 0.0

    url = video.playback_url if video.has_video else video.audio_playback_url
    if not url:
        _FAILED.add(video_id)
        return 0.0

    duration = get_duration_from_url(url)
    if duration <= 0:
        _FAILED.add(video_id)
        return 0.0

    # ``duration_seconds=0`` in the filter keeps another worker's result from
    # being overwritten (and skips the write entirely if it got there first).
    Video.objects.filter(id=video_id, duration_seconds=0).update(
        duration_seconds=duration
    )
    return duration


def _worker() -> None:
    global _WORKER_RUNNING
    # A background thread gets its own database connection; tidy it up so
    # SQLite does not keep a stale handle (or leak one in tests).
    close_old_connections()
    try:
        while True:
            with _QUEUE_LOCK:
                if not _QUEUE:
                    _WORKER_RUNNING = False
                    return
                video_id = _QUEUE.pop(0)
                _QUEUED.discard(video_id)
            try:
                fill_duration(video_id)
            except Exception:  # noqa: BLE001 - a background thread must not die
                logger.warning("Could not measure video %s", video_id, exc_info=True)
                _FAILED.add(video_id)
    finally:
        close_old_connections()


def schedule_duration_fill(video_ids) -> int:
    """Queue the given video ids for a background length measurement.

    Returns how many were newly queued. Ids already handled, in flight, or
    known to be unmeasurable are ignored, so callers can pass every video on a
    page without worrying about duplicates.
    """
    global _WORKER_RUNNING

    # Never reach out to the network during the test suite.
    if getattr(settings, "TESTING", False):
        return 0
    if not is_configured():
        return 0

    queued = 0
    with _QUEUE_LOCK:
        for video_id in video_ids:
            if video_id in _QUEUED or video_id in _FAILED:
                continue
            _QUEUED.add(video_id)
            _QUEUE.append(video_id)
            queued += 1

        if queued and not _WORKER_RUNNING:
            _WORKER_RUNNING = True
            threading.Thread(target=_worker, daemon=True).start()

    return queued


def schedule_missing_durations(queryset) -> int:
    """Queue every video in ``queryset`` that has no length recorded yet."""
    ids = list(
        queryset.filter(duration_seconds=0).values_list("id", flat=True)
    )
    if not ids:
        return 0
    return schedule_duration_fill(ids)
