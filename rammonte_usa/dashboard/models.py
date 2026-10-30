"""Models for the administrator dashboard.

A single :class:`StorageMonitor` row keeps the storage limits, the latest
measurement and the alert state, so the "is the storage nearly full?" question
has one obvious place to look.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

# Defaults mirror the settings of the same name (STORAGE_QUOTA_BYTES, ...).
DEFAULT_QUOTA_BYTES = 10 * 1024 ** 3  # Cloudflare R2 free tier
DEFAULT_DB_QUOTA_BYTES = 1 * 1024 ** 3
DEFAULT_ALERT_THRESHOLD = 90
DEFAULT_ALERT_EMAIL = "makhoutech@gmail.com"


class StorageMonitor(models.Model):
    """Singleton row holding storage limits and the latest measurement."""

    class AlertState(models.TextChoices):
        OK = "ok", _("OK")
        WARNING = "warning", _("Nearly full")
        CRITICAL = "critical", _("Full")

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)

    quota_bytes = models.BigIntegerField(
        default=DEFAULT_QUOTA_BYTES,
        verbose_name=_("Media quota (bytes)"),
        help_text=_("How much room the Cloudflare R2 bucket has."),
    )
    db_quota_bytes = models.BigIntegerField(
        default=DEFAULT_DB_QUOTA_BYTES,
        verbose_name=_("Database quota (bytes)"),
        help_text=_("Soft limit for the SQLite database file."),
    )
    alert_threshold_percent = models.PositiveSmallIntegerField(
        default=DEFAULT_ALERT_THRESHOLD,
        verbose_name=_("Alert threshold (%)"),
        help_text=_("Send an email once this percentage of the space is used."),
    )
    notify_email = models.EmailField(
        default=DEFAULT_ALERT_EMAIL,
        verbose_name=_("Notification email"),
    )

    used_bytes = models.BigIntegerField(default=0, editable=False)
    object_count = models.PositiveIntegerField(default=0, editable=False)
    db_size_bytes = models.BigIntegerField(default=0, editable=False)
    measured_at = models.DateTimeField(null=True, blank=True, editable=False)

    alert_state = models.CharField(
        max_length=10,
        choices=AlertState.choices,
        default=AlertState.OK,
        editable=False,
    )
    last_notified_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("Storage monitor")
        verbose_name_plural = _("Storage monitor")

    def __str__(self):
        return "Storage monitor"

    @classmethod
    def load(cls) -> "StorageMonitor":
        """Return the singleton row, creating it from the settings on first use."""
        from django.conf import settings

        defaults = {
            "quota_bytes": getattr(settings, "STORAGE_QUOTA_BYTES", DEFAULT_QUOTA_BYTES),
            "db_quota_bytes": getattr(
                settings, "DATABASE_QUOTA_BYTES", DEFAULT_DB_QUOTA_BYTES
            ),
            "alert_threshold_percent": getattr(
                settings, "STORAGE_ALERT_THRESHOLD_PERCENT", DEFAULT_ALERT_THRESHOLD
            ),
            "notify_email": getattr(
                settings, "STORAGE_ALERT_EMAIL", DEFAULT_ALERT_EMAIL
            ),
        }
        monitor, _created = cls.objects.get_or_create(pk=1, defaults=defaults)
        return monitor


class ActivityLog(models.Model):
    """A single usage event: a page view or an audio play.

    Rows are append-only and aggregated in the dashboard's usage view, so the
    administrator can see how many (guest or registered) users touched the app
    on a given day and which recordings they played.
    """

    KIND_VIEW = "view"
    KIND_AUDIO = "audio"
    KIND_CHOICES = [
        (KIND_VIEW, _("View")),
        (KIND_AUDIO, _("Audio")),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activity_logs",
    )
    video = models.ForeignKey(
        "videos.Video",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activity_logs",
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at", "kind"]),
        ]

    def __str__(self):
        return f"{self.kind} {self.video_id} by {self.user_id} at {self.created_at}"
