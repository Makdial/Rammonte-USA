"""Email alerts sent by the storage dashboard."""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

from .storage import format_bytes

logger = logging.getLogger(__name__)


def _recipients(monitor) -> list[str]:
    email = (getattr(monitor, "notify_email", "") or "").strip()
    return [email] if email else []


def send_storage_alert(monitor, report, state: str, *, test: bool = False) -> tuple[bool, str]:
    """Email the administrator about the storage level.

    Returns ``(sent, error)``; a failure is logged but never raised so it can
    never break an upload or a dashboard page.
    """
    recipients = _recipients(monitor)
    if not recipients:
        return False, "No notification email is configured."

    if test:
        subject = f"[Test] [Rammonte USA] Storage alert email ({report.max_percent:.0f}% used)"
    else:
        level = "full" if state == "critical" else "nearly full"
        subject = f"[Rammonte USA] Storage is {level} ({report.max_percent:.0f}% used)"

    lines = [
        "This is a test message." if test else "Rammonte USA storage needs attention.",
        "",
        (
            f"Media storage (Cloudflare R2): {format_bytes(report.media_used)} used "
            f"of {format_bytes(report.media_quota)} "
            f"({report.media_percent}%); {format_bytes(report.media_free)} free."
        ),
        (
            f"Database (SQLite): {format_bytes(report.db_size)} used "
            f"of {format_bytes(report.db_quota)} "
            f"({report.db_percent}%); {format_bytes(report.db_free)} free."
        ),
        f"Objects stored: {report.object_count}",
        f"Reclaimable orphaned files: {format_bytes(report.orphan_bytes)}",
        "",
        "Open the administrator dashboard to free space before the storage fills up.",
        f"Alert threshold: {monitor.alert_threshold_percent}%.",
    ]
    body = "\n".join(lines)

    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients)
    except Exception as exc:  # noqa: BLE001 - alerts must never crash the caller
        logger.warning("Could not send the storage alert email", exc_info=True)
        return False, str(exc)

    return True, ""


def send_test_email(monitor, report) -> tuple[bool, str]:
    """Send a sample alert so the administrator can check the configuration."""
    state = "critical" if report.max_percent >= 100 else "warning"
    return send_storage_alert(monitor, report, state, test=True)
