"""Measure storage and email the administrator when it is nearly full.

Schedule this command (cron, Task Scheduler, ...) to get a warning before the
bucket runs out of room, for example every hour::

    python manage.py checkstorage
"""
from django.core.management.base import BaseCommand

from dashboard.models import StorageMonitor
from dashboard.notifications import send_test_email
from dashboard.storage import build_report, check_and_notify, format_bytes


class Command(BaseCommand):
    help = "Check storage usage and email the administrator when it is nearly full."

    def add_arguments(self, parser):
        parser.add_argument(
            "--test-email",
            action="store_true",
            help="Send a sample alert email instead of the regular check.",
        )

    def handle(self, *args, **options):
        if options["test_email"]:
            monitor = StorageMonitor.load()
            report = build_report(monitor)
            sent, error = send_test_email(monitor, report)
            if sent:
                self.stdout.write(
                    self.style.SUCCESS(f"Test email sent to {monitor.notify_email}.")
                )
            else:
                self.stderr.write(
                    self.style.ERROR(f"Could not send the test email: {error}")
                )
            return

        report = check_and_notify()

        media = (
            f"{format_bytes(report.media_used)} / {format_bytes(report.media_quota)} "
            f"({report.media_percent}%)"
        )
        database = (
            f"{format_bytes(report.db_size)} / {format_bytes(report.db_quota)} "
            f"({report.db_percent}%)"
        )
        self.stdout.write(f"Media storage: {media}")
        self.stdout.write(f"Database:      {database}")
        self.stdout.write(f"Objects:       {report.object_count}")
        self.stdout.write(f"Alert state:   {report.alert_state}")
