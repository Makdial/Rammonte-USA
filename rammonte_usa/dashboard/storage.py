"""Storage accounting for the administrator dashboard.

This module answers three questions:

* how much room the media bucket (Cloudflare R2) is using;
* how large the SQLite database file is;
* which files could be deleted to reclaim space (largest videos, orphaned
  objects left behind by interrupted uploads).

It also drives the "nearly full" email alert: :func:`check_and_notify` measures
the storage and emails the administrator once each time the usage crosses the
configured threshold (and again if it becomes completely full).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

from django.db import connection
from django.utils import timezone

from videos import r2_storage
from videos.models import Video

from .models import StorageMonitor

logger = logging.getLogger(__name__)

_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]

# Severity of each alert state, used to avoid re-notifying when the situation
# is improving (for example critical -> warning just means space was freed).
_SEVERITY = {"ok": 0, "warning": 1, "critical": 2}


def format_bytes(value) -> str:
    """Return a human friendly size such as ``1.5 GB``."""
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        size = 0.0

    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def percent(used, quota) -> float:
    """Return ``used / quota`` as a percentage (0 when the quota is unknown)."""
    try:
        quota = float(quota or 0)
        used = float(used or 0)
    except (TypeError, ValueError):
        return 0.0
    if quota <= 0:
        return 0.0
    return round(used / quota * 100, 1)


def database_size() -> int:
    """Size of the SQLite database file including its WAL/SHM sidecars."""
    if connection.vendor != "sqlite":
        return 0

    name = connection.settings_dict.get("NAME")
    if not name or str(name) == ":memory:":
        return 0

    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(f"{name}{suffix}")
        except OSError:
            pass
    return total


def measure_bucket() -> dict:
    """List the bucket and return its totals and per-object sizes.

    Never raises: when R2 is not configured (or unreachable) the result reports
    ``configured``/``error`` so the dashboard can explain the problem instead of
    crashing.
    """
    empty = {"configured": False, "error": "", "total": 0, "count": 0, "sizes": {}, "by_prefix": {}}

    if not r2_storage.is_configured():
        return empty

    total = 0
    count = 0
    sizes: dict[str, int] = {}
    by_prefix: dict[str, int] = {}
    try:
        for obj in r2_storage.list_objects():
            key = obj["key"]
            size = obj["size"]
            total += size
            count += 1
            sizes[key] = size
            prefix = key.split("/", 1)[0] + "/"
            by_prefix[prefix] = by_prefix.get(prefix, 0) + size
    except r2_storage.StorageError as exc:
        logger.warning("Could not measure the R2 bucket: %s", exc)
        return {**empty, "configured": True, "error": str(exc)}

    return {
        "configured": True,
        "error": "",
        "total": total,
        "count": count,
        "sizes": sizes,
        "by_prefix": by_prefix,
    }


def video_size_rows(sizes: dict[str, int]) -> list[dict]:
    """Return every video paired with the total size of its R2 objects."""
    rows = []
    for video in Video.objects.select_related("user").all():
        paths = [video.file_id, video.thumbnail_file_id, video.audio_file_id]
        total = sum(sizes.get(path, 0) for path in paths if path)
        rows.append({"video": video, "bytes": total})
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    return rows


def orphaned_objects(sizes: dict[str, int]) -> list[dict]:
    """Objects created by this app that no video row points at any more."""
    referenced = set()
    for video in Video.objects.all():
        for path in (video.file_id, video.thumbnail_file_id, video.audio_file_id):
            if path:
                referenced.add(path)

    orphans = [
        {"key": key, "size": size}
        for key, size in sizes.items()
        if key not in referenced and r2_storage.is_object_path(key)
    ]
    orphans.sort(key=lambda item: item["size"], reverse=True)
    return orphans


@dataclass
class StorageReport:
    configured: bool
    error: str
    media_used: int
    media_quota: int
    media_free: int
    media_percent: float
    media_bar: float
    db_size: int
    db_quota: int
    db_free: int
    db_percent: float
    db_bar: float
    max_percent: float
    object_count: int
    by_prefix: dict = field(default_factory=dict)
    sizes: dict = field(default_factory=dict)
    videos: list = field(default_factory=list)
    orphans: list = field(default_factory=list)
    orphan_bytes: int = 0
    measured_at: datetime | None = None
    alert_state: str = StorageMonitor.AlertState.OK
    threshold: int = 90
    notify_email: str = ""
    last_notified_at: datetime | None = None


def build_report(monitor: StorageMonitor | None = None, *, persist: bool = True) -> StorageReport:
    """Measure storage and return a :class:`StorageReport`.

    When ``persist`` is true the latest numbers are written back to the monitor
    row so the dashboard can show when it was last measured.
    """
    monitor = monitor or StorageMonitor.load()
    bucket = measure_bucket()
    db_size = database_size()

    media_used = bucket["total"]
    media_quota = monitor.quota_bytes
    db_quota = monitor.db_quota_bytes

    media_percent = percent(media_used, media_quota)
    db_percent = percent(db_size, db_quota)

    if persist:
        monitor.used_bytes = media_used
        monitor.object_count = bucket["count"]
        monitor.db_size_bytes = db_size
        monitor.measured_at = timezone.now()
        monitor.save(
            update_fields=[
                "used_bytes",
                "object_count",
                "db_size_bytes",
                "measured_at",
            ]
        )

    orphans = orphaned_objects(bucket["sizes"])

    return StorageReport(
        configured=bucket["configured"],
        error=bucket["error"],
        media_used=media_used,
        media_quota=media_quota,
        media_free=max(media_quota - media_used, 0),
        media_percent=media_percent,
        media_bar=min(media_percent, 100),
        db_size=db_size,
        db_quota=db_quota,
        db_free=max(db_quota - db_size, 0),
        db_percent=db_percent,
        db_bar=min(db_percent, 100),
        max_percent=max(media_percent, db_percent),
        object_count=bucket["count"],
        by_prefix=bucket["by_prefix"],
        sizes=bucket["sizes"],
        videos=video_size_rows(bucket["sizes"]),
        orphans=orphans,
        orphan_bytes=sum(item["size"] for item in orphans),
        measured_at=monitor.measured_at,
        alert_state=monitor.alert_state,
        threshold=monitor.alert_threshold_percent,
        notify_email=monitor.notify_email,
        last_notified_at=monitor.last_notified_at,
    )


def desired_state(max_percent: float, threshold: int) -> str:
    """Map a usage percentage to an alert state."""
    if max_percent >= 100:
        return StorageMonitor.AlertState.CRITICAL
    if max_percent >= threshold:
        return StorageMonitor.AlertState.WARNING
    return StorageMonitor.AlertState.OK


def check_and_notify(*, persist: bool = True) -> StorageReport:
    """Measure storage and email the administrator when it gets dangerously full.

    The email is sent only when the alert state becomes *worse* (ok -> warning
    -> critical), so it is not repeated on every check. When space is freed the
    state falls back to ``ok`` and the alert is re-armed.
    """
    from .notifications import send_storage_alert

    monitor = StorageMonitor.load()
    report = build_report(monitor, persist=persist)

    new_state = desired_state(report.max_percent, monitor.alert_threshold_percent)
    previous = monitor.alert_state

    if new_state != previous:
        monitor.alert_state = new_state
        monitor.save(update_fields=["alert_state"])
        report.alert_state = new_state

        if _SEVERITY.get(new_state, 0) > _SEVERITY.get(previous, 0):
            sent, _error = send_storage_alert(monitor, report, new_state)
            if sent:
                monitor.last_notified_at = timezone.now()
                monitor.save(update_fields=["last_notified_at"])
                report.last_notified_at = monitor.last_notified_at

    return report
