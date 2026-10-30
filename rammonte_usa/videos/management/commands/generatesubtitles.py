"""Generate subtitles and translations for videos that have an MP3.

The per-video equivalent is the **Automatic subtitles** button on the video page;
this command is the "do it now, all of them" switch, useful for backfilling after
adding the feature or after a failed run.
"""
from django.core.management.base import BaseCommand

from videos import transcription
from videos.models import Video


class Command(BaseCommand):
    help = "Transcribe and translate videos using the configured subtitle provider."

    def add_arguments(self, parser):
        parser.add_argument(
            "--id",
            type=int,
            action="append",
            dest="ids",
            help="Only process this video id (may be repeated).",
        )
        parser.add_argument(
            "--skip-existing",
            action="store_true",
            help="Only process videos that do not have any subtitle track yet.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Also process videos whose previous run failed.",
        )

    def handle(self, *args, **options):
        if not transcription.is_configured():
            self.stderr.write(
                self.style.ERROR(
                    "No subtitle provider is configured. Set SUBTITLE_PROVIDER and "
                    f"{transcription.configuration_hint()} in your .env file first."
                )
            )
            return

        queryset = Video.objects.exclude(audio_file_id="")
        if options["ids"]:
            queryset = queryset.filter(id__in=options["ids"])
        if options["skip_existing"]:
            queryset = queryset.filter(subtitle_tracks__isnull=True)
        if not options["force"]:
            queryset = queryset.exclude(auto_subtitle_status="failed")

        videos = list(queryset.order_by("id"))
        if not videos:
            self.stdout.write("No videos need subtitles.")
            return

        self.stdout.write(f"Provider: {transcription.get_provider().name}")

        successes = 0
        for video in videos:
            self.stdout.write(f"Processing #{video.id} {video.title!r} ...")
            Video.objects.filter(id=video.id).update(auto_subtitle_status="processing")
            if transcription.generate_and_record(video.id):
                successes += 1
                video.refresh_from_db()
                tracks = ", ".join(
                    video.subtitle_tracks.values_list("language_code", flat=True)
                )
                self.stdout.write(
                    self.style.SUCCESS(f"  -> created {tracks or 'no tracks'}")
                )
            else:
                video.refresh_from_db()
                self.stdout.write(
                    self.style.WARNING(
                        f"  -> failed: {video.auto_subtitle_error or 'unknown error'}"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {successes}/{len(videos)} video(s) now have subtitles."
            )
        )