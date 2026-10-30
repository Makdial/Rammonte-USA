from django import forms
from django.utils.translation import gettext_lazy as _

MAX_VIDEO_SIZE = 3 * 1024 * 1024 * 1024  # 3 GB
MAX_IMAGE_SIZE = 8 * 1024 * 1024  # 8 MB
ALLOWED_VIDEO_TYPES = [
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
]
ALLOWED_IMAGE_TYPES = [
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
]


class VideoUploadForm(forms.Form):
    MEDIA_TYPE_CHOICES = [
        ("video", _("Video")),
        ("audio", _("Audio only (MP3)")),
    ]

    media_type = forms.ChoiceField(
        choices=MEDIA_TYPE_CHOICES,
        initial="video",
        label=_("Upload type"),
        widget=forms.RadioSelect,
    )

    title = forms.CharField(
        max_length=200,
        label=_("Title"),
        widget=forms.TextInput(
            attrs={
                "class": "form-input",
                "placeholder": _("Enter video title"),
            }
        ),
    )
    description = forms.CharField(
        required=False,
        label=_("Description"),
        widget=forms.Textarea(
            attrs={
                "class": "form-input",
                "placeholder": _("Enter video description"),
                "rows": 4,
            }
        ),
    )
    video_file = forms.FileField(
        label=_("Video File"),
        required=False,
        widget=forms.FileInput(attrs={
            "class": "form-input",
            "accept": "video/*",
        }),
        help_text=_("Required for a video upload. Leave empty for an audio-only upload."),
    )

    audio_file = forms.FileField(
        label=_("Audio File (MP3)"),
        required=False,
        widget=forms.FileInput(attrs={
            "class": "form-input",
            "accept": "audio/*",
        }),
        help_text=_("Required for an audio-only upload. Leave empty for a video upload, "
                    "where the MP3 is extracted automatically."),
    )

    def clean(self):
        cleaned = super().clean()
        media_type = cleaned.get("media_type")
        video_file = cleaned.get("video_file")
        audio_file = cleaned.get("audio_file")

        # The two uploads are alternatives, not a pair: a video brings its own
        # extracted MP3, and an audio upload has no picture at all. Accepting
        # both would silently throw one of them away.
        if video_file and audio_file:
            raise forms.ValidationError(
                _("Choose either a video or an MP3 file, not both.")
            )

        if media_type == "audio":
            if not audio_file:
                self.add_error(
                    "audio_file", _("Please choose an MP3 file for an audio upload.")
                )
        elif not video_file:
            self.add_error("video_file", _("Please choose a video file."))
        return cleaned

    def clean_video_file(self):
        video = self.cleaned_data.get("video_file")
        if video:
            if video.size > MAX_VIDEO_SIZE:
                raise forms.ValidationError(_("Video must be under 3 GB."))

            if video.content_type not in ALLOWED_VIDEO_TYPES:
                raise forms.ValidationError(_("This video type is not allowed."))

        return video


class VideoEditForm(forms.Form):
    """Lets an administrator change every editable field of an existing video.

    The file fields are optional on purpose: leaving them empty keeps whatever
    is already stored in R2, so a title fix never re-uploads hundreds of
    megabytes. Replacing the video also resets the derived media (MP3 and
    subtitles) because the old ones belong to the previous footage.
    """

    title = forms.CharField(
        max_length=120,
        label=_("Title"),
        widget=forms.TextInput(
            attrs={
                "class": "form-input",
                "placeholder": _("Enter video title"),
            }
        ),
    )
    description = forms.CharField(
        required=False,
        label=_("Description"),
        widget=forms.Textarea(
            attrs={
                "class": "form-input",
                "placeholder": _("Enter video description"),
                "rows": 5,
            }
        ),
    )
    thumbnail_file = forms.FileField(
        required=False,
        label=_("Replace thumbnail"),
        widget=forms.FileInput(
            attrs={"class": "form-input", "accept": "image/*"}
        ),
        help_text=_("Leave empty to keep the current thumbnail."),
    )
    video_file = forms.FileField(
        required=False,
        label=_("Replace video"),
        widget=forms.FileInput(
            attrs={"class": "form-input", "accept": "video/*"}
        ),
        help_text=_(
            "Leave empty to keep the current video. Replacing it deletes the "
            "old file, MP3 and subtitles, and rebuilds the MP3."
        ),
    )
    remove_thumbnail = forms.BooleanField(
        required=False,
        label=_("Remove the current thumbnail"),
        widget=forms.CheckboxInput(),
    )

    def clean_thumbnail_file(self):
        image = self.cleaned_data.get("thumbnail_file")
        if not image:
            return None

        if image.size > MAX_IMAGE_SIZE:
            raise forms.ValidationError(_("Image must be under 8 MB."))

        if image.content_type not in ALLOWED_IMAGE_TYPES:
            raise forms.ValidationError(_("This image type is not allowed."))

        return image

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("thumbnail_file") and cleaned.get("remove_thumbnail"):
            raise forms.ValidationError(
                _("Choose a new thumbnail or remove the current one, not both.")
            )
        return cleaned

    def clean_video_file(self):
        video = self.cleaned_data.get("video_file")
        if not video:
            return None

        if video.size > MAX_VIDEO_SIZE:
            raise forms.ValidationError(_("Video must be under 3 GB."))

        if video.content_type not in ALLOWED_VIDEO_TYPES:
            raise forms.ValidationError(_("This video type is not allowed."))

        return video
