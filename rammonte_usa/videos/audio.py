"""Extract an MP3 audio track from an uploaded video.

A system ``ffmpeg`` is used when one is installed (the production image
installs it). ``imageio-ffmpeg``'s self-contained binary is the fallback, so a
development machine needs no system-wide ffmpeg installation.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioExtractionError(Exception):
    """Raised when ffmpeg itself fails while extracting audio."""


def _ffmpeg_exe() -> str:
    # Prefer a system ffmpeg. The static binary that imageio-ffmpeg ships
    # crashes (SIGSEGV, no output) as soon as it is handed a network URL inside
    # a container, which silently broke reading media straight from R2.
    system = shutil.which("ffmpeg")
    if system:
        return system

    # Fall back to the bundled binary; imported lazily so the rest of the site
    # works even if the optional dependency is missing.
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


# ffmpeg prints "Duration: 00:03:21.45, ..." in its header banner. Parsing that
# avoids needing ffprobe, which imageio-ffmpeg does not ship.
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def materialize(uploaded_file) -> tuple[str, str | None]:
    """Return a filesystem path for an uploaded file.

    Returns ``(path, cleanup_path)``. ``cleanup_path`` is a temporary file the
    caller should remove, or ``None`` when the upload already lives on disk
    (Django's ``TemporaryUploadedFile`` for larger files).
    """
    if hasattr(uploaded_file, "temporary_file_path"):
        return uploaded_file.temporary_file_path(), None

    suffix = Path(getattr(uploaded_file, "name", "") or "").suffix
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            uploaded_file.seek(0)
            for chunk in uploaded_file.chunks():
                handle.write(chunk)
    except Exception:
        os.unlink(temp_path)
        raise

    return temp_path, temp_path


def _build_command(source: str, dest_path: str, *, remote: bool = False) -> list[str]:
    command = [_ffmpeg_exe(), "-y", "-nostdin", "-loglevel", "error"]
    if remote:
        # When reading straight from an HTTP(S) URL, give up rather than hang
        # forever if the connection stalls.
        command += ["-rw_timeout", "60000000", "-user_agent", "Rammonte USA/1.0"]
    command += [
        "-i",
        str(source),
        "-vn",
        "-map",
        "0:a:0",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "4",
        str(dest_path),
    ]
    return command


def _run_ffmpeg(command: list[str], dest_path: str) -> bool:
    try:
        proc = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        raise AudioExtractionError(f"Could not run ffmpeg: {exc}") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        # A video without an audio stream is a normal, non-fatal case.
        if "does not contain any stream" in stderr or "matches no streams" in stderr:
            return False
        raise AudioExtractionError(f"ffmpeg failed: {stderr[:400]}")

    return os.path.exists(dest_path) and os.path.getsize(dest_path) > 0


def extract_mp3(source_path: str, dest_path: str) -> bool:
    """Extract the first audio stream of a local file to ``dest_path``.

    Returns ``True`` when an MP3 was produced and ``False`` when the video has
    no audio track. Raises :class:`AudioExtractionError` when ffmpeg fails.
    """
    return _run_ffmpeg(_build_command(source_path, dest_path), dest_path)


def extract_mp3_from_url(source_url: str, dest_path: str) -> bool:
    """Like :func:`extract_mp3`, but reads the video from an HTTP(S) URL.

    Handy for videos that are already in object storage: ffmpeg streams the
    file instead of the server downloading and storing a second copy.
    """
    return _run_ffmpeg(_build_command(source_url, dest_path, remote=True), dest_path)


def is_mp3(path: str) -> bool:
    """True when the file already is an MP3, read from its first bytes.

    The name cannot be trusted: a file called ".mp3" is often an M4A or a WAV
    that someone renamed, and those must still be converted.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(3)
    except OSError:
        return False

    if header == b"ID3":
        return True
    # An MPEG audio frame starts with eleven set bits; the layer bits follow.
    return len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0


def prepare_mp3(uploaded_file, base_name: str = "audio") -> tuple[str, list[str]]:
    """Return an MP3 path for any uploaded audio, plus the files to remove.

    An MP3 is handed back untouched (re-encoding it would only lose quality);
    anything else â€” WAV, M4A, OGG, AAC, FLAC â€” is converted, because the site
    streams and offers for download only MP3. Raises
    :class:`AudioExtractionError` when the file cannot be read as audio.
    """
    source_path, cleanup_source = materialize(uploaded_file)
    removals = [cleanup_source] if cleanup_source else []

    if is_mp3(source_path):
        return source_path, removals

    handle_fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    os.close(handle_fd)
    removals.append(mp3_path)
    try:
        if not extract_mp3(source_path, mp3_path):
            raise AudioExtractionError(
                "That file has no sound in it. Please choose an audio file."
            )
    except AudioExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported to the uploader
        raise AudioExtractionError(
            "That file could not be read as audio. Please choose an MP3, WAV or M4A file."
        ) from exc

    if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
        raise AudioExtractionError(
            "That file could not be converted to MP3. Please choose another file."
        )
    return mp3_path, removals


def _parse_duration(text: str) -> float:
    match = _DURATION_RE.search(text or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def get_duration(source_path: str) -> float:
    """Return the duration of a media file in seconds, or 0.0 if unknown."""
    command = [_ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", str(source_path)]
    try:
        # ffmpeg exits non-zero when handed no output file, but it still prints
        # the input's header (with the duration) to stderr, so the code is not
        # checked.
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    return _parse_duration(proc.stderr)


def get_duration_from_url(source_url: str) -> float:
    """Like :func:`get_duration`, but reads the media from an HTTP(S) URL."""
    command = [
        _ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-rw_timeout",
        "60000000",
        "-user_agent",
        "Rammonte USA/1.0",
        "-i",
        str(source_url),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    duration = _parse_duration(proc.stderr)
    if duration <= 0 and proc.returncode:
        # A crashed or rejected read used to pass unnoticed; say so, because a
        # silent zero means "no badge and never retried".
        logger.warning(
            "Could not read the length from %s (ffmpeg rc=%s)",
            source_url.split("?")[0],
            proc.returncode,
        )
    return duration


def duration_for_upload(uploaded_file) -> float:
    """Best-effort duration, in seconds, for a Django uploaded file.

    Materializes the upload to disk when needed and always cleans up after
    itself. Never raises: an unreadable file simply yields ``0.0``.
    """
    cleanup = None
    try:
        source_path, cleanup = materialize(uploaded_file)
        return get_duration(source_path)
    except Exception:  # noqa: BLE001 - duration is cosmetic, never fail an upload
        return 0.0
    finally:
        if cleanup:
            _remove(cleanup)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
