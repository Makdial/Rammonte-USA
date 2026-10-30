"""Views for the administrator dashboard.

The dashboard groups the things an administrator needs in one place: the media
library, the users, the comments and how much room is left in the storage.
Deletes here reuse the existing ``videos`` endpoints so every removal is
reflected in the database and in Cloudflare R2.
"""
from __future__ import annotations

import datetime
import json
import logging

from django.utils import timezone

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.translation import gettext, gettext as _
from django.views.decorators.http import require_POST

from core.permissions import read_required, write_required
from videos import r2_storage
from videos.audio_tasks import (
    audio_sweep_status,
    eligible_audio_queryset,
    start_audio_sweep,
)
from videos.models import Comment, Video
from videos.transcription import (
    audio_sweep_status as subtitle_sweep_status,
    subtitle_progress,
    start_subtitle_sweep,
)

from .models import ActivityLog, StorageMonitor
from .notifications import send_test_email
from .storage import build_report, check_and_notify, format_bytes

logger = logging.getLogger(__name__)


def _audio_badge(video) -> tuple[str, str]:
    """Return ``(label, css_class)`` describing a video's MP3 state."""
    if video.has_audio:
        return _("Ready"), "badge-ok"
    if video.audio_status == "processing":
        return _("Generating"), "badge-warning"
    if video.audio_status == "failed":
        return _("Failed"), "badge-critical"
    if video.audio_status == "none":
        return _("No audio track"), "muted"
    return _("Not generated"), "muted"


def _caption_badge(video) -> tuple[str, str]:
    """Return ``(label, css_class)`` describing a video's captions.

    The languages are named, so the dashboard shows what a video actually has
    rather than only that something happened.
    """
    codes = sorted(
        code for code in video.subtitle_tracks.values_list("language_code", flat=True)
    )
    if codes:
        # Lyrics ride along with the source language, so list them last.
        named = [code for code in codes if code != "lyrics"]
        if "lyrics" in codes:
            named.append(_("lyrics"))
        return ", ".join(named), "badge-ok"
    if video.auto_subtitle_status == "processing":
        return _("Working"), "badge-warning"
    if video.auto_subtitle_status == "failed":
        return _("Failed"), "badge-critical"
    return _("None"), "muted"


def _to_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _gb_to_bytes(value, default: int) -> int:
    """Interpret a user supplied value as a number of gigabytes."""
    try:
        gigabytes = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if gigabytes <= 0:
        return default
    return int(gigabytes * 1024 ** 3)


@read_required
def home(request):
    # Measure *and* refresh the alert state whenever an administrator looks at
    # the dashboard, so freeing space re-arms the email notification.
    report = check_and_notify()
    monitor = StorageMonitor.load()

    stats = {
        "videos": Video.objects.count(),
        "users": User.objects.exclude(profile__is_guest=True).count(),
        "comments": Comment.objects.count(),
        "objects": report.object_count,
    }

    return render(
        request,
        "dashboard/home.html",
        {
            "report": report,
            "monitor": monitor,
            "stats": stats,
            "top_videos": report.videos[:8],
            "media_used_display": format_bytes(report.media_used),
            "media_quota_display": format_bytes(report.media_quota),
            "media_free_display": format_bytes(report.media_free),
            "db_size_display": format_bytes(report.db_size),
            "db_quota_display": format_bytes(report.db_quota),
            "db_free_display": format_bytes(report.db_free),
            "orphan_display": format_bytes(report.orphan_bytes),
            "quota_gb": round(report.media_quota / 1024 ** 3, 2),
            "db_quota_gb": round(report.db_quota / 1024 ** 3, 2),
            "active_tab": "home",
        },
    )


@read_required
def video_manage(request):
    report = build_report()
    query = (request.GET.get("q") or "").strip()

    videos = Video.objects.select_related("user").annotate(
        comment_total=Count("comments")
    )
    if query:
        videos = videos.filter(
            Q(title__icontains=query) | Q(user__username__icontains=query)
        )

    rows = []
    for video in videos:
        paths = [video.file_id, video.thumbnail_file_id, video.audio_file_id]
        total = sum(report.sizes.get(path, 0) for path in paths if path)
        audio_label, audio_class = _audio_badge(video)
        caption_label, caption_class = _caption_badge(video)
        rows.append(
            {
                "video": video,
                "bytes": total,
                "size_display": format_bytes(total),
                "comment_total": video.comment_total,
                "audio_label": audio_label,
                "audio_class": audio_class,
                "caption_label": caption_label,
                "caption_class": caption_class,
            }
        )

    return render(
        request,
        "dashboard/videos.html",
        {
            "report": report,
            "rows": rows,
            "query": query,
            "total_videos": len(rows),
            "missing_audio": eligible_audio_queryset().count(),
            "no_audio_count": Video.objects.filter(audio_status="none").count(),
            "active_tab": "videos",
        },
    )


@read_required
def video_order(request):
    """Let an administrator arrange the order of the home page.

    The rows are listed in exactly the order visitors see, so what is dragged
    here is what everybody gets.
    """
    videos = Video.objects.all()
    return render(
        request,
        "dashboard/order.html",
        {
            "videos": videos,
            "total_videos": videos.count(),
            "active_tab": "order",
        },
    )


@write_required
@require_POST
def video_order_save(request):
    """Store the order the administrator dropped the rows into."""
    try:
        payload = json.loads(request.body or "[]")
    except (TypeError, ValueError):
        payload = None

    if not isinstance(payload, list) or not payload:
        return JsonResponse(
            {"success": False, "error": _("Nothing to reorder.")}, status=400
        )

    # Only rows that really exist may be numbered, and each at most once, so a
    # hand-written request cannot renumber a video that was not on the page.
    by_id = {str(video.id): video for video in Video.objects.all()}
    ordered = []
    seen = set()
    for raw in payload:
        key = str(raw)
        if key in by_id and key not in seen:
            seen.add(key)
            ordered.append(by_id[key])

    if not ordered:
        return JsonResponse(
            {"success": False, "error": _("Nothing to reorder.")}, status=400
        )

    with transaction.atomic():
        for index, video in enumerate(ordered, start=1):
            if video.position != index:
                video.position = index
                video.save(update_fields=["position"])

    return JsonResponse(
        {"success": True, "count": len(ordered), "message": _("Order saved.")}
    )


@read_required
def comment_manage(request):
    query = (request.GET.get("q") or "").strip()

    comments = Comment.objects.select_related("user", "video").order_by("-created_at")
    if query:
        comments = comments.filter(
            Q(body__icontains=query)
            | Q(user__username__icontains=query)
            | Q(user__first_name__icontains=query)
        )

    return render(
        request,
        "dashboard/comments.html",
        {
            "comments": comments,
            "query": query,
            "total_comments": comments.count(),
            "active_tab": "comments",
        },
    )


@write_required
@require_POST
def cleanup(request):
    """Delete objects in the bucket that no video row references."""
    report = build_report()

    removed = 0
    freed = 0
    errors = 0
    for orphan in report.orphans:
        try:
            r2_storage.delete_file(orphan["key"])
            removed += 1
            freed += orphan["size"]
        except r2_storage.StorageError:
            errors += 1
            logger.warning("Could not delete orphaned object %r", orphan["key"], exc_info=True)

    # Re-measure so the page reflects the reclaimed space right away.
    build_report()

    return JsonResponse(
        {
            "success": True,
            "removed": removed,
            "errors": errors,
            "freed": freed,
            "freed_display": format_bytes(freed),
        }
    )


@write_required
@require_POST
def settings_update(request):
    monitor = StorageMonitor.load()

    quota_gb = request.POST.get("quota_gb")
    db_quota_gb = request.POST.get("db_quota_gb")
    threshold = _to_int(request.POST.get("alert_threshold_percent"), monitor.alert_threshold_percent)
    email = (request.POST.get("notify_email") or "").strip() or monitor.notify_email

    threshold = min(max(threshold, 1), 100)

    monitor.quota_bytes = _gb_to_bytes(quota_gb, monitor.quota_bytes)
    monitor.db_quota_bytes = _gb_to_bytes(db_quota_gb, monitor.db_quota_bytes)
    monitor.alert_threshold_percent = threshold
    monitor.notify_email = email
    monitor.save(
        update_fields=[
            "quota_bytes",
            "db_quota_bytes",
            "alert_threshold_percent",
            "notify_email",
        ]
    )

    # Re-evaluate the alert state against the (possibly new) threshold.
    report = check_and_notify()

    return JsonResponse(
        {
            "success": True,
            "message": _("Storage settings saved."),
            "alert_state": report.alert_state,
        }
    )


@write_required
@require_POST
def test_email(request):
    monitor = StorageMonitor.load()
    report = build_report(monitor)
    sent, error = send_test_email(monitor, report)

    if sent:
        return JsonResponse(
            {"success": True, "message": _("Test email sent to %(email)s.") % {"email": monitor.notify_email}}
        )
    return JsonResponse(
        {"success": False, "error": error or _("Could not send the email.")},
        status=400,
    )


@write_required
@require_POST
def audio_generate(request):
    """Start converting every video that is missing an MP3, in the background."""
    started = start_audio_sweep()
    return JsonResponse(
        {
            "success": True,
            "started": started,
            "message": _("Generating missing MP3s in the background.")
            if started
            else _("An MP3 generation run is already in progress."),
        }
    )


@write_required
@require_POST
def subtitle_generate(request):
    """Start generating the captions of every video, in the background."""
    started = start_subtitle_sweep()
    return JsonResponse(
        {
            "success": True,
            "started": started,
            "message": gettext("Generating subtitles in the background.")
            if started
            else gettext("A subtitle run is already in progress."),
        }
    )


@read_required
def subtitle_status(request):
    """Report the progress of the bulk caption run.

    The counters come from the database rather than the thread that runs the
    sweep: gunicorn serves from two workers, so half the requests land in a
    process that never started the run and would always answer "not running".
    """
    state = subtitle_sweep_status()
    counted = subtitle_progress()
    return JsonResponse(
        {
            # A run is in flight when a video is marked processing, whatever
            # worker answers this request.
            "running": bool(state["running"] or counted["processing"]),
            "total": counted["total"] or state["total"],
            "done": counted["done"],
            "succeeded": counted["succeeded"],
            "failed": counted["failed"],
            "processing": counted["processing"],
        }
    )


@read_required
def audio_status(request):
    """Report how many videos still need an MP3 and the progress of a run."""
    state = audio_sweep_status()
    return JsonResponse(
        {
            "missing": eligible_audio_queryset().count(),
            "processing": Video.objects.filter(audio_status="processing").count(),
            "no_audio": Video.objects.filter(audio_status="none").count(),
            "running": state["running"],
            "total": state["total"],
            "done": state["done"],
            "succeeded": state["succeeded"],
            "failed": state["failed"],
        }
    )


@read_required
def usage(request):
    """Daily usage: active users (guest vs registered) and top played videos."""
    today = timezone.localdate()
    date_str = (request.GET.get("date") or "").strip()
    try:
        selected = datetime.date.fromisoformat(date_str) if date_str else today
    except ValueError:
        selected = today

    day_start = timezone.make_aware(datetime.datetime.combine(selected, datetime.time.min))
    day_end = day_start + datetime.timedelta(days=1)
    day_logs = ActivityLog.objects.filter(created_at__gte=day_start, created_at__lt=day_end)

    user_ids = [uid for uid in day_logs.values_list("user_id", flat=True).distinct() if uid is not None]
    from accounts.models import Profile

    guest_user_ids = set(Profile.objects.filter(is_guest=True).values_list("user_id", flat=True))
    active_guests = sum(1 for uid in user_ids if uid in guest_user_ids)
    active_registered = sum(1 for uid in user_ids if uid not in guest_user_ids)

    top_views = list(
        day_logs.filter(kind=ActivityLog.KIND_VIEW)
        .values("video__id", "video__title")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )
    top_audio = list(
        day_logs.filter(kind=ActivityLog.KIND_AUDIO)
        .values("video__id", "video__title")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    series = []
    for i in range(13, -1, -1):
        d = today - datetime.timedelta(days=i)
        ds = timezone.make_aware(datetime.datetime.combine(d, datetime.time.min))
        de = ds + datetime.timedelta(days=1)
        logs = ActivityLog.objects.filter(created_at__gte=ds, created_at__lt=de)
        uids = set(uid for uid in logs.values_list("user_id", flat=True) if uid is not None)
        series.append(
            {
                "date": d,
                "users": len(uids),
                "views": logs.filter(kind=ActivityLog.KIND_VIEW).count(),
                "audio": logs.filter(kind=ActivityLog.KIND_AUDIO).count(),
            }
        )

    return render(
        request,
        "dashboard/usage.html",
        {
            "active_tab": "usage",
            "selected": selected,
            "today": today,
            "active_users": len(user_ids),
            "active_guests": active_guests,
            "active_registered": active_registered,
            "total_views": day_logs.filter(kind=ActivityLog.KIND_VIEW).count(),
            "total_audio": day_logs.filter(kind=ActivityLog.KIND_AUDIO).count(),
            "top_views": top_views,
            "top_audio": top_audio,
            "series": series,
        },
    )
