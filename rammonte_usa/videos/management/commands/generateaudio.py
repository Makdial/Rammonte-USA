"""Generate the MP3 audio track for videos that do not have one yet.

New uploads extract their audio automatically, and playing a video triggers a
background extraction too. This command is the "do it now" switch: useful for
backfilling older videos or retrying failures in bulk.
"""
from django.core.management.base import BaseCommand

from videos.audio_tasks import generate_audio
from videos.models import Video


class Command(BaseCommand):
    help = "Generate MP3 audio tracks for videos that are missing one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--id",
            type=int,
            action="append",
            dest="ids",
            help="Only process this video id (may be repeated).",
        )
        parser.add_argument(
            "--include-no-audio",
            action="store_true",
            help=(
                "Also retry videos previously found to have no audio stream. "
                "Without this flag such videos are left alone."
            ),
        )

    def handle(self, *args, **options):
        queryset = Video.objects.filter(audio_file_id="")
        if options["ids"]:
            queryset = queryset.filter(id__in=options["ids"])
        if not options["include_no_audio"]:
            queryset = queryset.exclude(audio_status="none")

        videos = list(queryset.order_by("id"))
        if not videos:
            self.stdout.write("No videos need an MP3.")
            return

        successes = 0
        for video in videos:
            self.stdout.write(f"Processing #{video.id} {video.title!r} ...")
            # Mirror schedule_audio_generation's lock, but run in the foreground
            # so the command only returns once the work is really done.
            Video.objects.filter(id=video.id, audio_file_id="").update(
                audio_status="processing"
            )
            if generate_audio(video.id):
                successes += 1
                video.refresh_from_db()
                self.stdout.write(
                    self.style.SUCCESS(f"  -> stored {video.audio_file_id}")
                )
            else:
                video.refresh_from_db()
                detail = video.audio_error or video.audio_status or "no audio stream"
                self.stdout.write(self.style.WARNING(f"  -> skipped ({detail})"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {successes}/{len(videos)} video(s) now have an MP3."
            )
        )
