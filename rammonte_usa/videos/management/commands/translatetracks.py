"""Add the missing language tracks to videos that are already transcribed.

Speech-to-text is the expensive half of the caption job, and the translator is
often unavailable (out of credit, not configured yet) while the transcript is
fine. This command fills in the French and English tracks from a transcript that
is already stored, so the audio is never sent off to be listened to twice.

    python manage.py translatetracks            # every video missing a track
    python manage.py translatetracks --id 3     # just video 3
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from videos import transcription
from videos.models import Video


class Command(BaseCommand):
    help = "Translate existing transcripts into the missing language tracks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--id",
            type=int,
            action="append",
            dest="ids",
            help="Only process this video id (may be repeated).",
        )
        parser.add_argument(
            "--no-lyrics",
            action="store_true",
            help="Do not create the lyrics track when it is missing.",
        )

    def _targets(self) -> list[str]:
        return [
            code.strip()
            for code in getattr(
                settings, "SUBTITLE_TARGET_LANGUAGES", "fr,en"
            ).split(",")
            if code.strip()
        ]

    def _missing(self, video: Video) -> list[str]:
        """Target languages this video's stored transcript has not reached yet."""
        have = set(video.subtitle_tracks.values_list("language_code", flat=True))
        if not have:
            return []
        return [code for code in self._targets() if code not in have]

    def handle(self, *args, **options):
        if not transcription.translation_configured():
            self.stderr.write(
                self.style.ERROR(
                    "No translation provider is configured. Set "
                    "SUBTITLE_TRANSLATION_PROVIDER and its API key first."
                )
            )
            return

        queryset = Video.objects.exclude(audio_file_id="")
        if options["ids"]:
            queryset = queryset.filter(id__in=options["ids"])

        videos = [video for video in queryset.order_by("id") if self._missing(video)]
        if not videos:
            self.stdout.write("Every video already has all its language tracks.")
            return

        created = 0
        for video in videos:
            missing = ", ".join(self._missing(video))
            self.stdout.write(f"#{video.id} {video.title!r}: needs {missing} ...")
            try:
                result = transcription.translate_existing_tracks(
                    video.id, include_lyrics=not options["no_lyrics"]
                )
            except transcription.TranscriptionError as exc:
                self.stdout.write(self.style.WARNING(f"  -> skipped: {exc}"))
                continue
            created += len(result["languages"])
            if result["languages"]:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  -> added {', '.join(result['languages'])}"
                    )
                )
            for warning in result["warnings"]:
                self.stdout.write(self.style.WARNING(f"  -> {warning}"))

        self.stdout.write(self.style.SUCCESS(f"Done. {created} track(s) created."))
