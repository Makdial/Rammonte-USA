"""Publish the walkthrough recordings as videos in the library.

The "how to use the app" recordings live in ``tools/demo/out`` as plain MP4
files. This command pushes each one to Cloudflare R2 and creates a matching
:class:`~videos.models.Video` row, so they appear in the library and play
through the ordinary player (and obey the ordinary permissions) instead of
being loose files on somebody's desktop.

It deliberately reuses the same helpers the upload view uses, so a published
recording is indistinguishable from one uploaded through the web form:

    python manage.py publishwalkthroughs --uploader MakhouTech

By default every ``*.mp4`` in the source directory is published. Pass one or
more ``--file`` names to be selective, or ``--dry-run`` to see what would
happen without touching R2 or the database.
"""
from pathlib import Path
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from videos.audio import duration_for_upload
from videos.models import Video
from videos.r2_storage import StorageError, is_configured, upload_video
from videos.views import _extract_audio_track

# Titles and blurbs for the known recordings. Anything not listed still gets
# published, with a title derived from its file name, so adding a language does
# not require editing this file.
KNOWN = {
    "user": {
        "title": {
            "en": "How to use Rammonte USA (viewer guide, English)",
            "fr": "Comment utiliser Rammonte USA (guide spectateur, franÃ§ais)",
            "ar": "ÙƒÙŠÙÙŠØ© Ø§Ø³ØªØ®Ø¯Ø§Ù… ÙØ²ÙƒØ± (Ø¯Ù„ÙŠÙ„ Ø§Ù„Ù…Ø´Ø§Ù‡Ø¯ØŒ Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©)",
            "wo": "Naka nga jÃ«fandikoo Rammonte USA (jÃ ppale, Wolof)",
        },
        "description": {
            "en": "A short walkthrough of the viewer features: playing a talk, "
            "audio-only listening, captions, saving to a playlist, downloading "
            "and searching.",
            "fr": "Un aperÃ§u des fonctions du spectateur : Ã©couter une causerie, "
            "l'Ã©coute audio seule, les sous-titres, sauvegarder dans une liste, "
            "tÃ©lÃ©charger et rechercher.",
            "ar": "Ù†Ø¸Ø±Ø© Ø³Ø±ÙŠØ¹Ø© Ø¹Ù„Ù‰ Ù…ÙŠØ²Ø§Øª Ø§Ù„Ù…Ø´Ø§Ù‡Ø¯: Ø§Ù„Ø§Ø³ØªÙ…Ø§Ø¹ Ø¥Ù„Ù‰ Ø¯Ø±Ø³ØŒ Ø§Ù„Ø§Ø³ØªÙ…Ø§Ø¹ "
            "Ø§Ù„ØµÙˆØªÙŠ ÙÙ‚Ø·ØŒ Ø§Ù„ØªØ±Ø¬Ù…Ø©ØŒ Ø§Ù„Ø­ÙØ¸ ÙÙŠ Ù‚Ø§Ø¦Ù…Ø©ØŒ Ø§Ù„ØªÙ†Ø²ÙŠÙ„ ÙˆØ§Ù„Ø¨Ø­Ø«.",
            "wo": "JÃ ppale bu gÃ tt ci fi mu njek: dÃ©glu waxtaan, dÃ©glu ndÃ nk, "
            "lÃ«kkalekaay, denc ci cÃ©rÃ©, wÃ llu ak seet.",
        },
    },
    "admin": {
        "title": {
            "en": "How to run Rammonte USA (administrator guide, English)",
            "fr": "GÃ©rer Rammonte USA (guide administrateur, franÃ§ais)",
            "ar": "Ø¥Ø¯Ø§Ø±Ø© ÙØ²ÙƒØ± (Ø¯Ù„ÙŠÙ„ Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„ØŒ Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©)",
            "wo": "Naka nga saytu Rammonte USA (jÃ ppale, Wolof)",
        },
        "description": {
            "en": "A short walkthrough of the administrator features: uploading "
            "a talk, editing it, generating captions, and managing the library.",
            "fr": "Un aperÃ§u des fonctions de l'administrateur : mettre en ligne "
            "une causerie, la modifier, gÃ©nÃ©rer les sous-titres et gÃ©rer la "
            "bibliothÃ¨que.",
            "ar": "Ù†Ø¸Ø±Ø© Ø³Ø±ÙŠØ¹Ø© Ø¹Ù„Ù‰ Ù…ÙŠØ²Ø§Øª Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„: Ø±ÙØ¹ Ø¯Ø±Ø³ØŒ ØªØ¹Ø¯ÙŠÙ„Ù‡ØŒ ØªÙˆÙ„ÙŠØ¯ Ø§Ù„ØªØ±Ø¬Ù…Ø©ØŒ "
            "ÙˆØ¥Ø¯Ø§Ø±Ø© Ø§Ù„Ù…ÙƒØªØ¨Ø©.",
            "wo": "JÃ ppale bu gÃ tt ci fi mu njek: yÃ³nnee waxtaan, soppi ko, "
            "defar lÃ«kkalekaay, ak saytu mbooloo mi.",
        },
    },
}

LANGUAGE_NAMES = {
    "en": "English",
    "fr": "FranÃ§ais",
    "ar": "Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©",
    "wo": "Wolof",
}


class _LocalUpload:
    """A file on disk dressed up as a Django ``UploadedFile``.

    ``materialize`` and the MP3 extractor are written for uploads, so they ask
    for ``temporary_file_path()``. Pointing that at the real file lets both work
    on a local path with no copy and no chunking.
    """

    def __init__(self, handle, name: str):
        self.file = handle
        self.name = name

    def temporary_file_path(self) -> str:
        return self.file.name

    def seek(self, position: int) -> int:
        return self.file.seek(position)

    def read(self, size: int = -1) -> bytes:
        return self.file.read(size)

    def chunks(self, chunk_size: int = 64 * 1024):
        self.file.seek(0)
        while True:
            chunk = self.file.read(chunk_size)
            if not chunk:
                return
            yield chunk


def _parse_stem(stem: str) -> tuple[str, str]:
    """Split ``user-en`` into ``("user", "en")``.

    The recorder names its output ``<audience>-<language>.mp4``, so the file
    name alone says what it is and who it is for.
    """
    audience, _, language = stem.partition("-")
    return audience.lower(), language.lower()


def _printable(text: str) -> str:
    """Make ``text`` safe to write to any console.

    Titles here are Arabic and Wolof, and a Windows console still defaults to
    cp1252, where writing them raises ``UnicodeEncodeError``. Showing the
    escaped form is better than crashing halfway through a real upload.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return text.encode("ascii", "backslashreplace").decode("ascii")
    return text


class Command(BaseCommand):
    help = "Upload the walkthrough recordings to R2 and publish them as videos."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            default="",
            help="Directory holding the MP4s (default: ../tools/demo/out).",
        )
        parser.add_argument(
            "--uploader",
            required=True,
            help="Username that will own the published videos.",
        )
        parser.add_argument(
            "--file",
            action="append",
            dest="files",
            help="Publish only this file name (may be repeated).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be published without uploading anything.",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help=(
                "Replace an existing video with the same title instead of "
                "skipping it, and delete the old R2 object."
            ),
        )

    def _source_dir(self, given: str) -> Path:
        if given:
            return Path(given).expanduser().resolve()
        # The command ships next to manage.py; the recordings live at the repo
        # root in tools/demo/out.
        return (Path(__file__).resolve().parents[4] / "tools" / "demo" / "out").resolve()

    def handle(self, *args, **options):
        source = self._source_dir(options["source"])
        if not source.is_dir():
            raise CommandError(f"No such directory: {source}")

        User = get_user_model()
        try:
            uploader = User.objects.get(username=options["uploader"])
        except User.DoesNotExist as exc:
            raise CommandError(f"No user named {options['uploader']!r}.") from exc

        wanted = set(options["files"] or [])
        files = sorted(p for p in source.glob("*.mp4") if not wanted or p.name in wanted)
        if wanted:
            missing = wanted - {p.name for p in files}
            if missing:
                raise CommandError(f"Not found in {source}: {', '.join(sorted(missing))}")
        if not files:
            raise CommandError(f"No MP4 files found in {source}")

        if not options["dry_run"] and not is_configured():
            raise CommandError(
                "Cloudflare R2 is not configured. Add the R2_* values to your .env "
                "file, or run with --dry-run."
            )

        self.stdout.write(f"Source:   {source}")
        self.stdout.write(f"Uploader: {uploader.username}")
        self.stdout.write(f"Files:    {len(files)}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing will be uploaded."))
        self.stdout.write("")

        published = 0
        for path in files:
            audience, language = _parse_stem(path.stem)
            title, description = self._describe(audience, language, path)
            existing = Video.objects.filter(title=title).first()

            if existing and not options["replace"]:
                self.stdout.write(
                    f"  skip   {path.name}  (already published as #{existing.id})"
                )
                continue
            if existing and options["replace"]:
                self.stdout.write(f"  replace {path.name}  (video #{existing.id})")

            if options["dry_run"]:
                self.stdout.write(f"  would publish {path.name} -> {_printable(title)!r}")
                continue

            self.stdout.write(f"  upload {path.name} ...")
            try:
                video = self._publish(path, uploader, title, description, existing)
            except StorageError as exc:
                self.stderr.write(self.style.ERROR(f"    failed: {exc}"))
                continue
            published += 1
            self.stdout.write(self.style.SUCCESS(f"    -> video #{video.id}  {_printable(video.title)}"))

        self.stdout.write("")
        self.stdout.write(f"Published {published} video(s).")

    def _describe(self, audience: str, language: str, path: Path) -> tuple[str, str]:
        """Title and description for one recording."""
        entry = KNOWN.get(audience)
        if entry and language in entry["title"]:
            return entry["title"][language], entry["description"][language]

        # Unknown naming: fall back to something readable rather than an error,
        # so a new audience or language still publishes.
        pretty = " ".join(part.capitalize() for part in path.stem.replace("-", " ").split())
        label = LANGUAGE_NAMES.get(language, language.upper() or "unknown")
        return f"{pretty} ({label})", f"Walkthrough recording: {path.name}."

    def _publish(self, path: Path, uploader, title: str, description: str, existing) -> Video:
        """Upload one file and create (or replace) its Video row.

        The helpers take a Django ``UploadedFile``: they either ask it for
        ``temporary_file_path()`` or read it in chunks. A plain file handle does
        neither, which is why the recording is wrapped in
        :class:`_LocalUpload` first â€” that way the duration is measured and the
        MP3 is extracted exactly as they would be for a normal upload.
        """
        with path.open("rb") as handle:
            upload = _LocalUpload(handle, path.name)
            duration = duration_for_upload(upload)
            audio_file_id, audio_url, audio_status = _extract_audio_track(upload, path.name)
            handle.seek(0)
            result = upload_video(file_data=handle, file_name=path.name)

        if existing is not None:
            self._delete_old(existing)
            video = existing
        else:
            video = Video()

        video.user = uploader
        video.title = title
        video.description = description
        video.media_type = "video"
        video.file_id = result["path"]
        video.video_url = result["url"]
        video.audio_file_id = audio_file_id
        video.audio_url = audio_url
        video.audio_status = audio_status
        video.audio_error = ""
        video.duration_seconds = duration
        video.save()
        return video

    def _delete_old(self, video) -> None:
        """Remove the R2 objects behind a video that is being replaced."""
        from videos.r2_storage import delete_file

        for path in (video.file_id, video.audio_file_id, video.thumbnail_file_id):
            if path:
                try:
                    delete_file(path)
                except StorageError:
                    pass
