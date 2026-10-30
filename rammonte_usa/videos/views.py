import base64
import binascii
import logging
import os
import re
import tempfile
from pathlib import Path

from urllib.parse import urlencode

from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from core.permissions import login_required, user_can_write, write_required

from .audio import (
    AudioExtractionError,
    duration_for_upload,
    extract_mp3,
    get_duration,
    materialize,
    prepare_mp3,
)
from .audio_tasks import clear_stale_jobs, schedule_audio_generation
from .duration_tasks import schedule_missing_durations
from .forms import VideoEditForm, VideoUploadForm
from .models import Comment, Playlist, PlaylistItem, SubtitleTrack, Video, VideoLike
from .r2_storage import (
    StorageError,
    delete_file,
    get_object,
    is_configured,
    is_image,
    is_object_path,
    upload_audio,
    upload_thumbnail,
    upload_video,
)
from .subtitles import ALLOWED_SUBTITLE_EXTENSIONS, language_choices, label_for, normalize_to_vtt
from .transcription import (
    configuration_hint as subtitles_key_hint,
)
from .transcription import (
    is_configured as subtitles_configured,
)
from .transcription import (
    schedule_subtitle_generation,
)

logger = logging.getLogger(__name__)

# Where a viewer is sent when the library has nothing that matches. The same
# link works from every country and needs no API key: it is simply YouTube's
# own results page for the words the visitor typed.
YOUTUBE_SEARCH_URL = "https://www.youtube.com/results?"


def video_detail(request, video_id):
    video = get_object_or_404(Video.objects, id=video_id)

    if not video.duration_seconds:
        schedule_missing_durations(Video.objects.filter(id=video.id))

    video.views += 1
    video.save(update_fields=["views"])

    # Record a view for the daily usage dashboard.
    try:
        from dashboard.models import ActivityLog

        ActivityLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            video=video,
            kind=ActivityLog.KIND_VIEW,
        )
    except Exception:
        logger.debug("Could not record view activity", exc_info=True)

    # Play counts as "please give me the audio": if this video has no MP3 yet,
    # generate one in the background so the Listen / Download MP3 options show
    # up without the administrator having to do anything.
    if not video.has_audio and video.audio_status == "":
        schedule_audio_generation(video.id)

    audio_pending = not video.has_audio and video.audio_status in ("", "processing")
    audio_failed = not video.has_audio and video.audio_status == "failed"
    audio_none = not video.has_audio and video.audio_status == "none"

    user_vote = None
    if request.user.is_authenticated:
        like = VideoLike.objects.filter(user=request.user, video=video).first()
        if like:
            user_vote = like.value

    comments = video.comments.select_related("user").all()

    # "Up next": the most watched other recordings, which is what YouTube shows
    # down the right-hand side. Excluded from the query rather than filtered in
    # the template so the current video cannot appear in its own sidebar.
    related_videos = list(
        Video.objects.exclude(id=video.id)
        .select_related("user")
        .order_by("-views", "-created_at")[:12]
    )

    user_playlists = []
    playlist_ids_with_video = set()
    if request.user.is_authenticated:
        user_playlists = list(
            request.user.playlists.annotate(item_count=Count("items"))
        )
        playlist_ids_with_video = set(
            PlaylistItem.objects.filter(
                playlist__user=request.user, video=video
            ).values_list("playlist_id", flat=True)
        )

    # Absolute links a visitor can send to a friend. The MP3 is shared through
    # our own audio-stream route rather than the presigned download URL, because
    # a presigned link expires after 12 hours and would be dead on arrival.
    share_video_url = request.build_absolute_uri(
        reverse("videos:detail", args=[video.id])
    )
    share_audio_url = ""
    if video.has_audio:
        share_audio_url = request.build_absolute_uri(
            reverse("videos:audio_stream", args=[video.id])
        )

    return render(
        request,
        "videos/detail.html",
        {
            "video": video,
            "user_vote": user_vote,
            "comments": comments,
            "related_videos": related_videos,
            "comment_count": comments.count(),
            "user_playlists": user_playlists,
            "playlist_ids_with_video": playlist_ids_with_video,
            "audio_pending": audio_pending,
            "audio_failed": audio_failed,
            "audio_none": audio_none,
            "share_video_url": share_video_url,
            "share_audio_url": share_audio_url,
            "is_audio_only": video.is_audio_only,
            "subtitles_configured": subtitles_configured(),
            "subtitles_pending": video.auto_subtitle_status
            in ("", "processing"),
            "subtitles_failed": video.auto_subtitle_status == "failed",
            "subtitle_tracks": video.subtitle_tracks.all(),
            "subtitle_choices": [(code, _(name)) for code, name in language_choices()],
        },
    )


def video_list(request):
    videos = Video.objects.all()
    # Old uploads have no length yet; measure the ones on this page in the
    # background so the badge fills in without the administrator doing anything.
    schedule_missing_durations(videos)
    return render(request, "videos/list.html", {"videos": videos})


def video_search(request):
    """Search videos by any word in the title or description, the uploader's
    name, or the year they were uploaded.

    Typing a bare year such as "2000" finds everything uploaded that year even
    though no field stores the year as text. Each word is matched separately so
    "mame 2000" narrows to the 2000 recordings by that uploader rather than
    looking for the literal phrase.
    """
    query = (request.GET.get("q") or "").strip()
    words = [w for w in re.split(r"\s+", query) if w]

    # With nothing typed there is no search to run, so show no results rather
    # than quietly dumping the whole library under an empty search box.
    videos = Video.objects.none() if not words else Video.objects.select_related("user")

    for word in words:
        clause = (
            Q(title__icontains=word)
            | Q(description__icontains=word)
            | Q(user__username__icontains=word)
        )
        # A four-digit run inside a plausible range is treated as an upload
        # year; anything else (a title that happens to contain digits) is left
        # to the text search above.
        if word.isdigit() and len(word) == 4:
            year = int(word)
            if 1900 <= year <= 2200:
                clause = clause | Q(created_at__year=year)
        videos = videos.filter(clause)

    videos = videos.distinct()
    schedule_missing_durations(videos)

    # When the library has nothing to offer, hand the visitor off to YouTube
    # rather than leaving them at a dead end. The query is passed through
    # verbatim so the search they already typed is the search that runs there.
    youtube_url = ""
    if query:
        youtube_url = YOUTUBE_SEARCH_URL + urlencode({"search_query": query})

    return render(
        request,
        "videos/search.html",
        {
            "videos": videos,
            "query": query,
            "words": words,
            "youtube_url": youtube_url,
        },
    )


def channel_videos(request, username):
    videos = Video.objects.filter(user__username=username)
    schedule_missing_durations(videos)
    return render(request, "videos/channel.html", {"videos": videos, "channel_name": username})


def offline_library(request):
    """A client-rendered list of the MP3s this device saved for offline use."""
    return render(request, "videos/offline.html")


# Chunk size used while streaming the MP3 back from R2.
AUDIO_CHUNK_SIZE = 64 * 1024


@require_GET
def audio_stream(request, video_id):
    """Stream a video's MP3 from R2 through this same origin.

    Serving the audio from our own domain (instead of the expiring, cross-origin
    R2 URL) lets the browser seek normally and lets the offline service worker
    cache the file, so it can be replayed without a connection.
    """
    video = get_object_or_404(Video.objects, id=video_id)
    if not video.audio_file_id:
        raise Http404("This video has no audio track.")

    # Legacy records keep their previously stored (external) URL.
    if not is_object_path(video.audio_file_id):
        if video.audio_url:
            return redirect(video.audio_url)
        raise Http404("This video has no audio track.")

    range_header = request.headers.get("Range")
    # Record one audio play per initial request (skip range/seek follow-ups).
    if not range_header or range_header.strip().lower() in ("bytes=0-", "bytes=0-0"):
        try:
            from dashboard.models import ActivityLog

            ActivityLog.objects.create(
                user=request.user if request.user.is_authenticated else None,
                video=video,
                kind=ActivityLog.KIND_AUDIO,
            )
        except Exception:
            logger.debug("Could not record audio activity", exc_info=True)
    try:
        obj = get_object(range_header=range_header, path=video.audio_file_id)
    except StorageError:
        # Fall back to a presigned URL so playback still works if the direct
        # stream fails (for example when the R2 credentials are wrong).
        fallback = video.audio_playback_url
        if fallback:
            return redirect(fallback)
        raise Http404("Audio is not available right now.")

    body = obj.get("Body")
    content_range = obj.get("ContentRange")
    length = obj.get("ContentLength")

    def chunks():
        try:
            for chunk in body.iter_chunks(chunk_size=AUDIO_CHUNK_SIZE):
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    response = StreamingHttpResponse(
        chunks(),
        status=206 if content_range else 200,
        content_type=obj.get("ContentType") or "audio/mpeg",
    )
    response["Accept-Ranges"] = "bytes"
    if length is not None:
        response["Content-Length"] = str(length)
    if content_range:
        response["Content-Range"] = content_range
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_GET
def audio_status(request, video_id):
    """JSON endpoint the video page polls while an MP3 is being generated."""
    # A sleeping host can kill the worker; release any job it abandoned.
    clear_stale_jobs()
    video = get_object_or_404(Video.objects, id=video_id)
    return JsonResponse(
        {
            "has_audio": video.has_audio,
            "status": video.audio_status,
            "pending": not video.has_audio
            and video.audio_status in ("", "processing"),
            "audio_url": reverse("videos:audio_stream", args=[video.id])
            if video.has_audio
            else "",
            "download_url": video.audio_download_url if video.has_audio else "",
        }
    )


@require_POST
def audio_generate(request, video_id):
    """Retry MP3 generation for a video whose first attempt failed."""
    video = get_object_or_404(Video.objects, id=video_id)
    if video.has_audio:
        return JsonResponse({"success": True, "started": False, "status": "ready"})
    started = schedule_audio_generation(video.id, retry=True)
    return JsonResponse(
        {"success": True, "started": started, "status": "processing"}
    )


@write_required
def video_upload_page(request):
    return render(
        request,
        "videos/upload.html",
        {"form": VideoUploadForm(), "storage_ready": is_configured()},
    )


# The cover travels either as a streamed file part (preferred) or, from an older
# page, as a base64 data URL inside a normal form field. A phone photo is many
# megabytes, and Django refuses a request whose fields are too large before any
# view runs, which used to look like an unexplained failure.
MAX_THUMBNAIL_CHARS = 4 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024  # the same limit the image form allows


def _thumbnail_payload(request) -> bytes | str:
    """Return the chosen cover, or "" when none was chosen.

    Raises ``ValueError`` with a message the uploader can act on. A file part is
    preferred; the base64 field is still read so an older page keeps working.
    """
    upload = request.FILES.get("thumbnail_file")
    if upload is not None:
        if upload.size > MAX_THUMBNAIL_BYTES:
            raise ValueError(_("That picture is too large. Please choose a smaller one."))
        raw = upload.read()
        if not raw or not is_image(raw):
            raise ValueError(
                _("That file is not a picture the site can show. Please choose a JPEG or PNG image.")
            )
        return raw

    data = (request.POST.get("thumbnail_data") or "").strip()
    if not data:
        return ""
    if not data.startswith("data:image"):
        raise ValueError(_("That thumbnail is not a picture. Please choose an image."))
    if len(data) > MAX_THUMBNAIL_CHARS:
        raise ValueError(_("That picture is too large. Please choose a smaller one."))

    encoded = data.split(",", 1)[1] if "," in data else ""
    try:
        raw = base64.b64decode(encoded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            _("That picture could not be read. Please choose another one.")
        ) from exc

    if not raw or not is_image(raw):
        raise ValueError(
            _("That file is not a picture the site can show. Please choose a JPEG or PNG image.")
        )
    return raw


def _store_thumbnail(thumbnail: bytes | str, base_name: str) -> tuple[str, str, str]:
    """Store an already validated cover.

    Returns ``(file_id, url, warning)``. A cover that cannot be stored must not
    throw away the media that was just uploaded, so the failure comes back as a
    warning the page can show instead of an error.
    """
    if not thumbnail:
        return "", "", ""
    try:
        thumb = upload_thumbnail(
            file_data=thumbnail, file_name=base_name + "_thumb.jpg"
        )
    except StorageError:
        logger.warning(
            "Storing the chosen thumbnail failed for %r", base_name, exc_info=True
        )
        return "", "", _("The file was saved, but its picture could not be stored.")
    return thumb["path"], thumb["url"], ""


@write_required
@require_POST
def video_upload(request):
    form = VideoUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        errors = []
        for field, field_errors in form.errors.items():
            for error in field_errors:
                errors.append(error if field == "__all__" else f"{field}: {error}")
        return JsonResponse({"success": False, "errors": "; ".join(errors)})

    video_file = form.cleaned_data.get("video_file")
    audio_file = form.cleaned_data.get("audio_file")
    media_type = form.cleaned_data["media_type"]

    # Checked first, so a bad cover cannot reject an upload that already spent
    # minutes on the wire.
    try:
        thumbnail = _thumbnail_payload(request)
    except ValueError as exc:
        return JsonResponse({"success": False, "error": str(exc)})

    # Audio-only upload: store the MP3 itself, with no picture behind it.
    if media_type == "audio":
        base_name = Path(audio_file.name or "").stem or "audio"
        try:
            # An MP3 is kept as it is; anything else is converted, because the
            # site only ever streams and offers MP3.
            mp3_path, removals = prepare_mp3(audio_file, base_name)
        except AudioExtractionError as exc:
            return JsonResponse({"success": False, "error": str(exc)})

        try:
            duration = get_duration(mp3_path)
            with open(mp3_path, "rb") as handle:
                result = upload_audio(handle, base_name + ".mp3")
        except (StorageError, OSError) as exc:
            return JsonResponse({"success": False, "error": str(exc)})
        finally:
            for path in removals:
                _remove_path(path)

        # An audio upload has no frame to sample, so the picture is the one
        # the owner chose on the upload page (if any).
        thumbnail_file_id, thumbnail_url, warning = _store_thumbnail(
            thumbnail, base_name
        )

        video = Video.objects.create(
            user=request.user,
            title=form.cleaned_data["title"],
            description=form.cleaned_data["description"],
            media_type="audio",
            file_id="",
            video_url="",
            thumbnail_file_id=thumbnail_file_id,
            thumbnail_url=thumbnail_url,
            audio_file_id=result["path"],
            audio_url=result["url"],
            audio_status="ready",
            duration_seconds=duration,
        )
        _notify_storage_after_upload()
        payload = {
            "success": True,
            "video_id": video.id,
            "message": _("Audio uploaded successfully"),
        }
        if warning:
            payload["warning"] = warning
        return JsonResponse(payload)

    # Extract the audio first: uploading the video to R2 closes the temporary
    # file Django saved the upload to, so it is gone afterwards. The duration is
    # read at the same time, for the same reason.
    duration = duration_for_upload(video_file)
    audio_file_id, audio_url, audio_status = _extract_audio_track(
        video_file, video_file.name
    )

    try:
        video_file.seek(0)
        result = upload_video(file_data=video_file, file_name=video_file.name)
    except StorageError as exc:
        # Do not leave the MP3 orphaned if the video itself could not be stored.
        if audio_file_id:
            try:
                delete_file(audio_file_id)
            except StorageError:
                pass
        return JsonResponse({"success": False, "error": str(exc)})

    # Stored after the video, and never fatal: the footage is what matters.
    thumbnail_file_id, thumbnail_url, warning = _store_thumbnail(
        thumbnail, video_file.name.rsplit(".", 1)[0]
    )

    video = Video.objects.create(
        user=request.user,
        title=form.cleaned_data["title"],
        description=form.cleaned_data["description"],
        media_type="video",
        file_id=result["path"],
        video_url=result["url"],
        thumbnail_file_id=thumbnail_file_id,
        thumbnail_url=thumbnail_url,
        audio_file_id=audio_file_id,
        audio_url=audio_url,
        audio_status=audio_status,
        duration_seconds=duration,
    )

    _notify_storage_after_upload()

    payload = {
        "success": True,
        "video_id": video.id,
        "message": _("Video uploaded successfully"),
    }
    if warning:
        payload["warning"] = warning
    return JsonResponse(payload)


def _notify_storage_after_upload() -> None:
    """Warn the administrator when the storage is getting full.

    Imported lazily to avoid an import cycle, and never allowed to break the
    upload itself.
    """
    try:
        from dashboard.storage import check_and_notify

        check_and_notify()
    except Exception:  # noqa: BLE001 - a storage check must not fail an upload
        logger.warning("Storage check after upload failed", exc_info=True)


def _extract_audio_track(uploaded_file, original_name: str) -> tuple[str, str, str]:
    """Best-effort extraction of a video's audio track as an R2-hosted MP3.

    Returns ``(audio_file_id, audio_url, audio_status)``. On success the status
    is ``"ready"``; when the video has no audio stream it is ``"none"`` (so the
    video page will not keep retrying); on a transient failure it is ``""`` (so
    the next page view retries).
    """
    try:
        source_path, cleanup_source = materialize(uploaded_file)
    except Exception:
        logger.warning("Could not read the upload for MP3 extraction", exc_info=True)
        return "", "", ""

    mp3_path = ""
    try:
        base_name = Path(original_name or "").stem or "audio"
        handle_fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
        os.close(handle_fd)

        if not extract_mp3(source_path, mp3_path):
            logger.info("Video %r has no audio track; skipping MP3.", original_name)
            return "", "", "none"

        with open(mp3_path, "rb") as mp3_file:
            result = upload_audio(mp3_file, base_name + ".mp3")
        return result["path"], result["url"], "ready"
    except (AudioExtractionError, StorageError, OSError):
        logger.warning("MP3 extraction failed for %r", original_name, exc_info=True)
        return "", "", ""
    finally:
        _remove_path(mp3_path)
        _remove_path(cleanup_source)


def _remove_path(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


@write_required
def video_edit(request, video_id):
    """Edit every editable field of an existing video.

    Text fields are always saved. The thumbnail and the video file are only
    touched when a new file is supplied, so correcting a typo never re-uploads
    a large file. Replacing the video discards the derived media (MP3 and
    subtitles) because they describe the footage that was just removed.
    """
    video = get_object_or_404(Video, id=video_id)

    if request.method == "POST":
        form = VideoEditForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(
                request,
                "videos/edit.html",
                {"video": video, "form": form},
                status=400,
            )

        video.title = form.cleaned_data["title"].strip()
        video.description = form.cleaned_data["description"]

        new_thumbnail = form.cleaned_data.get("thumbnail_file")
        new_video = form.cleaned_data.get("video_file")
        remove_thumbnail = form.cleaned_data.get("remove_thumbnail")

        if new_video:
            # Measure the duration before the upload closes the file.
            new_duration = duration_for_upload(new_video)
            audio_file_id, audio_url, audio_status = _extract_audio_track(
                new_video, new_video.name
            )
            try:
                new_video.seek(0)
                result = upload_video(
                    file_data=new_video, file_name=new_video.name
                )
            except StorageError as exc:
                # The MP3 just built from the new file would otherwise be left
                # orphaned in the bucket.
                if audio_file_id:
                    try:
                        delete_file(audio_file_id)
                    except StorageError:
                        pass
                form.add_error("video_file", str(exc))
                return render(
                    request,
                    "videos/edit.html",
                    {"video": video, "form": form},
                    status=400,
                )

            # Everything derived from the old footage is now wrong.
            delete_file(video.file_id)
            delete_file(video.audio_file_id)
            video.subtitle_tracks.all().delete()

            video.file_id = result["path"]
            video.video_url = result["url"]
            video.audio_file_id = audio_file_id
            video.audio_url = audio_url
            video.audio_status = audio_status
            video.audio_error = ""
            video.auto_subtitle_status = ""
            video.auto_subtitle_error = ""
            video.job_started_at = None
            video.duration_seconds = new_duration
            video.save()

            if not audio_file_id and audio_status == "":
                schedule_audio_generation(video.id)
        elif new_thumbnail:
            try:
                thumb = upload_thumbnail(
                    file_data=new_thumbnail.read(),
                    file_name=new_thumbnail.name or "thumbnail.jpg",
                )
            except StorageError as exc:
                form.add_error("thumbnail_file", str(exc))
                return render(
                    request,
                    "videos/edit.html",
                    {"video": video, "form": form},
                    status=400,
                )

            old_thumbnail = video.thumbnail_file_id
            video.thumbnail_file_id = thumb["path"]
            video.thumbnail_url = thumb["url"]
            video.save()
            if old_thumbnail:
                delete_file(old_thumbnail)
        elif remove_thumbnail:
            old_thumbnail = video.thumbnail_file_id
            video.thumbnail_file_id = ""
            video.thumbnail_url = ""
            video.save()
            if old_thumbnail:
                delete_file(old_thumbnail)
        else:
            video.save()

        return redirect(f"{reverse('videos:detail', args=[video.id])}?saved=1")

    return render(
        request,
        "videos/edit.html",
        {
            "video": video,
            "form": VideoEditForm(
                initial={"title": video.title, "description": video.description}
            ),
        },
    )


@write_required
@require_POST
def delete_video(request, video_id):
    video = get_object_or_404(Video, id=video_id)

    # Remove the R2 objects first; the database row (and, via CASCADE, its
    # comments, subtitle tracks and playlist entries) is deleted right after.
    video.delete_stored_files()
    video.delete()

    return JsonResponse({"success": True, "message": _("Video deleted")})


@require_POST
def video_vote(request, video_id):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": _("You must be signed in to vote.")},
            status=403,
        )

    video = get_object_or_404(Video, id=video_id)
    vote_type = request.POST.get("vote")

    if vote_type not in ["like", "dislike"]:
        return JsonResponse({"success": False, "error": _("Invalid vote")}, status=400)

    value = VideoLike.LIKE if vote_type == "like" else VideoLike.DISLIKE

    existing_vote = VideoLike.objects.filter(user=request.user, video=video).first()

    if existing_vote:
        if existing_vote.value == value:
            if value == VideoLike.LIKE:
                video.likes -= 1
            else:
                video.dislikes -= 1
            existing_vote.delete()
            user_vote = None
        else:
            if value == VideoLike.LIKE:
                video.likes += 1
                video.dislikes -= 1
            else:
                video.likes -= 1
                video.dislikes += 1
            existing_vote.value = value
            existing_vote.save()
            user_vote = value
    else:
        VideoLike.objects.create(user=request.user, video=video, value=value)
        if value == VideoLike.LIKE:
            video.likes += 1
        else:
            video.dislikes += 1
        user_vote = value

    video.save(update_fields=["likes", "dislikes"])

    return JsonResponse({
        "likes": video.likes,
        "dislikes": video.dislikes,
        "user_vote": user_vote,
    })


# --- Subtitles / lyrics ----------------------------------------------------

def subtitle_file(request, video_id, language_code):
    """Serve a stored WebVTT track from Django (always same-origin)."""
    track = get_object_or_404(
        SubtitleTrack, video_id=video_id, language_code=language_code
    )
    response = HttpResponse(track.content, content_type="text/vtt; charset=utf-8")
    response["Cache-Control"] = "public, max-age=3600"
    return response


@write_required
@require_POST
def subtitle_upload(request, video_id):
    video = get_object_or_404(Video, id=video_id)

    language_code = (request.POST.get("language_code") or "").strip()
    if language_code not in dict(SubtitleTrack.LANGUAGE_CHOICES):
        return JsonResponse(
            {"success": False, "error": _("Choose a valid language.")}, status=400
        )

    upload = request.FILES.get("subtitle_file")
    if not upload:
        return JsonResponse(
            {"success": False, "error": _("Please choose a subtitle file.")},
            status=400,
        )

    if Path(upload.name).suffix.lower() not in ALLOWED_SUBTITLE_EXTENSIONS:
        return JsonResponse(
            {"success": False, "error": _("Only .vtt, .srt or .txt files are supported.")},
            status=400,
        )

    try:
        content = normalize_to_vtt(upload.read())
    except ValueError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    SubtitleTrack.objects.update_or_create(
        video=video,
        language_code=language_code,
        defaults={
            "label": label_for(language_code),
            "content": content,
            "is_lyrics": language_code == "lyrics",
        },
    )

    return JsonResponse({"success": True, "message": _("Subtitle saved")})


@write_required
@require_POST
def subtitle_delete(request, video_id, language_code):
    track = get_object_or_404(
        SubtitleTrack, video_id=video_id, language_code=language_code
    )
    track.delete()
    return JsonResponse({"success": True, "message": _("Subtitle removed")})


@write_required
@require_POST
def subtitles_generate(request, video_id):
    """Start generating subtitles for a video without uploading anything."""
    video = get_object_or_404(Video, id=video_id)

    if not subtitles_configured():
        return JsonResponse(
            {
                "success": False,
                "error": _(
                    "Automatic subtitles are not configured. "
                    "Set %(key)s in the .env file to enable them."
                )
                % {"key": subtitles_key_hint()},
            },
            status=400,
        )

    if not video.has_audio:
        return JsonResponse(
            {
                "success": False,
                "error": _(
                    "This video has no audio track yet. "
                    "Generate its MP3 first, then create the subtitles."
                ),
            },
            status=400,
        )

    started = schedule_subtitle_generation(video.id, retry=True)
    return JsonResponse(
        {
            "success": True,
            "started": started,
            "message": _("Generating subtitles in the background.")
            if started
            else _("A subtitle run is already in progress for this video."),
        }
    )


@require_GET
def subtitles_status(request, video_id):
    """Report the state of the automatic subtitle run for a video."""
    clear_stale_jobs()
    video = get_object_or_404(Video, id=video_id)
    return JsonResponse(
        {
            "status": video.auto_subtitle_status,
            "error": video.auto_subtitle_error,
            "pending": video.auto_subtitle_status in ("", "processing"),
            "languages": [
                track.language_code for track in video.subtitle_tracks.all()
            ],
        }
    )


# --- Comments --------------------------------------------------------------

def _can_moderate(user) -> bool:
    return user_can_write(user)


@require_POST
def comment_add(request, video_id):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": _("You must be signed in to comment.")},
            status=403,
        )

    video = get_object_or_404(Video, id=video_id)
    body = (request.POST.get("body") or "").strip()

    if not body:
        return JsonResponse(
            {"success": False, "error": _("Write something first.")}, status=400
        )

    comment = Comment.objects.create(video=video, user=request.user, body=body[:2000])

    display_name = comment.user.first_name or comment.user.username
    return JsonResponse(
        {
            "success": True,
            "comment": {
                "id": comment.id,
                "user": display_name,
                "initial": display_name[:1].upper(),
                "body": comment.body,
                "created": comment.created_at.strftime("%b %d, %Y %H:%M"),
                "can_delete": True,
            },
            "comment_count": video.comments.count(),
        }
    )


@require_POST
def comment_delete(request, comment_id):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": _("You must be signed in.")}, status=403
        )

    comment = get_object_or_404(Comment.objects.select_related("video"), id=comment_id)
    if not comment.can_delete(request.user):
        return JsonResponse(
            {"success": False, "error": _("You cannot delete this comment.")},
            status=403,
        )

    video = comment.video
    comment.delete()

    return JsonResponse({"success": True, "comment_count": video.comments.count()})


# --- Playlists -------------------------------------------------------------

@login_required
def playlist_list(request):
    playlists = (
        request.user.playlists.annotate(item_count=Count("items"))
        .prefetch_related("items__video")
    )
    return render(request, "videos/playlists.html", {"playlists": playlists})


@login_required
@require_POST
def playlist_create(request):
    title = (request.POST.get("title") or "").strip()
    if not title:
        return JsonResponse(
            {"success": False, "error": _("Give your playlist a name.")}, status=400
        )

    playlist = Playlist.objects.create(
        user=request.user,
        title=title[:120],
        description=(request.POST.get("description") or "").strip(),
        is_public=request.POST.get("is_public") == "1",
    )

    video_id = request.POST.get("video_id")
    if video_id:
        video = Video.objects.filter(id=video_id).first()
        if video:
            PlaylistItem.objects.get_or_create(playlist=playlist, video=video)

    # A picture chosen while creating the playlist becomes its cover.
    cover_error = _store_playlist_cover(playlist, request.POST.get("cover_data", ""))

    return JsonResponse(
        {
            "success": True,
            "playlist": {
                "id": playlist.id,
                "title": playlist.title,
                "url": reverse("videos:playlist_detail", args=[playlist.id]),
            },
            "cover_error": cover_error,
        }
    )


def _get_owned_playlist(request, playlist_id):
    """Return the playlist if it belongs to the current user, else None."""
    return Playlist.objects.filter(id=playlist_id, user=request.user).first()


def _store_playlist_cover(playlist, cover_data):
    """Upload a ``data:image`` picture and make it the playlist's cover.

    Returns an error message when the upload fails, or an empty string on
    success. The picture being replaced is removed from the bucket so covers
    do not pile up every time the owner changes their mind.
    """
    if not (cover_data or "").startswith("data:image"):
        return ""

    try:
        result = upload_thumbnail(
            file_data=cover_data,
            file_name=f"playlist_{playlist.id}.jpg",
            folder="playlist-covers",
        )
    except StorageError as exc:
        return str(exc)

    previous = playlist.cover_file_id
    playlist.cover_file_id = result["path"]
    playlist.cover_url = result["url"]
    playlist.save(update_fields=["cover_file_id", "cover_url"])

    if previous and previous != result["path"]:
        try:
            delete_file(previous)
        except StorageError:
            pass
    return ""


@login_required
def playlist_detail(request, playlist_id):
    playlist = Playlist.objects.filter(id=playlist_id).first()
    if playlist is None:
        return render(request, "videos/playlist_not_found.html", status=404)

    is_owner = playlist.user_id == request.user.id
    if not playlist.is_public and not is_owner and not _can_moderate(request.user):
        return render(request, "videos/playlist_not_found.html", status=403)

    items = playlist.items.select_related("video", "video__user")
    return render(
        request,
        "videos/playlist_detail.html",
        {"playlist": playlist, "items": items, "is_owner": is_owner},
    )


@login_required
@require_POST
def playlist_delete(request, playlist_id):
    playlist = _get_owned_playlist(request, playlist_id)
    if playlist is None:
        return JsonResponse(
            {"success": False, "error": _("Playlist not found.")}, status=404
        )
    if playlist.cover_file_id:
        try:
            delete_file(playlist.cover_file_id)
        except StorageError:
            pass
    playlist.delete()
    return JsonResponse({"success": True, "message": _("Playlist deleted")})


@login_required
@require_POST
def playlist_update(request, playlist_id):
    playlist = _get_owned_playlist(request, playlist_id)
    if playlist is None:
        return JsonResponse(
            {"success": False, "error": _("Playlist not found.")}, status=404
        )

    cover_data = request.POST.get("cover_data", "")
    error = _store_playlist_cover(playlist, cover_data)
    if error:
        return JsonResponse({"success": False, "error": error})

    title = (request.POST.get("title") or "").strip()
    if title:
        playlist.title = title[:120]
    if "description" in request.POST:
        playlist.description = (request.POST.get("description") or "").strip()
    if "is_public" in request.POST:
        playlist.is_public = request.POST.get("is_public") == "1"
    playlist.save()

    return JsonResponse({"success": True, "message": _("Playlist updated")})


@login_required
@require_POST
def playlist_toggle_video(request, playlist_id):
    """Add the video to the playlist, or remove it when it is already in."""
    playlist = _get_owned_playlist(request, playlist_id)
    if playlist is None:
        return JsonResponse(
            {"success": False, "error": _("Playlist not found.")}, status=404
        )

    video = get_object_or_404(Video, id=request.POST.get("video_id"))
    item = PlaylistItem.objects.filter(playlist=playlist, video=video).first()

    if item:
        item.delete()
        added = False
    else:
        PlaylistItem.objects.create(playlist=playlist, video=video)
        added = True

    playlist.save(update_fields=["updated_at"])

    return JsonResponse(
        {"success": True, "added": added, "count": playlist.items.count()}
    )


@login_required
@require_POST
def playlist_remove_video(request, playlist_id, video_id):
    playlist = _get_owned_playlist(request, playlist_id)
    if playlist is None:
        return JsonResponse(
            {"success": False, "error": _("Playlist not found.")}, status=404
        )

    PlaylistItem.objects.filter(playlist=playlist, video_id=video_id).delete()
    playlist.save(update_fields=["updated_at"])
    return JsonResponse({"success": True, "message": _("Removed from playlist")})
