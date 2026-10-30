"""Subtitle utilities: turn uploaded SRT/VTT text into clean WebVTT."""
from __future__ import annotations

import re

ALLOWED_SUBTITLE_EXTENSIONS = {".vtt", ".srt", ".txt"}

# SRT uses 00:00:01,500 while WebVTT uses 00:00:01.500.
_SRT_TIMESTAMP = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")

# A WebVTT timing line: 00:00:01.500 --> 00:00:03.000 (hours optional, settings
# after the end time ignored). Used to read a stored track back into cues.
_VTT_TIMING = re.compile(
    r"^(?:(\d{1,2}):)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*"
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})\.(\d{3})"
)


def _decode(content: bytes | str) -> str:
    if isinstance(content, bytes):
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                return content.decode(encoding)
            except UnicodeError:
                continue
        return content.decode("utf-8", errors="replace")
    return content


def normalize_to_vtt(content: bytes | str) -> str:
    """Return WebVTT text for ``content`` (accepts VTT, SRT or plain text).

    Plain text without timestamps is turned into a single cue covering the whole
    video so it still works as lyrics.
    """
    text = _decode(content).replace("\r\n", "\n").replace("\r", "\n")
    stripped = text.strip()

    if not stripped:
        raise ValueError("The subtitle file is empty.")

    if stripped.startswith("WEBVTT"):
        return stripped + "\n"

    if _SRT_TIMESTAMP.search(text):
        # Convert SRT timestamps and drop the numeric cue counters.
        body = _SRT_TIMESTAMP.sub(
            lambda m: f"{m.group(1)}:{m.group(2)}:{m.group(3)}.{m.group(4)}", text
        )
        lines = [
            line for line in body.split("\n")
            if not re.fullmatch(r"\d+", line.strip())
        ]
        return "WEBVTT\n\n" + "\n".join(lines).strip() + "\n"

    # Plain text (e.g. lyrics without timings): one cue from 0 to the end.
    return f"WEBVTT\n\n00:00:00.000 --> 99:59:59.000\n{stripped}\n"


def parse_vtt(content: bytes | str) -> list[tuple[float, float, str]]:
    """Read a stored WebVTT track back into ``(start, end, text)`` cues.

    The automatic pipeline saves each track it produces; this is what lets an
    existing transcript be re-used (translated into another language, for
    example) instead of paying to transcribe the same audio again.
    """
    text = _decode(content).replace("\r\n", "\n").replace("\r", "\n")
    cues: list[tuple[float, float, str]] = []
    start: float | None = None
    end: float | None = None
    body: list[str] = []

    def flush() -> None:
        if start is None or end is None:
            return
        joined = "\n".join(body).strip()
        if joined:
            cues.append((start, end, joined))

    for line in text.split("\n"):
        match = _VTT_TIMING.match(line.strip())
        if match:
            flush()
            hours, minutes, seconds, millis, e_hours, e_minutes, e_seconds, e_millis = (
                match.groups()
            )
            start = (
                int(hours or 0) * 3600
                + int(minutes) * 60
                + int(seconds)
                + int(millis) / 1000
            )
            end = (
                int(e_hours or 0) * 3600
                + int(e_minutes) * 60
                + int(e_seconds)
                + int(e_millis) / 1000
            )
            body = []
            continue
        if start is not None:
            body.append(line)

    flush()
    return cues


# Every language a subtitle track can use, as a single source of truth so the
# upload dropdown, the stored labels and automatically generated tracks always
# agree. The values are the English names; the player translates them at render
# time (see ``SubtitleTrack.display_label``), so a language without a catalogue
# entry simply shows its English name.
CAPTION_LANGUAGES = {
    "ar": "Arabic",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "hi": "Hindi",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "tr": "Turkish",
    "ur": "Urdu",
    "wo": "Wolof",
    "zh": "Chinese",
    "lyrics": "Lyrics",
}

# The synthetic "language" that holds a video's lyrics rather than captions.
LYRICS_CODE = "lyrics"


def language_choices() -> list[tuple[str, str]]:
    """The ``(code, English name)`` pairs a subtitle track may use.

    Languages come first, sorted by code, with the special lyrics track last.
    """
    choices = [
        (code, name)
        for code, name in sorted(CAPTION_LANGUAGES.items())
        if code != LYRICS_CODE
    ]
    return choices + [(LYRICS_CODE, CAPTION_LANGUAGES[LYRICS_CODE])]


def label_for(language_code: str) -> str:
    """Human readable English label for a language code."""
    return CAPTION_LANGUAGES.get(language_code, language_code)
