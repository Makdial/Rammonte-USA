import logging
from pathlib import Path

from django.contrib.auth.models import User
from django.db import models
from django.utils.translation import gettext_lazy as _

from . import r2_storage
from .subtitles import CAPTION_LANGUAGES, language_choices

logger = logging.getLogger(__name__)



class Video(models.Model):
    # ``user`` is the administrator who uploaded the video. Videos are public
    # and visible to every visitor regardless of who uploaded them.
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="videos")
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)

    # An audio-only upload (a recitation with no recording) has no video file
    # at all, so these two are blank for it and ``media_type`` says so.
    MEDIA_TYPE_CHOICES = [
        ("video", _("Video")),
        ("audio", _("Audio only")),
    ]
    media_type = models.CharField(
        max_length=10, choices=MEDIA_TYPE_CHOICES, default="video"
    )

    # ``file_id`` stores the Cloudflare R2 object path (used for deletion),
    # while ``video_url`` is the public URL used by the player.
    file_id = models.CharField(max_length=200, blank=True, default="")
    video_url = models.URLField(max_length=500, blank=True, default="")

    # Same idea for the optional/auto-generated thumbnail.
    thumbnail_file_id = models.CharField(max_length=200, blank=True, default="")
    thumbnail_url = models.URLField(max_length=500, blank=True)

    # Extracted MP3 audio track. Empty when the video has no audio stream.
    audio_file_id = models.CharField(max_length=200, blank=True, default="")
    audio_url = models.URLField(max_length=500, blank=True)

    # Video/audio duration in seconds. 0 means not computed yet.
    duration_seconds = models.FloatField(default=0.0, blank=True)

    # Status of the automatic MP3 extraction (see ``audio_tasks``).
    AUDIO_STATUS_CHOICES = [
        ("", _("Not generated")),
        ("processing", _("Generating")),
        ("ready", _("Ready")),
        ("none", _("No audio track")),
        ("failed", _("Failed")),
    ]
    audio_status = models.CharField(
        max_length=12, choices=AUDIO_STATUS_CHOICES, blank=True, default=""
    )
    audio_error = models.CharField(max_length=500, blank=True, default="")

    # Status of the automatic subtitle + translation run (see ``transcription``).
    AUTO_SUBTITLE_STATUS_CHOICES = [
        ("", _("Not generated")),
        ("processing", _("Generating")),
        ("ready", _("Ready")),
        ("failed", _("Failed")),
    ]
    auto_subtitle_status = models.CharField(
        max_length=12, choices=AUTO_SUBTITLE_STATUS_CHOICES, blank=True, default=""
    )
    auto_subtitle_error = models.CharField(max_length=500, blank=True, default="")

    # When the current MP3 / subtitle job started. A host that sleeps or
    # restarts can kill the worker mid-job, so a "processing" flag older than
    # the stale threshold is treated as abandoned and may be retried.
    job_started_at = models.DateTimeField(null=True, blank=True)

    views = models.PositiveIntegerField(default=0)
    likes = models.PositiveIntegerField(default=0)
    dislikes = models.PositiveIntegerField(default=0)

    # Manual position on the home page. Administrators drag the rows on the
    # dashboard's "Home order" page and each one is numbered from 1. Left at 0
    # it means "not arranged yet", so it sorts ahead of the arranged rows and
    # the newest upload still comes first on its own.
    position = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "-created_at"]

    def __str__(self):
        return self.title

    @property
    def playback_url(self):
        """A URL the browser can play: the public URL or a presigned link."""
        return r2_storage.resolve_url(self.file_id, self.video_url)

    @property
    def display_thumbnail_url(self):
        """Public or presigned thumbnail URL, or an empty string."""
        return r2_storage.resolve_url(self.thumbnail_file_id, self.thumbnail_url)

    @property
    def has_audio(self):
        """True when an MP3 audio track is available for this video."""
        return bool(self.audio_file_id)

    @property
    def is_audio_only(self):
        """True for an audio upload with no picture behind it."""
        return self.media_type == "audio" or not self.file_id

    @property
    def has_video(self):
        """True when there is a picture to play."""
        return not self.is_audio_only and bool(self.file_id)

    @property
    def audio_playback_url(self):
        """A URL the browser can play the MP3 from, or an empty string."""
        return r2_storage.resolve_url(self.audio_file_id, self.audio_url)

    @property
    def video_download_url(self):
        """A URL that downloads the video, or an empty string for audio-only."""
        if not self.file_id:
            return ""
        suffix = Path(self.file_id).suffix or ".mp4"
        return r2_storage.download_url(
            self.file_id, f"{self.title}{suffix}", self.video_url
        )

    @property
    def audio_download_url(self):
        """A URL that downloads the MP3, or an empty string when there is none."""
        if not self.audio_file_id:
            return ""
        return r2_storage.download_url(
            self.audio_file_id, f"{self.title}.mp3", self.audio_url
        )

    @property
    def duration_formatted(self):
        """Format duration as M:SS or H:MM:SS."""
        total = int(round(self.duration_seconds or 0))
        if total < 0:
            total = 0
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def delete_stored_files(self) -> None:
        """Best-effort removal of every R2 object that belongs to this video.

        Called whenever a video is deleted (directly, or because its owner's
        account was removed) so the bucket does not keep orphaned files.
        """
        for path in (self.thumbnail_file_id, self.audio_file_id, self.file_id):
            if not path:
                continue
            try:
                r2_storage.delete_file(path)
            except Exception:  # noqa: BLE001 - storage cleanup must never block a delete
                logger.warning("Could not remove R2 object %r", path, exc_info=True)


class SubtitleTrack(models.Model):
    """A subtitle or lyrics track attached to a video.

    The WebVTT text is stored in the database (these files are tiny) and served
    from Django so the browser never has to make a cross-origin request for it.
    """

    LANGUAGE_CHOICES = language_choices()

    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="subtitle_tracks")
    language_code = models.CharField(max_length=10, choices=LANGUAGE_CHOICES)
    label = models.CharField(max_length=50)
    content = models.TextField(help_text="WebVTT subtitle/lyrics text.")
    is_lyrics = models.BooleanField(default=False)
    is_enabled_by_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["language_code"]
        unique_together = ("video", "language_code")

    def __str__(self):
        return f"{self.video.title} - {self.label}"

    @property
    def display_label(self):
        """Translated label shown in the player UI."""
        from django.utils.translation import gettext

        # Translate the English name for known languages; anything else (a
        # caption language without a catalogue entry) falls back to the stored
        # label, which itself defaults to the code.
        name = CAPTION_LANGUAGES.get(self.language_code)
        if name is None:
            return self.label
        return gettext(name)


class VideoLike(models.Model):
    LIKE = 1
    DISLIKE = -1
    LIKE_CHOICES = [
        (LIKE, "Like"),
        (DISLIKE, "Dislike"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="user_likes")
    value = models.SmallIntegerField(choices=LIKE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["user", "video"]

    def __str__(self):
        action = "liked" if self.value == self.LIKE else "disliked"
        return f"{self.user.username} {action} {self.video.title}"


class Playlist(models.Model):
    """A named collection of videos owned by one user.

    Every signed-in account (administrators included) can keep playlists.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="playlists")
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_public = models.BooleanField(
        default=False,
        help_text=_("Public playlists can be viewed by anyone."),
    )

    # The picture the owner picked for the playlist. ``cover_file_id`` is the
    # R2 object path (needed to delete it), ``cover_url`` its public address.
    cover_file_id = models.CharField(max_length=200, blank=True, default="")
    cover_url = models.URLField(max_length=500, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title

    @property
    def video_count(self):
        return self.items.count()

    @property
    def display_cover_url(self):
        """Public or presigned cover URL, or an empty string."""
        return r2_storage.resolve_url(self.cover_file_id, self.cover_url)

    def contains(self, video) -> bool:
        return self.items.filter(video=video).exists()


class PlaylistItem(models.Model):
    """A single video inside a :class:`Playlist`."""

    playlist = models.ForeignKey(
        Playlist, on_delete=models.CASCADE, related_name="items"
    )
    video = models.ForeignKey(
        Video, on_delete=models.CASCADE, related_name="playlist_items"
    )
    position = models.PositiveIntegerField(default=0)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "added_at"]
        unique_together = ("playlist", "video")

    def __str__(self):
        return f"{self.playlist.title} - {self.video.title}"


class Comment(models.Model):
    """A viewer's comment on a video."""

    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} on {self.video.title}"

    def can_delete(self, user) -> bool:
        """Authors may delete their own comments; staff may delete any."""
        if not user or not user.is_authenticated:
            return False
        if self.user_id == user.id:
            return True
        from core.permissions import user_can_write

        return user_can_write(user)
