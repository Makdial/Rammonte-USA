"""Automatic subtitles and lyrics.

Turns a video's audio into timed subtitle tracks without anyone uploading a
file: the audio is transcribed in the spoken language and each cue is then
translated into the configured target languages.

The back end is pluggable.  A provider only has to implement two small methods
(:meth:`SubtitleProvider.transcribe` and :meth:`SubtitleProvider.translate`), so
a local/offline engine can be added later without touching anything else.

Three providers ship today, chosen with ``SUBTITLE_PROVIDER``: :class:`OpenAIProvider`
(``openai``), :class:`GoogleProvider` (``google``, Google Cloud Speech-to-Text
plus Cloud Translation), :class:`ElevenLabsProvider` (``elevenlabs``, Scribe
speech-to-text) and :class:`PuterProvider` (``puter``, Puter's AI gateway).

ElevenLabs and Puter cannot translate text, so by themselves they only produce
the source-language track. Naming a second back end in
``SUBTITLE_TRANSLATION_PROVIDER`` (``google`` or ``openai``) pairs such a
transcription with a real translation, so the target-language caption tracks are
created as well.

Pipeline per video::

    audio  ->  16 kHz mono chunks  ->  transcribe  ->  translate  ->  WebVTT

Long videos are split into chunks because speech-to-text APIs cap the size of a
single upload, and the per-chunk timestamps are shifted back so the cues line up
with the start of the full track.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import r2_storage
from .models import SubtitleTrack, Video
from .subtitles import label_for, parse_vtt

logger = logging.getLogger(__name__)

# Roughly 24 kB/s at 16 kHz mono, so ~15 MB per 10 minute chunk: comfortably
# below the 25 MB limit of the speech-to-text API while keeping cues long
# enough to stay accurate.
CHUNK_SECONDS = 480
CHUNK_SAMPLE_RATE = 16000
CHUNK_BITRATE = "32k"
TRANSLATE_BATCH = 40

# A transcript that covers almost none of a recording is not captions: it is a
# handful of phrases the transcriber happened to catch. Below these limits the
# run fails instead of publishing them, so a finished video always has captions
# that actually match it. Short clips are exempt, because a few words of speech
# can be all there is.
MIN_COVERAGE_SECONDS = 120
MIN_COVERAGE_RATIO = 0.05

# Wolof is written in both Latin script and Ajami (Arabic script). Cloud
# Translation reads the first and hands the second straight back, so any Arabic
# still present after translating is Arabic it did not translate.
ARABIC_RANGE = (0x0600, 0x06FF)
ARABIC_SUPPLEMENT_RANGE = (0x0750, 0x077F)

# An Ajami track is mixed: the transcriber spells most words in Latin and falls
# back to Arabic for the rest, so a share-based test cannot tell the two tracks
# apart (video 3's Ajami track is 86% Latin by letter). The presence of Arabic
# can: a real English or French caption track has none at all, so Arabic that
# survives a translation means that text was handed back untranslated.
MIN_ARABIC_LETTERS = 20


class TranscriptionError(Exception):
    """Raised when the pipeline cannot produce subtitles."""


@dataclass
class Segment:
    """One timed line of dialogue."""

    start: float
    end: float
    text: str


# --- WebVTT rendering -------------------------------------------------------


def format_timestamp(seconds: float) -> str:
    """Format seconds as a WebVTT timestamp (``HH:MM:SS.mmm``)."""
    seconds = max(0.0, float(seconds))
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{int(round((seconds - int(seconds)) * 1000)):03d}"


def build_vtt(segments: list[Segment]) -> str:
    """Render transcribed/translated segments as a WebVTT document."""
    lines = ["WEBVTT", ""]
    for segment in segments:
        if not segment.text.strip():
            continue
        # WebVTT requires the whole timing on a single line.
        lines.append(
            f"{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}"
        )
        lines.append(segment.text.strip())
        lines.append("")
    return "\n".join(lines)


# --- Audio chunking ---------------------------------------------------------


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


_DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _audio_duration(path: str) -> float | None:
    """Length of an audio file in seconds, read from ffmpeg's metadata.

    ``ffmpeg -i`` prints the duration and exits without decoding, so this is
    cheap even for a long video (unlike decoding the whole file to measure it).
    """
    import subprocess

    try:
        proc = subprocess.run(
            [_ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", path],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    match = _DURATION_PATTERN.search(proc.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def chunk_audio(
    source_path: str, workdir: str, chunk_seconds: int = CHUNK_SECONDS
) -> list[tuple[str, float]]:
    """Split ``source_path`` into API-sized pieces.

    ``chunk_seconds`` is how long each piece lasts; it defaults to the global
    maximum but providers that cap a single request lower (Google's synchronous
    recogniser allows about a minute) pass their own, shorter value.

    Returns ``[(path, offset_seconds), ...]`` so the caller can shift the
    per-chunk timestamps back onto the full timeline.
    """
    import subprocess

    chunk_seconds = max(1, int(chunk_seconds))
    duration = _audio_duration(source_path)
    # MP3 frames are padded, so a file reported as exactly N seconds can be a
    # hair longer. Without a little slack, the boundary chunk at N seconds
    # would be a sliver of encoder padding.
    last_start = None if duration is None else duration - 0.5
    chunks: list[tuple[str, float]] = []
    for index in range(0, 1024):  # generous ceiling: ~85 hours of audio
        start = index * chunk_seconds
        # ffmpeg writes a header even when asked for audio past the end, so the
        # output file exists but is silent. Stop by the known duration instead
        # of relying on a missing file.
        if last_start is not None and start >= last_start:
            break
        path = os.path.join(workdir, f"chunk-{index:04d}.mp3")
        command = [
            _ffmpeg_exe(),
            "-y",
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-t",
            str(chunk_seconds),
            "-i",
            source_path,
            "-ac",
            "1",
            "-ar",
            str(CHUNK_SAMPLE_RATE),
            "-b:a",
            CHUNK_BITRATE,
            path,
        ]
        try:
            proc = subprocess.run(command, capture_output=True, text=True)
        except OSError as exc:
            raise TranscriptionError(f"Could not run ffmpeg: {exc}") from exc

        # ffmpeg exits non-zero once it runs past the end of the input, which
        # simply means "no more audio".
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            if os.path.exists(path):
                os.remove(path)
            break

        chunks.append((path, float(start)))

    if not chunks:
        raise TranscriptionError("No audio could be read from the video.")
    return chunks


def download_audio(video: Video, destination: str) -> str:
    """Download a video's MP3 from R2 to a local file."""
    if not video.has_audio:
        raise TranscriptionError(
            "This video has no audio track yet, so subtitles cannot be generated."
        )
    try:
        obj = r2_storage.get_object(video.audio_file_id)
    except r2_storage.StorageError as exc:
        raise TranscriptionError(f"Could not read the audio from storage: {exc}") from exc

    written = 0
    with open(destination, "wb") as handle:
        body = obj.get("Body")
        if body is None:
            raise TranscriptionError("Storage returned no audio data.")
        try:
            for piece in body.iter_chunks(chunk_size=1024 * 1024):
                handle.write(piece)
                written += len(piece)
        finally:
            body.close()

    if written == 0:
        raise TranscriptionError("The stored audio file was empty.")
    return destination


# --- Providers --------------------------------------------------------------


class SubtitleProvider:
    """Interface every speech-to-text / translation back end implements."""

    name = "base"

    # False for providers that can transcribe but have no text-translation
    # API (ElevenLabs only translates audio through its dubbing workflow), so
    # the pipeline saves the source track instead of failing on every target.
    supports_translation = True

    # Longest slice of audio handed to :meth:`transcribe` in one call. Back ends
    # whose API caps a request lower than the default shorten it (see
    # :class:`GoogleProvider`, whose synchronous recogniser allows ~1 minute).
    chunk_seconds = CHUNK_SECONDS

    def transcribe(self, path: str, language: str) -> list[Segment]:
        raise NotImplementedError

    def translate(self, segments: list[Segment], target_language: str) -> list[Segment]:
        raise NotImplementedError

    def translate_from_audio(
        self,
        audio_path: str,
        source_language: str,
        target_language: str,
        segments: list[Segment],
    ) -> list[Segment]:
        """Produce the target-language cues from the audio itself.

        A provider that translates a whole transcript (rather than a list of
        cues) needs the audio to work from. The default says it cannot, and
        the pipeline falls back to :meth:`translate`.
        """
        return []


class OpenAIProvider(SubtitleProvider):
    """OpenAI: ``whisper-1`` for speech-to-text, a chat model for translation.

    ``whisper-1`` is used rather than ``gpt-4o-transcribe`` because only the
    former returns the per-segment timestamps subtitles need.
    """

    name = "openai"

    def __init__(self):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise TranscriptionError(
                "The 'openai' package is not installed. "
                "Run: pip install openai"
            ) from exc

        api_key = getattr(settings, "OPENAI_API_KEY", "")
        if not api_key:
            raise TranscriptionError(
                "OPENAI_API_KEY is not set, so subtitles cannot be generated."
            )
        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key)

    def transcribe(self, path: str, language: str) -> list[Segment]:
        model = getattr(settings, "SUBTITLE_TRANSCRIBE_MODEL", "whisper-1")
        with open(path, "rb") as handle:
            try:
                result = self._client.audio.transcriptions.create(
                    model=model,
                    file=handle,
                    language=language,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )
            except Exception as exc:  # noqa: BLE001 - SDK raises many types
                raise TranscriptionError(f"Transcription failed: {exc}") from exc

        segments = [
            Segment(
                start=float(item.start),
                end=float(item.end),
                text=(item.text or "").strip(),
            )
            for item in getattr(result, "segments", None) or []
        ]
        if not segments and (getattr(result, "text", "") or "").strip():
            # No timestamps available: fall back to one full-length cue so the
            # translation is still useful instead of losing it entirely.
            segments = [Segment(0.0, CHUNK_SECONDS, result.text.strip())]
        return [segment for segment in segments if segment.text]

    def translate(self, segments: list[Segment], target_language: str) -> list[Segment]:
        model = getattr(settings, "SUBTITLE_TRANSLATE_MODEL", "gpt-4o-mini")
        translated: list[Segment] = []

        for start in range(0, len(segments), TRANSLATE_BATCH):
            batch = segments[start : start + TRANSLATE_BATCH]
            texts = self._translate_batch(batch, target_language, model)
            for segment, text in zip(batch, texts):
                if text:
                    translated.append(Segment(segment.start, segment.end, text))

        if len(translated) != len(segments):
            raise TranscriptionError(
                "The translation returned a different number of lines than expected."
            )
        return translated

    def _translate_batch(
        self, batch: list[Segment], target_language: str, model: str
    ) -> list[str]:
        numbered = "\n".join(
            f"{index}: {segment.text}" for index, segment in enumerate(batch)
        )
        prompt = (
            "Translate the following subtitle lines into "
            f"{target_language}. Keep the numbering exactly as it is and return "
            "only the translated lines, one per line, with no numbering, no extra "
            "commentary and no empty lines. Preserve the meaning, punctuation and "
            "line breaks within each numbered line.\n\n" + numbered
        )
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            content = response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 - SDK raises many types
            raise TranscriptionError(f"Translation failed: {exc}") from exc

        lines = [line.strip() for line in content.splitlines() if line.strip()]
        # Tolerate a model that echoes the numbering back.
        cleaned: list[str] = []
        for index, line in enumerate(lines):
            prefix = f"{index}:"
            if line.startswith(prefix):
                line = line[len(prefix) :].strip()
            cleaned.append(line)

        if len(cleaned) != len(batch):
            raise TranscriptionError(
                f"Expected {len(batch)} translated lines but got {len(cleaned)}."
            )
        return cleaned


# --- Google Cloud provider --------------------------------------------------
#
# Speech-to-Text turns each audio chunk into timed words; Cloud Translation
# turns those words into captions for every target language. Both services take
# the same API key, so a single ``GOOGLE_API_KEY`` enables the pair.

GOOGLE_SPEECH_URL = "https://speech.googleapis.com/v1/speech:recognize"
GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
GOOGLE_HTTP_TIMEOUT = 180.0

# The synchronous ``recognize`` endpoint accepts at most about one minute (and
# 10 MB) of audio, so Google chunks are much shorter than the OpenAI ones.
GOOGLE_CHUNK_SECONDS = 55

# Spoken-language codes: our short codes -> the BCP-47 tags Speech-to-Text
# expects. A tag that already contains a dash is passed through untouched, and
# ``GOOGLE_SPEECH_LANGUAGE`` overrides the lot (useful for an Arabic dialect
# other than ``ar-SA``).
GOOGLE_SPEECH_LANGUAGES = {
    "ar": "ar-SA",
    "en": "en-US",
    "fr": "fr-FR",
    "es": "es-ES",
    "tr": "tr-TR",
}

# How a long run of speech is broken into readable caption lines.
CAPTION_MAX_SECONDS = 7.0
CAPTION_MAX_CHARS = 90
CAPTION_GAP_SECONDS = 1.5
CAPTION_SENTENCE_ENDINGS = ".!?…。？！"


def speech_language_code(language: str) -> str:
    """Return the Speech-to-Text code for one of our language codes."""
    override = (getattr(settings, "GOOGLE_SPEECH_LANGUAGE", "") or "").strip()
    if override:
        return override
    language = (language or "").strip()
    if not language:
        return "en-US"
    if "-" in language:
        return language
    return GOOGLE_SPEECH_LANGUAGES.get(language.lower(), language)


def translate_language_code(language: str) -> str:
    """Return the Cloud Translation code for one of our language codes."""
    return (language or "").strip().lower()


def _google_seconds(value) -> float:
    """Convert a Speech-to-Text duration such as ``"1.500s"`` to seconds."""
    if value is None:
        return 0.0
    try:
        return float(str(value).strip().rstrip("s"))
    except ValueError:
        return 0.0


def words_from_speech_results(results) -> list[tuple[str, float, float]]:
    """Flatten ``results`` into ``(text, start, end)`` timed words."""
    words: list[tuple[str, float, float]] = []
    for result in results or []:
        alternatives = result.get("alternatives") or []
        if not alternatives:
            continue
        for word in alternatives[0].get("words") or []:
            text = (word.get("word") or "").strip()
            if text:
                words.append(
                    (
                        text,
                        _google_seconds(word.get("startTime")),
                        _google_seconds(word.get("endTime")),
                    )
                )
    return words


def build_captions(words) -> list[Segment]:
    """Group timed words into YouTube-style caption lines.

    A new line starts on a sentence ending, after a pause, or once the current
    line has grown long enough to be hard to read.
    """
    segments: list[Segment] = []
    buffer: list[tuple[str, float, float]] = []
    length = 0

    def flush() -> None:
        nonlocal buffer, length
        if buffer:
            text = " ".join(item[0] for item in buffer)
            start = buffer[0][1]
            end = buffer[-1][2]
            if end <= start:
                end = start + 0.5
            segments.append(Segment(start, end, text))
        buffer = []
        length = 0

    for text, start, end in words:
        # Scribe emits standalone punctuation as its own word; on its own it is
        # not a caption line ("."), so keep it only when attached to text.
        if not any(char.isalnum() for char in text):
            continue
        if buffer:
            gap = start - buffer[-1][2]
            duration = end - buffer[0][1]
            if (
                gap > CAPTION_GAP_SECONDS
                or duration > CAPTION_MAX_SECONDS
                or length + len(text) + 1 > CAPTION_MAX_CHARS
            ):
                flush()
        buffer.append((text, start, end))
        length += len(text) + 1
        if text and text[-1] in CAPTION_SENTENCE_ENDINGS:
            flush()

    flush()
    return segments


def captions_from_speech_results(results) -> list[Segment]:
    """Turn a Speech-to-Text response into timed caption segments."""
    words = words_from_speech_results(results)
    if words:
        return build_captions(words)

    # Word timings are only absent when the API refuses to return them; fall
    # back to one caption per result so the transcription is not lost.
    segments: list[Segment] = []
    cursor = 0.0
    for result in results or []:
        alternatives = result.get("alternatives") or []
        text = ((alternatives[0].get("transcript") if alternatives else "") or "").strip()
        if not text:
            continue
        duration = max(1.0, min(CAPTION_MAX_SECONDS, 0.4 * len(text.split()) + 0.5))
        segments.append(Segment(cursor, cursor + duration, text))
        cursor += duration
    return segments


def _google_error(response) -> str:
    """A readable message taken from a failed Google API response."""
    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    message = error.get("message") or (getattr(response, "text", "") or "").strip()
    return f"Google rejected the request: {message or f'HTTP {response.status_code}'}"


def _google_post_json(url: str, payload: dict, api_key: str) -> dict:
    """POST ``payload`` to a Google API and return the decoded JSON.

    ``httpx`` is imported lazily so the module keeps working for installs that
    only ever use the OpenAI provider.
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise TranscriptionError(
            "The 'httpx' package is not installed. Run: pip install httpx"
        ) from exc

    try:
        response = httpx.post(
            url, params={"key": api_key}, json=payload, timeout=GOOGLE_HTTP_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"Could not reach Google: {exc}") from exc

    if response.status_code >= 400:
        raise TranscriptionError(_google_error(response))
    try:
        return response.json()
    except ValueError as exc:
        raise TranscriptionError("Google returned an unexpected response.") from exc


class GoogleProvider(SubtitleProvider):
    """Google Cloud: Speech-to-Text transcription and Translation captions.

    Authenticates with a single API key (``GOOGLE_API_KEY``). Transcription uses
    the synchronous recogniser, whose ~1 minute ceiling is why this provider
    asks for shorter audio chunks, and returns word-level timings that are
    grouped into caption lines. Translation uses Cloud Translation v2 with its
    Neural Machine Translation model (``GOOGLE_TRANSLATE_MODEL``).
    """

    name = "google"
    chunk_seconds = GOOGLE_CHUNK_SECONDS

    def __init__(self):
        api_key = (getattr(settings, "GOOGLE_API_KEY", "") or "").strip()
        if not api_key:
            raise TranscriptionError(
                "GOOGLE_API_KEY is not set, so subtitles cannot be generated."
            )
        self._api_key = api_key
        self._source_language = ""

    def transcribe(self, path: str, language: str) -> list[Segment]:
        # Remembered so translation can be told the source language instead of
        # auto-detecting it line by line.
        self._source_language = language

        with open(path, "rb") as handle:
            audio = base64.b64encode(handle.read()).decode("ascii")

        config = {
            "encoding": "MP3",
            "sampleRateHertz": CHUNK_SAMPLE_RATE,
            "audioChannelCount": 1,
            "languageCode": speech_language_code(language),
            "enableAutomaticPunctuation": True,
            "enableWordTimeOffsets": True,
        }
        model = (getattr(settings, "GOOGLE_SPEECH_MODEL", "") or "").strip()
        if model:
            config["model"] = model
        alternatives = [
            speech_language_code(code)
            for code in (
                getattr(settings, "GOOGLE_SPEECH_ALTERNATIVE_LANGUAGES", "") or ""
            ).split(",")
            if code.strip()
        ]
        if alternatives:
            config["alternativeLanguageCodes"] = alternatives

        data = self._post_json(
            GOOGLE_SPEECH_URL, {"config": config, "audio": {"content": audio}}
        )
        return captions_from_speech_results(data.get("results"))

    def translate(self, segments: list[Segment], target_language: str) -> list[Segment]:
        target = translate_language_code(target_language)
        source = translate_language_code(self._source_language)
        translated: list[Segment] = []

        for start in range(0, len(segments), TRANSLATE_BATCH):
            batch = segments[start : start + TRANSLATE_BATCH]
            texts = self._translate_batch(
                [segment.text for segment in batch], target, source
            )
            for segment, text in zip(batch, texts):
                translated.append(Segment(segment.start, segment.end, text))

        if len(translated) != len(segments):
            raise TranscriptionError(
                "The translation returned a different number of lines than expected."
            )
        return translated

    def _translate_batch(
        self, texts: list[str], target: str, source: str
    ) -> list[str]:
        payload = {"q": texts, "target": target, "format": "text"}
        model = (getattr(settings, "GOOGLE_TRANSLATE_MODEL", "") or "").strip()
        if model:
            payload["model"] = model
        if source:
            payload["source"] = source

        data = self._post_json(GOOGLE_TRANSLATE_URL, payload)
        translations = (data.get("data") or {}).get("translations") or []
        # Cloud Translation escapes punctuation even in ``text`` mode.
        return [
            html.unescape((item.get("translatedText") or "").strip())
            for item in translations
        ]

    def _post_json(self, url: str, payload: dict) -> dict:
        return _google_post_json(url, payload, self._api_key)


# --- ElevenLabs provider ----------------------------------------------------
#
# Scribe returns word-level timings, which are grouped into the same caption
# lines as the Google provider. ElevenLabs has no text-translation endpoint, so
# this provider only produces the source-language track; the pipeline skips the
# target languages when ``supports_translation`` is false.

ELEVENLABS_SPEECH_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_HTTP_TIMEOUT = 300.0

# Scribe accepts files up to 5 GB / 10 hours; this is only a sane request size
# that still keeps each upload comfortably small.
ELEVENLABS_CHUNK_SECONDS = 480


def elevenlabs_language_code(language: str) -> str:
    """The ISO-639-1 code to pass to Scribe, or ``""`` to auto-detect.

    ``ELEVENLABS_LANGUAGE_CODE`` overrides the source language; set it to
    ``auto`` to let Scribe detect the language of every chunk.
    """
    override = (getattr(settings, "ELEVENLABS_LANGUAGE_CODE", "") or "").strip()
    if override:
        return "" if override.lower() == "auto" else override
    return (language or "").strip().lower()


def words_from_elevenlabs(result) -> list[tuple[str, float, float]]:
    """Flatten a Scribe response into ``(text, start, end)`` timed words."""
    words: list[tuple[str, float, float]] = []
    for item in (result or {}).get("words") or []:
        # Some back ends tag their entries ("spacing" and "audio_event" carry
        # no caption text); others leave the tag off entirely.
        kind = item.get("type")
        if kind is not None and kind != "word":
            continue
        text = (item.get("text") or "").strip()
        start = item.get("start")
        end = item.get("end")
        if not text or start is None or end is None:
            continue
        try:
            words.append((text, float(start), float(end)))
        except (TypeError, ValueError):
            continue
    return words


def captions_from_elevenlabs(result) -> list[Segment]:
    """Turn a Scribe response into timed caption segments."""
    words = words_from_elevenlabs(result)
    if words:
        return build_captions(words)

    # No word timings: keep the transcript as one cue spanning the audio.
    text = ((result or {}).get("text") or "").strip()
    if not text:
        return []
    duration = (result or {}).get("audio_duration_secs") or 0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        duration = max(1.0, 0.4 * len(text.split()) + 0.5)
    return [Segment(0.0, duration, text)]


def _elevenlabs_error(response) -> str:
    """A readable message taken from a failed ElevenLabs response."""
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("code") or ""
    elif isinstance(detail, list):
        message = "; ".join(
            str(item.get("msg", item)) if isinstance(item, dict) else str(item)
            for item in detail
        )
    else:
        message = str(detail or "")
    message = message or (getattr(response, "text", "") or "").strip()
    return f"ElevenLabs rejected the request: {message or f'HTTP {response.status_code}'}"


def _elevenlabs_transcribe_request(api_key: str, path: str, data: dict) -> dict:
    """POST one audio file to Scribe and return the decoded JSON."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise TranscriptionError(
            "The 'httpx' package is not installed. Run: pip install httpx"
        ) from exc

    try:
        with open(path, "rb") as handle:
            response = httpx.post(
                ELEVENLABS_SPEECH_URL,
                headers={"xi-api-key": api_key},
                data=data,
                files={"file": (os.path.basename(path), handle, "audio/mpeg")},
                timeout=ELEVENLABS_HTTP_TIMEOUT,
            )
    except (OSError, httpx.HTTPError) as exc:
        raise TranscriptionError(f"Could not reach ElevenLabs: {exc}") from exc

    if response.status_code >= 400:
        raise TranscriptionError(_elevenlabs_error(response))
    try:
        return response.json()
    except ValueError as exc:
        raise TranscriptionError("ElevenLabs returned an unexpected response.") from exc


class ElevenLabsProvider(SubtitleProvider):
    """ElevenLabs Scribe for speech-to-text.

    Authenticates with ``ELEVENLABS_API_KEY``. Scribe returns word-level
    timings, which become YouTube-style caption lines. ElevenLabs only
    translates audio (through dubbing), not text, so this provider produces the
    source-language track and the pipeline does not attempt translations.
    """

    name = "elevenlabs"
    chunk_seconds = ELEVENLABS_CHUNK_SECONDS
    supports_translation = False

    def __init__(self):
        api_key = (getattr(settings, "ELEVENLABS_API_KEY", "") or "").strip()
        if not api_key:
            raise TranscriptionError(
                "ELEVENLABS_API_KEY is not set, so subtitles cannot be generated."
            )
        self._api_key = api_key

    def transcribe(self, path: str, language: str) -> list[Segment]:
        data = {
            "model_id": (
                getattr(settings, "ELEVENLABS_STT_MODEL", "") or "scribe_v2"
            ).strip(),
            "timestamps_granularity": "word",
        }
        code = elevenlabs_language_code(language)
        if code:
            data["language_code"] = code

        result = _elevenlabs_transcribe_request(self._api_key, path, data)
        return captions_from_elevenlabs(result)

    def translate(self, segments: list[Segment], target_language: str) -> list[Segment]:
        raise TranscriptionError(
            "ElevenLabs does not provide text translation, so only the "
            "source-language track can be generated."
        )


# --- AssemblyAI provider -----------------------------------------------------
#
# AssemblyAI transcribes a file by uploading it, submitting a job and
# polling for the result. The answer carries per-word timings, which become
# the usual caption lines.
#
# It also translates: a transcript can be sent to the LLM Gateway with a list
# of target languages, and the whole text comes back translated. That is a
# whole-text translation, so the translated sentences are spread over the cue
# timings we already have.

ASSEMBLYAI_API_URL = "https://api.assemblyai.com/v2"
ASSEMBLYAI_HTTP_TIMEOUT = 300.0
ASSEMBLYAI_POLL_SECONDS = 3.0
ASSEMBLYAI_POLL_ATTEMPTS = 120

# A sentence ends on one of these, and a cue is never longer than this.
SENTENCE_ENDINGS = ".!?…؟"


def _assemblyai_error(response) -> str:
    """A readable message taken from a failed AssemblyAI response."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    message = payload.get("error") or (getattr(response, "text", "") or "").strip()
    return f"AssemblyAI rejected the request: {message or f'HTTP {response.status_code}'}"


def split_sentences(text: str) -> list[str]:
    """Split a block of text into sentences."""
    sentences: list[str] = []
    current: list[str] = []
    for chunk in re.split(r"(?<=[.!?…؟])\s+", (text or "").strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        current.append(chunk)
        if chunk[-1] in SENTENCE_ENDINGS:
            sentences.append(" ".join(current))
            current = []
    if current:
        sentences.append(" ".join(current))
    return sentences


def assemblyai_sentences(transcript) -> list[tuple[float, float, str]]:
    """Group AssemblyAI's words into sentences with their timings.

    Returns ``[(start, end, text), ...]`` in seconds.
    """
    words = (transcript or {}).get("words") or []
    sentences: list[tuple[float, float, str]] = []
    buffer: list[str] = []
    start = 0.0
    end = 0.0

    for word in words:
        text = (word.get("text") or "").strip()
        if not text:
            continue
        try:
            word_start = float(word.get("start") or 0) / 1000.0
            word_end = float(word.get("end") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        if not buffer:
            start = word_start
        buffer.append(text)
        end = word_end
        if text[-1] in SENTENCE_ENDINGS:
            sentences.append((start, end, " ".join(buffer)))
            buffer = []

    if buffer:
        sentences.append((start, end, " ".join(buffer)))
    return sentences


def captions_from_assemblyai(transcript) -> list[Segment]:
    """Turn an AssemblyAI transcript into timed caption segments."""
    words: list[tuple[str, float, float]] = []
    for word in (transcript or {}).get("words") or []:
        text = (word.get("text") or "").strip()
        if not text:
            continue
        try:
            words.append(
                (text, float(word.get("start") or 0) / 1000.0, float(word.get("end") or 0) / 1000.0)
            )
        except (TypeError, ValueError):
            continue
    if words:
        return build_captions(words)

    # No word timings: use the utterances instead.
    segments: list[Segment] = []
    for item in (transcript or {}).get("utterances") or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("start") or 0) / 1000.0
            end = float(item.get("end") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        segments.append(Segment(start, end, text))
    return segments


def spread_translation(
    segments: list[Segment], sentences: list[tuple[float, float, str]]
) -> list[Segment]:
    """Spread translated sentences over the timings of the original cues.

    AssemblyAI translates a whole transcript, not one cue at a time, so the
    translated sentences are handed out to the cues they overlap. A sentence
    covering several cues is split between them in proportion to how long
    each cue lasts, which keeps the text in step with the picture.
    """
    if not segments:
        return []
    if not sentences:
        return [Segment(s.start, s.end, s.text) for s in segments]

    translated: list[Segment] = []
    for segment in segments:
        # The sentence that overlaps this cue the most.
        best = None
        best_overlap = 0.0
        for start, end, text in sentences:
            overlap = min(end, segment.end) - max(start, segment.start)
            if overlap > best_overlap:
                best_overlap = overlap
                best = (start, end, text)
        if best is None:
            translated.append(Segment(segment.start, segment.end, segment.text))
            continue
        translated.append(Segment(segment.start, segment.end, best[2]))

    # A sentence that spans several cues would otherwise be repeated on each
    # of them, so it is divided between them instead.
    for index, (start, end, text) in enumerate(sentences):
        covered = [
            i
            for i, segment in enumerate(translated)
            if min(end, segment.end) - max(start, segment.start) > 0
        ]
        if len(covered) < 2:
            continue
        total = sum(
            max(0.0, min(end, translated[i].end) - max(start, translated[i].start))
            for i in covered
        )
        if total <= 0:
            continue
        position = 0
        for number, cue in enumerate(covered):
            share = max(
                0.0, min(end, translated[cue].end) - max(start, translated[cue].start)
            )
            cut = len(text) if number == len(covered) - 1 else int(
                round(len(text) * (share / total))
            )
            cut = max(position, min(len(text), cut))
            translated[cue] = Segment(
                translated[cue].start, translated[cue].end, text[position:cut].strip()
            )
            position = cut

    return [segment for segment in translated if segment.text]


class AssemblyAIProvider(SubtitleProvider):
    """AssemblyAI for speech-to-text and for the translation of the cues.

    Authenticates with ``ASSEMBLYAI_API_KEY``. The audio is uploaded and
    transcribed; the answer carries per-word timings, which become the
    caption lines. AssemblyAI can also translate a transcript, so this
    provider can produce the target-language tracks on its own.
    """

    name = "assemblyai"
    supports_translation = True

    def __init__(self):
        api_key = (getattr(settings, "ASSEMBLYAI_API_KEY", "") or "").strip()
        if not api_key:
            raise TranscriptionError(
                "ASSEMBLYAI_API_KEY is not set, so subtitles cannot be generated."
            )
        self._key = api_key

    # -- HTTP ----------------------------------------------------------------

    def _headers(self) -> dict:
        return {"authorization": self._key}

    def _upload(self, path: str) -> str:
        """Send a local audio file to AssemblyAI and return its address."""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise TranscriptionError(
                "The 'httpx' package is not installed. Run: pip install httpx"
            ) from exc

        with open(path, "rb") as handle:
            try:
                response = httpx.post(
                    f"{ASSEMBLYAI_API_URL}/upload",
                    headers=self._headers(),
                    files={"file": (os.path.basename(path), handle, "audio/mpeg")},
                    timeout=ASSEMBLYAI_HTTP_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise TranscriptionError(f"Could not reach AssemblyAI: {exc}") from exc

        if response.status_code >= 400:
            raise TranscriptionError(_assemblyai_error(response))
        try:
            return response.json()["upload_url"]
        except (ValueError, KeyError) as exc:
            raise TranscriptionError(
                "AssemblyAI did not return an upload address."
            ) from exc

    def _submit(self, upload_url: str, translate_to: list[str]) -> str:
        """Start a transcription job and return its id."""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise TranscriptionError(
                "The 'httpx' package is not installed. Run: pip install httpx"
            ) from exc

        payload: dict = {
            "audio_url": upload_url,
            "language_detection": True,
            "punctuate": True,
            "format_text": True,
        }
        if translate_to:
            payload["speech_understanding"] = {
                "request": {
                    "translation": {
                        "target_languages": translate_to,
                        "formal": False,
                        "force_translation": True,
                    }
                }
            }

        try:
            response = httpx.post(
                f"{ASSEMBLYAI_API_URL}/transcript",
                headers={**self._headers(), "content-type": "application/json"},
                json=payload,
                timeout=ASSEMBLYAI_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"Could not reach AssemblyAI: {exc}") from exc

        if response.status_code >= 400:
            raise TranscriptionError(_assemblyai_error(response))
        try:
            return response.json()["id"]
        except (ValueError, KeyError) as exc:
            raise TranscriptionError(
                "AssemblyAI did not return a transcript id."
            ) from exc

    def _wait(self, transcript_id: str) -> dict:
        """Poll until the transcript is ready, then return it."""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise TranscriptionError(
                "The 'httpx' package is not installed. Run: pip install httpx"
            ) from exc

        import time

        for _ in range(ASSEMBLYAI_POLL_ATTEMPTS):
            try:
                response = httpx.get(
                    f"{ASSEMBLYAI_API_URL}/transcript/{transcript_id}",
                    headers=self._headers(),
                    timeout=ASSEMBLYAI_HTTP_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise TranscriptionError(f"Could not reach AssemblyAI: {exc}") from exc

            if response.status_code >= 400:
                raise TranscriptionError(_assemblyai_error(response))
            try:
                body = response.json()
            except ValueError as exc:
                raise TranscriptionError(
                    "AssemblyAI returned an unexpected response."
                ) from exc

            status = body.get("status")
            if status == "completed":
                return body
            if status == "error":
                raise TranscriptionError(
                    f"AssemblyAI could not transcribe the audio: {body.get('error')}"
                )
            time.sleep(ASSEMBLYAI_POLL_SECONDS)

        raise TranscriptionError("AssemblyAI did not finish in time.")

    def _transcript(self, path: str, translate_to: list[str]) -> dict:
        """Upload, submit and wait for one audio file."""
        upload_url = self._upload(path)
        transcript_id = self._submit(upload_url, translate_to)
        return self._wait(transcript_id)

    # -- Provider interface --------------------------------------------------

    def transcribe(self, path: str, language: str) -> list[Segment]:
        transcript = self._transcript(path, [])
        return captions_from_assemblyai(transcript)

    def translate(self, segments: list[Segment], target_language: str) -> list[Segment]:
        raise TranscriptionError(
            "AssemblyAI translates a transcript, not a list of cues. Use the "
            "audio to produce the translated tracks."
        )

    def translate_from_audio(
        self,
        audio_path: str,
        source_language: str,
        target_language: str,
        segments: list[Segment],
    ) -> list[Segment]:
        """Produce the target-language cues from the audio itself.

        AssemblyAI transcribes the audio, translates its transcript, and the
        translated sentences are spread over the cue timings that were
        already worked out, so the tracks stay in step.
        """
        transcript = self._transcript(audio_path, [target_language])
        translated = (transcript.get("translated_texts") or {}).get(target_language, "")
        if not (translated or "").strip():
            return []

        # The translated text is matched with the sentences of the original
        # transcript, in order, and those carry the timings.
        original = split_sentences(transcript.get("text") or "")
        pieces = split_sentences(translated)
        sentences: list[tuple[float, float, str]] = []
        index = 0
        for start, end, _text in assemblyai_sentences(transcript):
            piece = pieces[index] if index < len(pieces) else ""
            sentences.append((start, end, piece))
            index += 1
        if not sentences and pieces:
            # No word timings: fall back to the plain sentences.
            sentences = [(0.0, 0.0, piece) for piece in pieces]

        return spread_translation(segments, sentences)


# --- Puter provider ----------------------------------------------------------
#
# Puter's AI gateway is reached through one driver endpoint. The audio is
# sent the same way the browser SDK sends it (as a data URL inside the
# request body) and the answer carries per-word timings, which become the
# usual caption lines.
#
# Puter follows the "user-pays" model: the account that owns the auth token
# covers the usage, so the site operator pays through that account rather
# than through a separate API bill.

PUTER_DRIVER_URL = "https://api.puter.com/drivers/call"
PUTER_HTTP_TIMEOUT = 300.0

# Gemini allows half an hour of audio when word timestamps are wanted, but
# keeping the pieces short keeps each request body small.
PUTER_CHUNK_SECONDS = 300

# The chat driver is a different interface on the same endpoint; it is what
# translates the caption lines.
PUTER_CHAT_INTERFACE = "puter-chat-completion"
PUTER_CHAT_DRIVER = "ai-chat"


def puter_translate_model() -> str:
    """The model Puter should translate with."""
    return (getattr(settings, "PUTER_TRANSLATE_MODEL", "") or "gpt-5.6-luna").strip()


def puter_stt_provider() -> str:
    """Which Puter back end transcribes: ``gemini``, ``openai`` or ``xai``."""
    return (getattr(settings, "PUTER_STT_PROVIDER", "") or "gemini").strip().lower()


def puter_stt_model() -> str:
    """The model Puter should transcribe with."""
    configured = (getattr(settings, "PUTER_STT_MODEL", "") or "").strip()
    if configured:
        return configured
    return "gemini-3.5-transcribe"


def puter_language_code(language: str) -> str:
    """The language hint to send, or ``""`` to let Puter decide.

    ``PUTER_LANGUAGE_CODE`` overrides the source language; set it to ``auto``
    to let the back end detect the language of every chunk.
    """
    override = (getattr(settings, "PUTER_LANGUAGE_CODE", "") or "").strip()
    if override:
        return "" if override.lower() == "auto" else override
    return (language or "").strip().lower()


def words_from_puter_result(result) -> list[tuple[str, float, float]]:
    """Flatten a Puter answer into ``(text, start, end)`` timed words."""
    words: list[tuple[str, float, float]] = []
    for item in (result or {}).get("words") or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        words.append((text, start, end))
    return words


def captions_from_puter_result(result) -> list[Segment]:
    """Turn a Puter answer into timed caption segments."""
    words = words_from_puter_result(result)
    if words:
        return build_captions(words)

    # No word timings: use the segments the back end returned itself.
    segments: list[Segment] = []
    for item in (result or {}).get("segments") or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or start)
        except (TypeError, ValueError):
            continue
        segments.append(Segment(start, end, text))
    if segments:
        return segments

    # Last resort: the whole transcript as one cue.
    text = ((result or {}).get("text") or "").strip()
    if not text:
        return []
    try:
        duration = float((result or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        duration = max(1.0, 0.4 * len(text.split()) + 0.5)
    return [Segment(0.0, duration, text)]


def _puter_error(response) -> str:
    """A readable message taken from a failed Puter response."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    message = payload.get("message") or payload.get("error") if isinstance(payload, dict) else ""
    if isinstance(message, dict):
        message = message.get("message") or message.get("code") or ""
    message = str(message or (getattr(response, "text", "") or "").strip())
    return f"Puter rejected the request: {message or f'HTTP {response.status_code}'}"


def _puter_transcribe_request(token: str, path: str, args: dict) -> dict:
    """Send one audio file to Puter's driver endpoint and return the answer."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise TranscriptionError(
            "The 'httpx' package is not installed. Run: pip install httpx"
        ) from exc

    with open(path, "rb") as handle:
        audio = base64.b64encode(handle.read()).decode("ascii")

    body = {
        "interface": "puter-speech2txt",
        "driver": "ai-speech2txt",
        "method": "transcribe",
        "args": {"file": f"data:audio/mpeg;base64,{audio}", **args},
        "test_mode": False,
        "auth_token": token,
    }

    try:
        response = httpx.post(
            PUTER_DRIVER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/plain;actually=json",
            },
            content=json.dumps(body).encode("utf-8"),
            timeout=PUTER_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"Could not reach Puter: {exc}") from exc

    if response.status_code >= 400:
        raise TranscriptionError(_puter_error(response))
    try:
        payload = response.json()
    except ValueError as exc:
        raise TranscriptionError("Puter returned an unexpected response.") from exc

    if not isinstance(payload, dict):
        return {}
    if payload.get("error"):
        raise TranscriptionError(_puter_error(response))
    # The driver answers with the transcript itself, sometimes wrapped in a
    # "result" envelope.
    inner = payload.get("result")
    if isinstance(inner, dict):
        return inner
    return payload


def _puter_chat_request(token: str, args: dict) -> dict:
    """Send one chat request to Puter's driver endpoint and return the answer."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise TranscriptionError(
            "The 'httpx' package is not installed. Run: pip install httpx"
        ) from exc

    body = {
        "interface": PUTER_CHAT_INTERFACE,
        "driver": PUTER_CHAT_DRIVER,
        "method": "complete",
        "args": args,
        "test_mode": False,
        "auth_token": token,
    }

    try:
        response = httpx.post(
            PUTER_DRIVER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/plain;actually=json",
            },
            content=json.dumps(body).encode("utf-8"),
            timeout=PUTER_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"Could not reach Puter: {exc}") from exc

    if response.status_code >= 400:
        raise TranscriptionError(_puter_error(response))
    try:
        payload = response.json()
    except ValueError as exc:
        raise TranscriptionError("Puter returned an unexpected response.") from exc

    if not isinstance(payload, dict):
        return {}
    if payload.get("error"):
        raise TranscriptionError(_puter_error(response))
    inner = payload.get("result")
    if isinstance(inner, dict):
        return inner
    return payload


def _puter_message_text(payload: dict) -> str:
    """Pull the text out of a Puter chat answer."""
    message = payload.get("message")
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")


class PuterProvider(SubtitleProvider):
    """Puter's AI gateway for speech-to-text.

    Authenticates with ``PUTER_AUTH_TOKEN``, the token from the Puter account
    that pays for the usage. The answer carries per-word timings, which become
    YouTube-style caption lines. Puter has no text-translation API, so this
    provider produces the source-language track and the pipeline uses a
    separate provider (see ``SUBTITLE_TRANSLATION_PROVIDER``) for the targets.
    """

    name = "puter"
    chunk_seconds = PUTER_CHUNK_SECONDS
    supports_translation = True

    def __init__(self):
        token = (getattr(settings, "PUTER_AUTH_TOKEN", "") or "").strip()
        if not token:
            raise TranscriptionError(
                "PUTER_AUTH_TOKEN is not set, so subtitles cannot be generated."
            )
        self._token = token

    def transcribe(self, path: str, language: str) -> list[Segment]:
        args = {"provider": puter_stt_provider(), "model": puter_stt_model()}
        code = puter_language_code(language)
        if code:
            args["language"] = code
        if args["provider"] == "gemini":
            # Only Gemini asks for the per-word timings in this way; the
            # other back ends return them on their own.
            args["timestamp_granularities"] = ["word"]
        elif args["provider"] == "xai":
            args["diarize"] = True

        result = _puter_transcribe_request(self._token, path, args)
        return captions_from_puter_result(result)

    def translate(self, segments: list[Segment], target_language: str) -> list[Segment]:
        """Translate the caption lines through Puter's chat driver."""
        model = puter_translate_model()
        translated: list[Segment] = []

        for start in range(0, len(segments), TRANSLATE_BATCH):
            batch = segments[start : start + TRANSLATE_BATCH]
            texts = self._translate_batch(
                [segment.text for segment in batch], target_language, model
            )
            for segment, text in zip(batch, texts):
                if text:
                    translated.append(Segment(segment.start, segment.end, text))

        if len(translated) != len(segments):
            raise TranscriptionError(
                "The translation returned a different number of lines than expected."
            )
        return translated

    def _translate_batch(
        self, texts: list[str], target_language: str, model: str
    ) -> list[str]:
        numbered = "\n".join(
            f"{index}: {text}" for index, text in enumerate(texts)
        )
        prompt = (
            "Translate the following subtitle lines into "
            f"{target_language}. Keep the numbering exactly as it is and return "
            "only the translated lines, one per line, each starting with its "
            "number, with no extra commentary and no empty lines. Preserve the "
            "meaning, punctuation and line breaks within each numbered line.\n\n"
            + numbered
        )
        payload = _puter_chat_request(
            self._token,
            {
                "messages": [{"role": "user", "content": prompt}],
                "model": model,
                "temperature": 0,
            },
        )
        content = _puter_message_text(payload)
        mapping = self._parse_numbered(content, len(texts))

        if len(mapping) == len(texts):
            return [mapping[index] for index in range(len(texts))]

        if len(texts) > 1:
            # Fewer lines came back than were asked for. A short batch is much
            # easier for the model to keep in step, so the batch is halved and
            # each half is translated on its own.
            half = len(texts) // 2
            return self._translate_batch(
                texts[:half], target_language, model
            ) + self._translate_batch(texts[half:], target_language, model)

        # A single line could not be translated: keep the original so the
        # track stays complete rather than losing a caption.
        return [texts[0]]

    @staticmethod
    def _parse_numbered(content: str, expected: int) -> dict[int, str]:
        """Read back the numbered lines of a translation.

        The number in front of a line says which line it belongs to, so a
        model that drops or reorders one still lines up. A line without a
        number takes the next free place.
        """
        found: dict[int, str] = {}
        position = 0
        for raw_line in (content or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            index = None
            match = re.match(r"^(\d+)\s*[:.)]\s*(.*)$", line)
            if match:
                index = int(match.group(1))
                line = match.group(2).strip()
            if index is None or index >= expected or index in found:
                index = position
            if index < expected:
                found[index] = line
                position = index + 1
        return found


def provider_name() -> str:
    """The configured provider's short name."""
    return (getattr(settings, "SUBTITLE_PROVIDER", "openai") or "openai").strip().lower()


def get_provider() -> SubtitleProvider:
    """Build the provider named by ``SUBTITLE_PROVIDER``."""
    name = provider_name()
    if name == "openai":
        return OpenAIProvider()
    if name == "google":
        return GoogleProvider()
    if name == "elevenlabs":
        return ElevenLabsProvider()
    if name == "puter":
        return PuterProvider()
    if name == "assemblyai":
        return AssemblyAIProvider()
    raise TranscriptionError(f"Unknown subtitle provider: {name!r}")


def translation_provider_name() -> str:
    """The provider that translates the cues, or ``""`` for none.

    An empty value means "use the transcription provider when it is able to
    translate". ElevenLabs is not, so naming one here (``google`` or
    ``openai``) is what lets an ElevenLabs transcription still produce the
    French and English caption tracks.
    """
    return (
        getattr(settings, "SUBTITLE_TRANSLATION_PROVIDER", "") or ""
    ).strip().lower()


def get_translation_provider(
    provider: SubtitleProvider | None = None,
) -> SubtitleProvider | None:
    """Build the provider that translates the cues, or ``None``.

    ``None`` means no translation is possible, so only the source-language
    track is produced.
    """
    name = translation_provider_name()
    if not name:
        # No explicit choice: the transcription provider translates too when
        # it can.
        if provider is not None and provider.supports_translation:
            return provider
        return None

    if provider is not None and provider.name == name:
        return provider
    if name == "openai":
        return OpenAIProvider()
    if name == "google":
        return GoogleProvider()
    if name == "assemblyai":
        return AssemblyAIProvider()
    if name == "puter":
        return PuterProvider()
    raise TranscriptionError(f"Unknown translation provider: {name!r}")


def translation_configured() -> bool:
    """True when the cues can be translated into the target languages."""
    name = translation_provider_name()
    if name:
        if name == "openai":
            return bool(getattr(settings, "OPENAI_API_KEY", ""))
        if name == "google":
            return bool(getattr(settings, "GOOGLE_API_KEY", ""))
        if name == "assemblyai":
            return bool(getattr(settings, "ASSEMBLYAI_API_KEY", ""))
        if name == "puter":
            return bool(getattr(settings, "PUTER_AUTH_TOKEN", ""))
        return False
    # Without an explicit choice the transcription provider does the work.
    return provider_name() in ("openai", "google")


def is_configured() -> bool:
    """True when the configured provider has everything it needs to run."""
    name = provider_name()
    if name == "openai":
        return bool(getattr(settings, "OPENAI_API_KEY", ""))
    if name == "google":
        return bool(getattr(settings, "GOOGLE_API_KEY", ""))
    if name == "elevenlabs":
        return bool(getattr(settings, "ELEVENLABS_API_KEY", ""))
    if name == "puter":
        return bool(getattr(settings, "PUTER_AUTH_TOKEN", ""))
    if name == "assemblyai":
        return bool(getattr(settings, "ASSEMBLYAI_API_KEY", ""))
    return False


def configuration_hint() -> str:
    """The ``.env`` variable the configured provider needs to be switched on."""
    return {
        "google": "GOOGLE_API_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
        "puter": "PUTER_AUTH_TOKEN",
        "assemblyai": "ASSEMBLYAI_API_KEY",
    }.get(provider_name(), "OPENAI_API_KEY")


# --- Orchestration ----------------------------------------------------------


def generate_subtitles(video_id: int, include_lyrics: bool = True) -> dict:
    """Transcribe and translate a video, saving the result as subtitle tracks.

    Returns a summary dict with the languages that were created. Raises
    :class:`TranscriptionError` on any failure.
    """
    video = Video.objects.filter(id=video_id).first()
    if video is None:
        raise TranscriptionError("This video no longer exists.")

    source_language = getattr(settings, "SUBTITLE_SOURCE_LANGUAGE", "ar")
    targets = [
        code.strip()
        for code in getattr(settings, "SUBTITLE_TARGET_LANGUAGES", "fr,en").split(",")
        if code.strip()
    ]

    provider = get_provider()
    # ElevenLabs transcribes but cannot translate text, so a separate back end
    # may be named to produce the target-language tracks. When there is none,
    # only the source-language track is created.
    translator = get_translation_provider(provider)
    workdir = tempfile.mkdtemp(prefix="fazakkir-subs-")
    try:
        audio_path = download_audio(video, os.path.join(workdir, "audio.mp3"))
        chunks = chunk_audio(audio_path, workdir, provider.chunk_seconds)

        segments: list[Segment] = []
        for chunk_path, offset in chunks:
            for segment in provider.transcribe(chunk_path, source_language):
                segments.append(
                    Segment(
                        start=segment.start + offset,
                        end=segment.end + offset,
                        text=segment.text,
                    )
                )

        if not segments:
            raise TranscriptionError("No speech was detected in this video.")

        # A transcriber that barely hears a loud recording (singing, chanting,
        # a language it does not know) returns a handful of stray phrases. Those
        # are not captions, and saving them would look like a finished job, so
        # the run fails with a message the owner can act on instead.
        spoken = sum(max(0.0, segment.end - segment.start) for segment in segments)
        length = video.duration_seconds or 0
        if length >= MIN_COVERAGE_SECONDS and spoken < length * MIN_COVERAGE_RATIO:
            raise TranscriptionError(
                f"Only {spoken / max(length, 1):.0%} of this recording could be "
                "made out, which is too little to publish as captions. The "
                "transcriber probably does not handle this kind of recording "
                "(singing or chanting, for example). Try again with another "
                "transcription provider."
            )

        created: list[str] = []
        warnings: list[str] = []
        if source_language:
            _save_track(video, source_language, segments)
            created.append(source_language)

        for target in targets:
            if target == source_language:
                continue
            if translator is None:
                # No text-translation API is configured (ElevenLabs alone), so
                # the source-language track is all we can offer.
                logger.info(
                    "Skipping the %s track for video %s: no translation provider.",
                    target,
                    video_id,
                )
                continue
            try:
                # A provider that translates a whole transcript needs the
                # audio; one that translates cue by cue does not.
                translated = translator.translate_from_audio(
                    audio_path, source_language, target, segments
                )
                if not translated:
                    translated = translator.translate(segments, target)
            except TranscriptionError as exc:
                # The source-language track is already saved, so a translation
                # that runs out of credit or is rejected must not throw the
                # whole run away: the captions that do exist still work.
                logger.warning(
                    "Translation into %s failed for video %s: %s", target, video_id, exc
                )
                warnings.append(f"{target}: {exc}")
                continue
            _save_track(video, target, translated)
            created.append(target)

        if include_lyrics and not _has_lyrics(video):
            # Lyrics are a single full-length cue holding the transcription,
            # which is how they behave when uploaded by hand.
            _save_lyrics(video, segments)

        return {"languages": created, "cues": len(segments), "warnings": warnings}
    finally:
        _remove_tree(workdir)


def _save_track(video: Video, language_code: str, segments: list[Segment]) -> None:
    SubtitleTrack.objects.update_or_create(
        video=video,
        language_code=language_code,
        defaults={
            "label": label_for(language_code),
            "content": build_vtt(segments),
            "is_lyrics": False,
        },
    )


def _save_lyrics(video: Video, segments: list[Segment]) -> None:
    SubtitleTrack.objects.update_or_create(
        video=video,
        language_code="lyrics",
        defaults={
            "label": label_for("lyrics"),
            "content": build_vtt(segments),
            "is_lyrics": True,
        },
    )


def _has_lyrics(video: Video) -> bool:
    return video.subtitle_tracks.filter(language_code="lyrics").exists()


def _remove_tree(path: str) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def translate_existing_tracks(video_id: int, include_lyrics: bool = True) -> dict:
    """Add the missing language tracks to a video that is already transcribed.

    Speech-to-text is billed by the minute; translation costs a fraction of it.
    A video whose source track exists but whose French or English track does not
    (because the translator was unavailable at the time, for instance) is
    therefore completed from the transcript already stored, without listening to
    the audio again.

    Returns a summary dict with the languages that were created. Raises
    :class:`TranscriptionError` when there is nothing to translate or no
    translation provider is configured.
    """
    video = Video.objects.filter(id=video_id).first()
    if video is None:
        raise TranscriptionError("This video no longer exists.")

    source_language = getattr(settings, "SUBTITLE_SOURCE_LANGUAGE", "ar")
    targets = [
        code.strip()
        for code in getattr(settings, "SUBTITLE_TARGET_LANGUAGES", "fr,en").split(",")
        if code.strip()
    ]

    translator = get_translation_provider()
    if translator is None:
        raise TranscriptionError(
            "No translation provider is configured. Set "
            "SUBTITLE_TRANSLATION_PROVIDER and its API key first."
        )

    tracks = {track.language_code: track for track in video.subtitle_tracks.all()}
    usable = [code for code in tracks if code and code != "lyrics"]
    if not usable and "lyrics" not in tracks:
        raise TranscriptionError(
            "This video has no transcript to translate yet. Generate the "
            "captions first."
        )

    # A translator such as Google is told the source language only while
    # transcribing; it is set again below for whichever track we translate from.
    created: list[str] = []
    warnings: list[str] = []
    for target in targets:
        if target in tracks:
            continue

        translated: list[Segment] = []
        for origin in translation_origins(tracks, target, source_language):
            if target == origin.language_code:
                continue
            cues = parse_vtt(origin.content)
            if not cues:
                continue
            segments = [Segment(start, end, text) for start, end, text in cues]
            if hasattr(translator, "_source_language"):
                translator._source_language = origin.language_code
            try:
                attempt = translator.translate(segments, target)
            except TranscriptionError as exc:
                logger.warning(
                    "Translation into %s from %s failed for video %s: %s",
                    target,
                    origin.language_code,
                    video_id,
                    exc,
                )
                warnings.append(f"{target} from {origin.language_code}: {exc}")
                continue
            if _looks_untranslated(attempt):
                # Arabic came back where English was asked for, so the
                # translator could not read this track. Saving it would offer an
                # English track full of Arabic, so try the next one instead.
                logger.warning(
                    "Translating video %s from %s into %s came back untranslated; "
                    "trying another track.",
                    video_id,
                    origin.language_code,
                    target,
                )
                warnings.append(
                    f"{target} from {origin.language_code}: the translator could "
                    "not read this text, so it was left untranslated."
                )
                continue
            translated = attempt
            break

        if not translated:
            continue
        _save_track(video, target, translated)
        created.append(target)

    if include_lyrics and not _has_lyrics(video):
        best = translation_origins(tracks, "", source_language)
        for candidate in best:
            cues = parse_vtt(candidate.content)
            if cues:
                _save_lyrics(
                    video, [Segment(start, end, text) for start, end, text in cues]
                )
                break

    return {"languages": created, "warnings": warnings}


def count_arabic(content: str) -> int:
    """How many Arabic codepoints ``content`` holds (letters and diacritics)."""
    return sum(
        1
        for ch in content
        if ARABIC_RANGE[0] <= ord(ch) <= ARABIC_RANGE[1]
        or ARABIC_SUPPLEMENT_RANGE[0] <= ord(ch) <= ARABIC_SUPPLEMENT_RANGE[1]
    )


def translation_origins(
    tracks: dict, target: str, source_language: str
) -> list:
    """The tracks to translate ``target`` from, best first.

    A track in the configured source language is the natural choice, but Wolof
    is also written in Arabic script (Ajami) and a translation API cannot read
    that, so a track with Arabic in it is tried last. Another target-language
    track is a good source too: an English track can be translated from the
    French one, which is what happens when the source track is Ajami.
    """
    candidates = [
        track
        for code, track in tracks.items()
        if code and code != "lyrics" and code != target
    ]
    if not candidates:
        # Nothing but the lyrics track: it holds the same transcript.
        lyrics = tracks.get("lyrics")
        candidates = [lyrics] if lyrics is not None else []

    def rank(track):
        # A track the translator cannot read is worse than one in another
        # language, so the script is checked before the language.
        return (
            0 if count_arabic(track.content) < MIN_ARABIC_LETTERS else 1,
            0 if track.language_code == source_language else 1,
        )

    return sorted(candidates, key=rank)


def _looks_untranslated(translated: list[Segment]) -> bool:
    """True when a translation still holds text the translator cannot read.

    Cloud Translation answers with the text it was given when it cannot identify
    the language. Saving that would offer an "English" track full of Arabic, so
    it is treated as a failure and another track is tried.
    """
    text = " ".join(segment.text for segment in translated)
    if not text.strip():
        return True
    return count_arabic(text) >= MIN_ARABIC_LETTERS


# --- Background scheduling --------------------------------------------------

# Progress of a bulk subtitle run started from the dashboard. The database
# column is still the source of truth; this only drives the progress bar.
_SWEEP_STATE = {
    "running": False,
    "total": 0,
    "done": 0,
    "succeeded": 0,
    "failed": 0,
}
_SWEEP_LOCK = threading.Lock()


def eligible_subtitle_queryset():
    """Videos that can get subtitles: they need an MP3 to transcribe."""
    return Video.objects.exclude(audio_file_id="").exclude(
        auto_subtitle_status="processing"
    )


def audio_sweep_status() -> dict:
    """Return a snapshot of the bulk subtitle progress."""
    with _SWEEP_LOCK:
        return dict(_SWEEP_STATE)


def subtitle_progress() -> dict:
    """Count the caption work from the database.

    The in-process counters above only exist in the worker that started the run.
    These counters are visible to every worker, so the dashboard tells the truth
    whichever one answers the request.
    """
    videos = Video.objects.exclude(audio_file_id="")
    return {
        "total": videos.count(),
        "processing": videos.filter(auto_subtitle_status="processing").count(),
        "succeeded": videos.filter(auto_subtitle_status="ready").count(),
        "failed": videos.filter(auto_subtitle_status="failed").count(),
        "done": videos.filter(
            auto_subtitle_status__in=("ready", "failed")
        ).count(),
    }


def _run_for_video(video_id: int) -> None:
    close_old_connections()
    try:
        generate_and_record(video_id)
    finally:
        close_old_connections()


def schedule_subtitle_generation(video_id: int, retry: bool = False) -> bool:
    """Start generating a video's subtitles in a background thread.

    ``auto_subtitle_status`` doubles as a lock, so a video is never processed by
    two workers at once. With ``retry=False`` only untouched videos are queued.
    """
    queryset = Video.objects.filter(id=video_id).exclude(audio_file_id="").exclude(
        auto_subtitle_status="processing"
    )
    if not retry:
        queryset = queryset.filter(auto_subtitle_status="")

    if not queryset.update(
        auto_subtitle_status="processing", job_started_at=timezone.now()
    ):
        return False

    threading.Thread(target=_run_for_video, args=(video_id,), daemon=True).start()
    return True


def _out_of_credit(message: str) -> bool:
    """True when a failure means the account has no credit left.

    Running the rest of a bulk job would only repeat the same rejection, so the
    sweep stops instead and leaves the remaining videos untouched. They stay
    "not attempted", so the next run (or the monthly quota reset) picks them up.
    """
    text = (message or "").lower()
    return any(
        marker in text
        for marker in (
            "insufficient_funds",
            "not enough credits",
            "exceeds your quota",
            "quota",
            "credit",
            "billing",
        )
    )


def _sweep_videos() -> None:
    """Generate subtitles for every video that still needs them, one at a time."""
    # A video that already has captions is left alone: re-transcribing hours of
    # audio would only spend credit to replace a good track. A single video can
    # still be redone from its own button.
    pending = (
        eligible_subtitle_queryset()
        .exclude(auto_subtitle_status="ready")
        .order_by("id")
    )
    video_ids = list(pending.values_list("id", flat=True))
    with _SWEEP_LOCK:
        _SWEEP_STATE.update(
            running=True, total=len(video_ids), done=0, succeeded=0, failed=0
        )

    for video_id in video_ids:
        if not Video.objects.filter(id=video_id).exclude(audio_file_id="").exclude(
            auto_subtitle_status="processing"
        ).update(auto_subtitle_status="processing"):
            continue
        succeeded = generate_and_record(video_id)
        with _SWEEP_LOCK:
            _SWEEP_STATE["done"] += 1
            _SWEEP_STATE["succeeded" if succeeded else "failed"] += 1

        if not succeeded:
            error = (
                Video.objects.filter(id=video_id)
                .values_list("auto_subtitle_error", flat=True)
                .first()
                or ""
            )
            if _out_of_credit(error):
                logger.warning(
                    "Stopping the caption run: the account is out of credit (%s). "
                    "The remaining videos were left for the next run.",
                    error[:160],
                )
                break


def generate_and_record(video_id: int) -> bool:
    """Run the pipeline for one video and record the outcome. Returns success.

    Never raises: any failure is written to ``auto_subtitle_status`` /
    ``auto_subtitle_error`` so background workers and bulk runs stay alive.
    """
    try:
        summary = generate_subtitles(video_id)
    except TranscriptionError as exc:
        logger.warning("Subtitle generation failed for video %s: %s", video_id, exc)
        Video.objects.filter(id=video_id).update(
            auto_subtitle_status="failed",
            auto_subtitle_error=str(exc)[:500],
            job_started_at=None,
        )
        return False
    except Exception:  # noqa: BLE001 - keep the sweep going
        logger.exception("Unexpected subtitle failure for video %s", video_id)
        Video.objects.filter(id=video_id).update(
            auto_subtitle_status="failed",
            auto_subtitle_error="Unexpected error while generating subtitles.",
            job_started_at=None,
        )
        return False

    # A run that saved the source track but could not translate it is still a
    # success: the captions that exist work, and the warning says what did
    # not make it.
    warnings = summary.get("warnings") or []
    Video.objects.filter(id=video_id).update(
        auto_subtitle_status="ready",
        auto_subtitle_error="; ".join(warnings)[:500],
        job_started_at=None,
    )
    return True


def _run_sweep() -> None:
    close_old_connections()
    try:
        _sweep_videos()
    finally:
        with _SWEEP_LOCK:
            _SWEEP_STATE["running"] = False
        close_old_connections()


def start_subtitle_sweep() -> bool:
    """Kick off a background subtitle run over every eligible video.

    Returns ``False`` when a run is already in progress.
    """
    with _SWEEP_LOCK:
        if _SWEEP_STATE["running"]:
            return False
        _SWEEP_STATE["running"] = True

    threading.Thread(target=_run_sweep, daemon=True).start()
    return True