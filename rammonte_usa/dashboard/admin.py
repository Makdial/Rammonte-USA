from django.contrib import admin

from .models import StorageMonitor


@admin.register(StorageMonitor)
class StorageMonitorAdmin(admin.ModelAdmin):
    list_display = (
        "quota_bytes",
        "used_bytes",
        "alert_threshold_percent",
        "notify_email",
        "alert_state",
        "measured_at",
    )
    readonly_fields = (
        "used_bytes",
        "object_count",
        "db_size_bytes",
        "measured_at",
        "alert_state",
        "last_notified_at",
    )

    def has_add_permission(self, request):
        # The monitor is a singleton created automatically on first use.
        return False

    def has_delete_permission(self, request, obj=None):
        return False
