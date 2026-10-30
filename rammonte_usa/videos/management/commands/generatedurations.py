"""Backfill the length of videos uploaded before durations were recorded.

New uploads measure their own duration at upload time. This command fills in
the videos that came before, by reading the media straight from object storage.
It is safe to re-run: only videos with no duration yet are touched.
"""
from django.core.management.base import BaseCommand

from videos.audio import get_duration_from_url
from videos.models import Video


class Command(BaseCommand):
    help = "Measure the duration of videos that do not have one recorded yet."

    def add_arguments(self, parser):
        parser.add_argument(
            "--id",
            type=int,
            action="append",
            dest="ids",
            help="Only process this video id (may be repeated).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_videos",
            help="Re-measure every video, including ones that already have one.",
        )

    def handle(self, *args, **options):
        queryset = Video.objects.all()
        if options["ids"]:
            queryset = queryset.filter(id__in=options["ids"])
        elif not options["all_videos"]:
            queryset = queryset.filter(duration_seconds=0)

        videos = list(queryset.order_by("id"))
        if not videos:
            self.stdout.write("Every video already has a duration.")
            return

        updated = 0
        for video in videos:
            url = video.playback_url if video.has_video else video.audio_playback_url
            if not url:
                self.stdout.write(
                    self.style.WARNING(f"#{video.id} {video.title!r}: no media URL")
                )
                continue

            self.stdout.write(f"Processing #{video.id} {video.title!r} ...")
            duration = get_duration_from_url(url)
            if duration <= 0:
                self.stdout.write(self.style.WARNING("  -> length unknown"))
                continue

            video.duration_seconds = duration
            video.save(update_fields=["duration_seconds"])
            updated += 1
            self.stdout.write(self.style.SUCCESS(f"  -> {video.duration_formatted}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {updated}/{len(videos)} video(s) now show a length."
            )
        )
