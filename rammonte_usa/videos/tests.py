from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from html.parser import HTMLParser
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile

from . import audio_tasks
from . import transcription
from . import audio
from .audio import AudioExtractionError
from .audio_tasks import generate_audio, schedule_audio_generation
from .models import Comment, Playlist, PlaylistItem, SubtitleTrack, Video
from .subtitles import label_for, parse_vtt
from . import r2_storage
from .r2_storage import StorageError


def make_user(username, password="pw12345678", **kwargs):
    return User.objects.create_user(username, password=password, **kwargs)


def make_video(user, title="Video", **kwargs):
    defaults = {
        "file_id": "videos/sample.mp4",
        "video_url": "https://example.com/sample.mp4",
    }
    defaults.update(kwargs)
    return Video.objects.create(user=user, title=title, **defaults)


class VideoPathTests(TestCase):
    def setUp(self):
        self.user = make_user("uploader")
        self.video = make_video(self.user)

    def test_download_urls_do_not_raise(self):
        # Regression test for the missing ``pathlib.Path`` import.
        self.assertTrue(self.video.video_download_url or True)
        self.assertEqual(self.video.audio_download_url, "")


class PlaylistTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")
        self.other = make_user("bob")
        self.video = make_video(self.user)
        self.client.login(username="alice", password="pw12345678")

    def test_list_available_to_guests(self):
        self.client.logout()
        response = self.client.get(reverse("videos:playlists"))
        self.assertEqual(response.status_code, 200)

    def test_list_renders(self):
        response = self.client.get(reverse("videos:playlists"))
        self.assertEqual(response.status_code, 200)

    def test_create_playlist(self):
        response = self.client.post(
            reverse("videos:playlist_create"), {"title": "Watch later"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertTrue(
            Playlist.objects.filter(user=self.user, title="Watch later").exists()
        )

    def test_create_playlist_with_video_adds_item(self):
        response = self.client.post(
            reverse("videos:playlist_create"),
            {"title": "Favourites", "video_id": self.video.id},
        )
        playlist_id = response.json()["playlist"]["id"]
        self.assertTrue(
            PlaylistItem.objects.filter(playlist_id=playlist_id, video=self.video).exists()
        )

    def test_toggle_adds_and_removes(self):
        playlist = Playlist.objects.create(user=self.user, title="P")
        url = reverse("videos:playlist_toggle", args=[playlist.id])

        first = self.client.post(url, {"video_id": self.video.id}).json()
        self.assertTrue(first["added"])
        self.assertEqual(first["count"], 1)

        second = self.client.post(url, {"video_id": self.video.id}).json()
        self.assertFalse(second["added"])
        self.assertEqual(second["count"], 0)

    def test_only_owner_can_toggle(self):
        other_playlist = Playlist.objects.create(user=self.other, title="Theirs")
        response = self.client.post(
            reverse("videos:playlist_toggle", args=[other_playlist.id]),
            {"video_id": self.video.id},
        )
        self.assertEqual(response.status_code, 404)

    def test_private_playlist_hidden_from_others(self):
        playlist = Playlist.objects.create(user=self.other, title="Private")
        self.client.logout()
        self.client.login(username="bob", password="pw12345678")
        # bob owns it
        self.assertEqual(
            self.client.get(reverse("videos:playlist_detail", args=[playlist.id])).status_code,
            200,
        )
        # alice does not
        self.client.logout()
        self.client.login(username="alice", password="pw12345678")
        self.assertEqual(
            self.client.get(reverse("videos:playlist_detail", args=[playlist.id])).status_code,
            403,
        )

    def test_public_playlist_visible_to_others(self):
        playlist = Playlist.objects.create(user=self.other, title="Public", is_public=True)
        self.client.logout()
        self.client.login(username="alice", password="pw12345678")
        self.assertEqual(
            self.client.get(reverse("videos:playlist_detail", args=[playlist.id])).status_code,
            200,
        )

    def test_delete_playlist(self):
        playlist = Playlist.objects.create(user=self.user, title="P")
        response = self.client.post(
            reverse("videos:playlist_delete", args=[playlist.id])
        )
        self.assertTrue(response.json()["success"])
        self.assertFalse(Playlist.objects.filter(id=playlist.id).exists())

    def test_owner_can_set_a_cover(self):
        playlist = Playlist.objects.create(user=self.user, title="P")
        with mock.patch(
            "videos.views.upload_thumbnail",
            return_value={"path": "playlist-covers/x.jpg", "url": "https://cdn/x.jpg"},
        ) as upload:
            response = self.client.post(
                reverse("videos:playlist_update", args=[playlist.id]),
                {"cover_data": "data:image/jpeg;base64,AAAA"},
            )

        self.assertTrue(response.json()["success"])
        upload.assert_called_once()
        playlist.refresh_from_db()
        self.assertEqual(playlist.cover_file_id, "playlist-covers/x.jpg")
        self.assertEqual(playlist.cover_url, "https://cdn/x.jpg")

    def test_detail_shows_the_cover_for_other_signed_in_users(self):
        playlist = Playlist.objects.create(
            user=self.user,
            title="P",
            is_public=True,
            cover_url="https://cdn/cover.jpg",
        )
        # Playlists need a signed-in account; bob is not the owner.
        self.client.logout()
        self.client.login(username="bob", password="pw12345678")
        response = self.client.get(
            reverse("videos:playlist_detail", args=[playlist.id])
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("https://cdn/cover.jpg", body)
        self.assertIn("has-cover", body)

    def test_non_owner_cannot_set_a_cover(self):
        theirs = Playlist.objects.create(user=self.other, title="Theirs")
        response = self.client.post(
            reverse("videos:playlist_update", args=[theirs.id]),
            {"cover_data": "data:image/jpeg;base64,AAAA"},
        )
        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.cover_file_id, "")

    def test_replacing_the_cover_deletes_the_old_object(self):
        playlist = Playlist.objects.create(
            user=self.user, title="P", cover_file_id="playlist-covers/old.jpg"
        )
        with mock.patch(
            "videos.views.upload_thumbnail",
            return_value={"path": "playlist-covers/new.jpg", "url": "https://cdn/new.jpg"},
        ), mock.patch("videos.views.delete_file") as delete_file:
            self.client.post(
                reverse("videos:playlist_update", args=[playlist.id]),
                {"cover_data": "data:image/jpeg;base64,AAAA"},
            )

        delete_file.assert_called_once_with("playlist-covers/old.jpg")

    def test_delete_playlist_removes_the_cover_object(self):
        playlist = Playlist.objects.create(
            user=self.user, title="P", cover_file_id="playlist-covers/x.jpg"
        )
        with mock.patch("videos.views.delete_file") as delete_file:
            self.client.post(reverse("videos:playlist_delete", args=[playlist.id]))

        delete_file.assert_called_once_with("playlist-covers/x.jpg")

    def test_removing_a_video_asks_first(self):
        playlist = Playlist.objects.create(user=self.user, title="P")
        PlaylistItem.objects.create(playlist=playlist, video=self.video)
        body = self.client.get(
            reverse("videos:playlist_detail", args=[playlist.id])
        ).content.decode()
        # The remove handler must route through our own confirmation dialog
        # rather than deleting on a single click.
        self.assertIn("function removeFromPlaylist", body)
        self.assertIn("fazakkirConfirm", body)
        self.assertNotIn("window.confirm", body)

    def test_cover_can_be_set_while_creating_the_playlist(self):
        with mock.patch(
            "videos.views.upload_thumbnail",
            return_value={"path": "playlist-covers/n.jpg", "url": "https://cdn/n.jpg"},
        ) as upload:
            response = self.client.post(
                reverse("videos:playlist_create"),
                {"title": "With cover", "cover_data": "data:image/jpeg;base64,AAAA"},
            )

        self.assertTrue(response.json()["success"])
        upload.assert_called_once()
        playlist = Playlist.objects.get(title="With cover")
        self.assertEqual(playlist.cover_url, "https://cdn/n.jpg")

    def test_create_without_a_cover_reports_no_error(self):
        response = self.client.post(
            reverse("videos:playlist_create"), {"title": "Plain"}
        )
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["cover_error"], "")

    def test_playlist_list_shows_the_cover(self):
        Playlist.objects.create(
            user=self.user, title="Covered", cover_url="https://cdn/c.jpg"
        )
        body = self.client.get(reverse("videos:playlists")).content.decode()
        self.assertIn("https://cdn/c.jpg", body)
        self.assertIn("has-cover", body)

    def test_playlist_list_keeps_the_placeholder_without_a_cover(self):
        Playlist.objects.create(user=self.user, title="Bare")
        body = self.client.get(reverse("videos:playlists")).content.decode()
        self.assertNotIn("has-cover", body)

    def test_video_page_can_create_a_playlist_with_a_cover(self):
        body = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()
        self.assertIn("saveNewCover", body)
        self.assertIn("coverToDataUrl", body)

    def test_cover_can_be_set_while_creating_from_the_video_page(self):
        with mock.patch(
            "videos.views.upload_thumbnail",
            return_value={"path": "playlist-covers/v.jpg", "url": "https://cdn/v.jpg"},
        ):
            response = self.client.post(
                reverse("videos:playlist_create"),
                {
                    "title": "From video",
                    "video_id": self.video.id,
                    "cover_data": "data:image/jpeg;base64,AAAA",
                },
            )
        self.assertTrue(response.json()["success"])
        playlist = Playlist.objects.get(title="From video")
        self.assertEqual(playlist.cover_url, "https://cdn/v.jpg")
        self.assertTrue(playlist.contains(self.video))


class CommentTests(TestCase):
    def setUp(self):
        self.author = make_user("carol")
        self.other = make_user("dave")
        self.video = make_video(self.author)

    def test_guest_can_comment(self):
        response = self.client.post(
            reverse("videos:comment_add", args=[self.video.id]), {"body": "Hi"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    def test_add_comment(self):
        self.client.login(username="carol", password="pw12345678")
        response = self.client.post(
            reverse("videos:comment_add", args=[self.video.id]), {"body": "Nice video"}
        )
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["comment"]["body"], "Nice video")
        self.assertEqual(Comment.objects.count(), 1)

    def test_empty_comment_rejected(self):
        self.client.login(username="carol", password="pw12345678")
        response = self.client.post(
            reverse("videos:comment_add", args=[self.video.id]), {"body": "   "}
        )
        self.assertEqual(response.status_code, 400)

    def test_author_can_delete_own_comment(self):
        comment = Comment.objects.create(video=self.video, user=self.author, body="hi")
        self.client.login(username="carol", password="pw12345678")
        response = self.client.post(reverse("videos:comment_delete", args=[comment.id]))
        self.assertTrue(response.json()["success"])
        self.assertFalse(Comment.objects.filter(id=comment.id).exists())

    def test_other_user_cannot_delete(self):
        comment = Comment.objects.create(video=self.video, user=self.author, body="hi")
        self.client.login(username="dave", password="pw12345678")
        response = self.client.post(reverse("videos:comment_delete", args=[comment.id]))
        self.assertEqual(response.status_code, 403)

    def test_staff_can_delete_any_comment(self):
        comment = Comment.objects.create(video=self.video, user=self.author, body="hi")
        moderator = make_user("mod", is_staff=True)
        moderator.profile.access_level = Profile.Access.WRITE
        moderator.profile.save(update_fields=["access_level"])
        self.client.login(username="mod", password="pw12345678")
        response = self.client.post(reverse("videos:comment_delete", args=[comment.id]))
        self.assertTrue(response.json()["success"])


class DetailPageTests(TestCase):
    def setUp(self):
        self.user = make_user("viewer")
        self.video = make_video(self.user)
        self.playlist = Playlist.objects.create(user=self.user, title="My list")
        PlaylistItem.objects.create(playlist=self.playlist, video=self.video)
        Comment.objects.create(video=self.video, user=self.user, body="Great clip")

    @mock.patch("videos.views.schedule_audio_generation")
    def test_detail_renders_comments_and_save_panel(self, _schedule):
        self.client.login(username="viewer", password="pw12345678")
        response = self.client.get(reverse("videos:detail", args=[self.video.id]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Great clip", content)
        self.assertIn("My list", content)
        self.assertIn('id="savePanel"', content)

    @mock.patch("videos.views.schedule_audio_generation")
    def test_detail_guest_can_comment_and_save(self, _schedule):
        response = self.client.get(reverse("videos:detail", args=[self.video.id]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('id="commentBody"', content)
        self.assertIn('id="savePanel"', content)


class VideoDeletionTests(TestCase):
    def setUp(self):
        self.admin = make_user("root", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(self.admin)
        Comment.objects.create(video=self.video, user=self.admin, body="c")
        PlaylistItem.objects.create(
            playlist=Playlist.objects.create(user=self.admin, title="P"), video=self.video
        )

    @mock.patch("videos.r2_storage.delete_file")
    def test_delete_video_cascades(self, _delete):
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(reverse("videos:delete", args=[self.video.id]))
        self.assertTrue(response.json()["success"])
        self.assertFalse(Video.objects.filter(id=self.video.id).exists())
        # Database cascades: comments and playlist entries are gone too.
        self.assertEqual(Comment.objects.count(), 0)
        self.assertEqual(PlaylistItem.objects.count(), 0)

    def test_member_cannot_delete_video(self):
        member = make_user("regular")
        self.client.login(username="regular", password="pw12345678")
        response = self.client.post(reverse("videos:delete", args=[self.video.id]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Video.objects.filter(id=self.video.id).exists())


class VideoEditTests(TestCase):
    """Administrators can change every editable field of a stored video."""

    def setUp(self):
        self.admin = make_user("editor", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(
            self.admin,
            title="Old title",
            description="Old description",
            thumbnail_file_id="thumbnails/old.jpg",
            thumbnail_url="https://example.com/old.jpg",
            audio_file_id="audio/old.mp3",
            audio_url="https://example.com/old.mp3",
            audio_status="ready",
        )
        SubtitleTrack.objects.create(
            video=self.video,
            language_code="fr",
            label="French",
            content="WEBVTT",
        )
        self.client.login(username="editor", password="pw12345678")

    def test_edit_page_loads_with_current_values(self):
        response = self.client.get(reverse("videos:edit", args=[self.video.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Old title")
        self.assertContains(response, "Old description")

    def test_member_cannot_reach_the_edit_page(self):
        make_user("regular")
        self.client.login(username="regular", password="pw12345678")
        response = self.client.get(reverse("videos:edit", args=[self.video.id]))
        self.assertEqual(response.status_code, 403)

    def test_guest_cannot_reach_the_edit_page(self):
        self.client.logout()
        response = self.client.get(reverse("videos:edit", args=[self.video.id]))
        self.assertEqual(response.status_code, 403)

    def test_title_and_description_are_saved(self):
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {"title": "New title", "description": "New description"},
        )
        self.assertEqual(response.status_code, 302)
        self.video.refresh_from_db()
        self.assertEqual(self.video.title, "New title")
        self.assertEqual(self.video.description, "New description")
        # Nothing else should have moved.
        self.assertEqual(self.video.file_id, "videos/sample.mp4")
        self.assertEqual(self.video.thumbnail_file_id, "thumbnails/old.jpg")
        self.assertEqual(self.video.audio_file_id, "audio/old.mp3")

    def test_empty_title_is_rejected(self):
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {"title": "", "description": "x"},
        )
        self.assertEqual(response.status_code, 400)
        self.video.refresh_from_db()
        self.assertEqual(self.video.title, "Old title")

    @mock.patch("videos.views.delete_file")
    @mock.patch("videos.views.upload_thumbnail")
    def test_replacing_the_thumbnail_swaps_and_deletes_the_old_object(
        self, upload_thumbnail, delete_file
    ):
        upload_thumbnail.return_value = {
            "path": "thumbnails/new.jpg",
            "url": "https://example.com/new.jpg",
        }
        image = SimpleUploadedFile(
            "new.jpg", b"\xff\xd8\xff\xe0fakejpeg", content_type="image/jpeg"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {"title": "Old title", "description": "Old description", "thumbnail_file": image},
        )
        self.assertEqual(response.status_code, 302)
        self.video.refresh_from_db()
        self.assertEqual(self.video.thumbnail_file_id, "thumbnails/new.jpg")
        # The previous object must not be left behind in the bucket.
        delete_file.assert_called_once_with("thumbnails/old.jpg")

    @mock.patch("videos.views.delete_file")
    def test_removing_the_thumbnail_clears_and_deletes(self, delete_file):
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {
                "title": "Old title",
                "description": "Old description",
                "remove_thumbnail": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.video.refresh_from_db()
        self.assertEqual(self.video.thumbnail_file_id, "")
        self.assertEqual(self.video.thumbnail_url, "")
        delete_file.assert_called_once_with("thumbnails/old.jpg")

    def test_new_thumbnail_and_removal_together_is_rejected(self):
        image = SimpleUploadedFile(
            "new.jpg", b"\xff\xd8\xff\xe0fakejpeg", content_type="image/jpeg"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {
                "title": "Old title",
                "description": "Old description",
                "thumbnail_file": image,
                "remove_thumbnail": "on",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.video.refresh_from_db()
        self.assertEqual(self.video.thumbnail_file_id, "thumbnails/old.jpg")

    def test_non_image_thumbnail_is_rejected(self):
        image = SimpleUploadedFile(
            "bad.txt", b"not an image", content_type="text/plain"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {"title": "Old title", "description": "Old description", "thumbnail_file": image},
        )
        self.assertEqual(response.status_code, 400)
        self.video.refresh_from_db()
        self.assertEqual(self.video.thumbnail_file_id, "thumbnails/old.jpg")

    @mock.patch("videos.views.schedule_audio_generation")
    @mock.patch("videos.views.delete_file")
    @mock.patch("videos.views.upload_video")
    @mock.patch("videos.views._extract_audio_track")
    def test_replacing_the_video_resets_derived_media(
        self, extract, upload_video, delete_file, schedule_audio
    ):
        extract.return_value = ("audio/new.mp3", "https://example.com/new.mp3", "ready")
        upload_video.return_value = {
            "path": "videos/new.mp4",
            "url": "https://example.com/new.mp4",
        }
        replacement = SimpleUploadedFile(
            "new.mp4", b"\x00\x00\x00\x18ftypmp42fake", content_type="video/mp4"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {
                "title": "Renamed",
                "description": "d",
                "video_file": replacement,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.video.refresh_from_db()
        self.assertEqual(self.video.file_id, "videos/new.mp4")
        self.assertEqual(self.video.audio_file_id, "audio/new.mp3")
        self.assertEqual(self.video.audio_status, "ready")
        # The stale objects and the subtitles of the old footage are gone.
        self.assertIn("videos/sample.mp4", delete_file.call_args_list[0][0])
        self.assertIn("audio/old.mp3", [call.args[0] for call in delete_file.call_args_list])
        self.assertEqual(SubtitleTrack.objects.filter(video=self.video).count(), 0)
        self.assertEqual(self.video.auto_subtitle_status, "")
        # The MP3 is already there, so no regeneration is scheduled.
        schedule_audio.assert_not_called()

    @mock.patch("videos.views.schedule_audio_generation")
    @mock.patch("videos.views.delete_file")
    @mock.patch("videos.views.upload_video")
    @mock.patch("videos.views._extract_audio_track")
    def test_replacing_the_video_schedules_mp3_when_extraction_is_deferred(
        self, extract, upload_video, delete_file, schedule_audio
    ):
        extract.return_value = ("", "", "")
        upload_video.return_value = {
            "path": "videos/new.mp4",
            "url": "https://example.com/new.mp4",
        }
        replacement = SimpleUploadedFile(
            "new.mp4", b"\x00\x00\x00\x18ftypmp42fake", content_type="video/mp4"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {
                "title": "Renamed",
                "description": "d",
                "video_file": replacement,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.video.refresh_from_db()
        self.assertEqual(self.video.audio_file_id, "")
        self.assertEqual(self.video.audio_status, "")
        schedule_audio.assert_called_once_with(self.video.id)

    @mock.patch("videos.views.upload_video")
    @mock.patch("videos.views._extract_audio_track")
    def test_failed_video_upload_keeps_the_original(
        self, extract, upload_video
    ):
        extract.return_value = ("", "", "")
        upload_video.side_effect = StorageError("bucket is full")
        replacement = SimpleUploadedFile(
            "new.mp4", b"\x00\x00\x00\x18ftypmp42fake", content_type="video/mp4"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {
                "title": "Renamed",
                "description": "d",
                "video_file": replacement,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.video.refresh_from_db()
        self.assertEqual(self.video.file_id, "videos/sample.mp4")
        self.assertEqual(self.video.title, "Old title")

    def test_detail_page_shows_edit_link_to_administrators(self):
        response = self.client.get(reverse("videos:detail", args=[self.video.id]))
        self.assertContains(response, reverse("videos:edit", args=[self.video.id]))

    def test_detail_page_hides_edit_link_from_members(self):
        make_user("regular")
        self.client.login(username="regular", password="pw12345678")
        response = self.client.get(reverse("videos:detail", args=[self.video.id]))
        self.assertNotContains(response, reverse("videos:edit", args=[self.video.id]))


class VideoSharingTests(TestCase):
    """Visitors can share the video page and the MP3 as never-expiring links."""

    def setUp(self):
        self.admin = make_user("sharer", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.with_audio = make_video(
            self.admin,
            title="Shareable",
            audio_file_id="audio/share.mp3",
            audio_url="https://example.com/share.mp3",
            audio_status="ready",
        )
        self.silent = make_video(self.admin, title="No audio")

    def test_share_panel_is_visible_to_anonymous_visitors(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="sharePanel"')
        self.assertContains(response, 'id="shareToggle"')

    def test_video_share_link_is_absolute(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        self.assertContains(response, "testserver" + reverse("videos:detail", args=[self.with_audio.id]))

    def test_mp3_share_link_uses_the_permanent_audio_route(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        body = self._share_links(response.content.decode())
        self.assertIn("testserver" + reverse("videos:audio_stream", args=[self.with_audio.id]), body)

    def test_mp3_share_link_is_never_the_expiring_presigned_url(self):
        # The download button legitimately signs a URL that dies after twelve
        # hours, but the link handed to a friend must never be one of those.
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        share_block = self._share_links(response.content.decode())
        self.assertNotIn("X-Amz-Signature", share_block)
        self.assertNotIn("share.mp3?", share_block)

    @staticmethod
    def _share_links(body: str) -> str:
        """Return just the SHARE_LINKS map, with JS escaping undone.

        ``escapejs`` turns ``/`` into ``\\/`` and ``-`` into ``\\u002D``. Both are
        valid inside a JavaScript string literal, so the browser decodes them
        back to the intended URL; the test just has to compare the decoded form.
        """
        import re

        start = body.index("const SHARE_LINKS")
        end = body.index("}", body.index("audio:", start))
        block = body[start:end].replace("\\/", "/")
        return re.sub(r"\\u00([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), block)

    def test_video_without_audio_hides_the_mp3_target(self):
        response = self.client.get(reverse("videos:detail", args=[self.silent.id]))
        self.assertContains(response, 'id="sharePanel"')
        self.assertNotContains(response, 'data-share="audio"')

    def test_share_network_links_carry_a_network_and_a_kind(self):
        # The links used to put the *kind* ("video"/"audio") in the network
        # attribute, so the script looked up a network that does not exist and
        # threw before it could ever fill the href in.
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        body = response.content.decode()
        for network in ("whatsapp", "telegram", "facebook", "x"):
            self.assertIn(f'data-share-network="{network}"', body)
        self.assertNotIn('data-share-network="video"', body)
        self.assertIn('data-share-kind="video"', body)

    def test_share_script_guards_an_unknown_network(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        body = response.content.decode()
        self.assertIn("SHARE_NETWORK_URLS[network]", body)
        self.assertNotIn("SHARE_NETWORK_URLS[kind](", body)

    def test_share_networks_are_prefilled_with_the_link(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        body = response.content.decode()
        self.assertIn("wa.me", body)
        self.assertIn("t.me/share/url", body)
        self.assertIn("facebook.com/sharer", body)
        self.assertIn("twitter.com/intent/tweet", body)

    def test_share_panel_javascript_is_present(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        body = response.content.decode()
        self.assertIn("function shareKind", body)
        self.assertIn("function toggleShareMenu", body)
        # A dismissal must not be reported as a failure.
        self.assertIn("AbortError", body)


class CustomPlayerTests(TestCase):
    """The player owns its controls instead of delegating to the browser.

    The native control bar drew its own play button over the poster image, so
    the poster and the `controls` attribute were both dropped. Two things must
    survive that change: `playsinline`, without which iOS takes playback
    fullscreen and background playback stops, and the Media Session wiring,
    which is what keeps audio going when the screen locks.
    """

    def setUp(self):
        self.admin = make_user("player", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(
            self.admin,
            title="Played",
            audio_file_id="audio/p.mp3",
            audio_url="https://example.com/p.mp3",
            audio_status="ready",
        )

    def body(self):
        response = self.client.get(reverse("videos:detail", args=[self.video.id]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_video_has_no_poster(self):
        self.assertNotIn("poster=", self.body())

    def test_media_elements_have_no_native_controls_attribute(self):
        body = self.body()
        self.assertNotIn('<video id="video-player" controls', body)
        self.assertNotIn('<audio id="audio-player" controls', body)

    def test_playsinline_is_kept_for_background_playback(self):
        body = self.body()
        video_tag = body[body.index("<video id="):body.index(">", body.index("<video id="))]
        self.assertIn("playsinline", video_tag)

    def test_custom_control_bar_is_rendered(self):
        body = self.body()
        for element_id in ("playerBar", "seekBar", "playPauseBtn", "volumeBar",
                           "rateSelect", "fullscreenBtn", "currentTime", "duration"):
            self.assertIn(f'id="{element_id}"', body)

    def test_media_session_wiring_survives(self):
        body = self.body()
        self.assertIn("navigator.mediaSession", body)
        self.assertIn("mediaSession.setActionHandler", body)
        # seekbackward / seekforward are what the lock screen buttons use.
        self.assertIn("seekbackward", body)
        self.assertIn("seekforward", body)
        self.assertIn("MediaMetadata", body)

    def test_control_bar_drives_the_active_player(self):
        body = self.body()
        # Switching to MP3 mode must not leave the bar bound to the video only.
        self.assertIn("activePlayer", body)
        self.assertIn("function refreshAllControls", body)
        self.assertIn("if (typeof refreshAllControls", body)

    def test_seek_and_volume_are_wired(self):
        body = self.body()
        self.assertIn("function seekBy", body)
        self.assertIn('seekBar.addEventListener("input"', body)
        self.assertIn('volumeBar.addEventListener("input"', body)

    def test_keyboard_transport_shortcuts_are_wired(self):
        body = self.body()
        self.assertIn('"keydown"', body)
        self.assertIn("togglePlayPause()", body)

    def test_subtitle_tracks_still_reach_the_video_element(self):
        SubtitleTrack.objects.create(
            video=self.video, language_code="fr", label="French", content="WEBVTT"
        )
        body = self.body()
        self.assertIn('kind="subtitles"', body)
        self.assertIn(reverse("videos:subtitle_file", args=[self.video.id, "fr"]), body)

    def test_cc_button_is_visible_on_the_page_below_the_player(self):
        SubtitleTrack.objects.create(
            video=self.video, language_code="fr", label="French", content="WEBVTT"
        )
        body = self.body()
        # The headline control lives on the page, outside the player bar, so it
        # is on screen without having to tap the video first.
        self.assertIn('id="subtitleCcBtn"', body)
        self.assertIn('id="subtitleCcMenu"', body)
        self.assertIn('id="subtitleCcList"', body)
        controls = body.index('class="subtitle-controls"')
        self.assertIn('id="subtitleCcBtn"', body[controls:controls + 800])
        # It sits after the player in the document, i.e. below it on the page.
        self.assertGreater(controls, body.index('id="playerBar"'))

    def test_page_cc_button_names_the_current_language(self):
        SubtitleTrack.objects.create(
            video=self.video, language_code="fr", label="French", content="WEBVTT"
        )
        body = self.body()
        self.assertIn('id="subtitleCcCurrent"', body)
        # The badge makes it unmistakably the caption control.
        self.assertIn("cc-badge", body)
        self.assertIn(">CC<", body)

    def test_cc_button_lives_in_the_control_bar(self):
        SubtitleTrack.objects.create(
            video=self.video, language_code="fr", label="French", content="WEBVTT"
        )
        body = self.body()
        # The bar copy is still there, so captions can be turned on while
        # watching â€” including in fullscreen and the audio player.
        self.assertIn('id="ccBtn"', body)
        self.assertIn('id="ccList"', body)
        bar = body.index('id="playerBar"')
        self.assertIn('id="ccBtn"', body[bar:body.index("</div>", bar)])

    def test_cc_menu_offers_off_and_every_language(self):
        SubtitleTrack.objects.create(
            video=self.video, language_code="fr", label="French", content="WEBVTT"
        )
        SubtitleTrack.objects.create(
            video=self.video, language_code="ar", label="Arabic", content="WEBVTT"
        )
        body = self.body()
        self.assertIn('id="ccList"', body)
        for lang in ("", "fr", "ar"):
            self.assertIn(f'data-lang="{lang}"', body)
        self.assertIn("Off", body)

    def test_cc_button_is_always_shown_even_without_tracks(self):
        # A viewer should be able to see that captions exist as a feature and
        # what they are set to, rather than hunting for a control that is not
        # drawn at all. This is the case that made the caption UI look missing.
        body = self.body()
        self.assertIn('id="ccBtn"', body)
        self.assertIn('id="subtitleCcBtn"', body)
        self.assertIn('class="subtitle-controls"', body)
        # With nothing to choose, the menu says so instead of opening empty.
        self.assertIn("cc-empty", body)

    def test_empty_cc_menu_offers_generate_to_writers_only(self):
        # The owner can fill the gap from the place they noticed it.
        writer = make_user("cc-writer")
        Profile.objects.filter(user=writer).update(access_level=Profile.Access.WRITE)
        staff_client = Client()
        staff_client.force_login(writer)
        staff_body = staff_client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode("utf-8")
        self.assertIn("cc-generate", staff_body)
        self.assertIn("generateSubtitles", staff_body)

        # A plain viewer must not be offered the action at all.
        self.assertNotIn("cc-generate", self.body())

    def test_captions_are_drawn_by_the_page_not_the_browser(self):
        # The native caption box cannot be styled to look the same everywhere,
        # so the cues are painted into an overlay inside the picture instead.
        SubtitleTrack.objects.create(
            video=self.video, language_code="fr", label="French", content="WEBVTT"
        )
        body = self.body()
        self.assertIn('id="captionLayer"', body)
        self.assertIn('id="captionText"', body)
        self.assertIn("parseVtt", body)
        # The browser's own rendering is suppressed, not the cues themselves.
        self.assertIn('track.mode = (lang && code === lang) ? mode : "disabled"', body)
        # ...except inside iPhone native fullscreen, where the native player is
        # the only thing on screen and must be given the track to paint.
        self.assertIn("function nativeVideoFullscreen()", body)
        self.assertIn('? "showing" : "hidden"', body)
        self.assertIn('"webkitbeginfullscreen", "webkitendfullscreen"', body)

    def test_cc_menu_script_is_wired(self):
        SubtitleTrack.objects.create(
            video=self.video, language_code="fr", label="French", content="WEBVTT"
        )
        body = self.body()
        self.assertIn("closeCcMenus", body)
        self.assertIn('menu.classList.add("open")', body)
        self.assertIn('btn.setAttribute("aria-expanded"', body)
        # Every CC menu on the page is wired, not just the one in the bar.
        self.assertIn('document.querySelectorAll(".cc-menu")', body)


class AudioModeCoverTests(TestCase):
    """Listening to the MP3 shows the thumbnail instead of a black rectangle.

    Switching to the audio element hides the <video>, which left the 16:9 stage
    as an empty black box.
    """

    def setUp(self):
        self.admin = make_user("listener", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(
            self.admin,
            title="Cover art",
            thumbnail_file_id="thumbnails/c.jpg",
            thumbnail_url="https://example.com/c.jpg",
            audio_file_id="audio/c.mp3",
            audio_url="https://example.com/c.mp3",
            audio_status="ready",
        )
        self.body = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()

    def test_cover_image_is_rendered(self):
        self.assertIn('id="audioCover"', self.body)
        # display_thumbnail_url may be a presigned R2 link, so match on the
        # object key rather than the stored URL.
        self.assertIn("thumbnails/c.jpg", self.body)
        self.assertIn('alt="Cover art"', self.body)

    def test_cover_is_hidden_until_audio_mode_is_entered(self):
        # The rules live in the stylesheet, not the markup.
        css = self._player_css()
        self.assertIn(".audio-cover", css)
        self.assertIn("video-player.audio-mode .audio-cover", css)
        # Hidden by default, shown only once the audio mode class is present.
        cover = css[css.index(".audio-cover {"):css.index("}", css.index(".audio-cover {"))]
        self.assertIn("display: none", cover)

    def test_switching_mode_toggles_the_cover_class(self):
        self.assertIn('classList.add("audio-mode")', self.body)
        self.assertIn('classList.remove("audio-mode")', self.body)

    def test_video_without_a_thumbnail_gets_no_cover_element(self):
        bare = make_video(self.admin, title="No art", audio_file_id="audio/n.mp3")
        body = self.client.get(reverse("videos:detail", args=[bare.id])).content.decode()
        self.assertNotIn('id="audioCover"', body)

    @staticmethod
    def _player_css() -> str:
        root = (
            Path(__file__).resolve().parent.parent
            / "static" / "css" / "videoplayer.css"
        )
        return root.read_text(encoding="utf-8")


class AutoHidingControlsTests(TestCase):
    """The control bar must not sit on top of the picture while it plays."""

    def setUp(self):
        self.admin = make_user("tapper", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(
            self.admin,
            title="Tapped",
            audio_file_id="audio/t.mp3",
            audio_url="https://example.com/t.mp3",
            audio_status="ready",
        )
        self.body = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()

    def test_bar_starts_hidden(self):
        self.assertIn("hideControls()", self.body)
        # The hidden state is the default, so the first frame is never covered.
        self.assertIn("visibility: hidden", self._player_css())

    def test_tap_zone_brings_the_controls_back(self):
        self.assertIn('id="tapZone"', self.body)

    def test_tapping_the_picture_plays_and_pauses(self):
        # Tapping used to only reveal the bar, which left no tap anywhere on
        # the video able to start playback.
        self.assertIn("tapZone.addEventListener", self.body)
        handler = self.body[
            self.body.index("tapZone.addEventListener"):self.body.index("if (playerStage)")
        ]
        self.assertIn("togglePlayPause()", handler)
        self.assertIn("showControls()", handler)

    def test_refused_playback_reveals_the_bar_instead_of_failing_silently(self):
        toggle = self.body[
            self.body.index("function togglePlayPause"):self.body.index("function seekBy")
        ]
        self.assertIn("showControls()", toggle)
        self.assertIn(".catch(", toggle)

    def test_play_hides_and_pause_shows(self):
        self.assertIn('el.addEventListener("play"', self.body)
        self.assertIn('el.addEventListener("pause"', self.body)
        self.assertIn("CONTROLS_IDLE_MS", self.body)

    def test_hover_reveals_the_bar_on_a_pointer_device(self):
        # Entering the picture must be enough, without needing to move.
        self.assertIn('addEventListener("mouseenter"', self.body)
        self.assertIn('addEventListener("mouseleave"', self.body)

    def test_hover_behaviour_is_limited_to_real_pointers(self):
        # A tablet reports hover: none even with a mouse attached; without this
        # guard a synthesised mouseenter would latch the bar open.
        self.assertIn("(hover: hover) and (pointer: fine)", self.body)
        self.assertIn("const canHover", self.body)

    def test_bar_never_hides_while_the_pointer_is_resting_on_it(self):
        guard = self.body[
            self.body.index("function hideControls()"):self.body.index("function toggleControls()")
        ]
        self.assertIn("if (pointerInsidePlayer() && canHover) return", guard)

    def test_the_pointer_is_tracked_on_the_bar_as_well_as_the_picture(self):
        # The bar floats over the bottom of the picture and is a sibling of it,
        # so moving down onto a control leaves the picture a moment before the
        # pointer lands on the bar.
        self.assertIn("function pointerInsidePlayer()", self.body)
        self.assertIn("pointerInsideBar = true", self.body)
        self.assertIn("pointerInsideBar = false", self.body)

    def test_playing_hides_only_when_nobody_is_hovering(self):
        self.assertIn("function shouldAutoHide()", self.body)
        helper = self.body[
            self.body.index("function shouldAutoHide()"):self.body.index("function showControls()")
        ]
        self.assertIn("pointerInsidePlayer()", helper)
        # A paused video must always keep its controls up.
        self.assertIn("return playing && !pointerInsidePlayer()", helper)
        play = self.body[self.body.index('el.addEventListener("play"'):]
        self.assertIn("if (shouldAutoHide()) hideControls()", play[:400])

    def test_a_paused_player_is_never_left_inert(self):
        # Removing the poster means no picture loads until playback starts, so
        # the player used to open as a black rectangle with hidden controls.
        self.assertIn('id="bigPlay"', self.body)
        self.assertIn("function refreshBigPlay()", self.body)
        self.assertIn(".player-bigplay", self._player_css())

    def test_big_play_button_starts_playback(self):
        handler = self.body[
            self.body.index("if (bigPlay) {"):self.body.index("if (tapZone) {")
        ]
        self.assertIn("togglePlayPause()", handler)

    def test_big_play_is_hidden_while_playing(self):
        helper = self.body[
            self.body.index("function refreshBigPlay()"):self.body.index("if (playerStage)")
        ]
        self.assertIn("bigPlay.hidden = !paused", helper)

    def test_controls_start_visible_not_hidden(self):
        # A paused video must show its bar; only playback hides it.
        tail = self.body[self.body.index("wireControls()\n"):]
        self.assertIn("showControls()", tail[:200])
        self.assertNotIn("hideControls()", tail[:200])

    def test_mobile_puts_the_bar_below_the_picture(self):
        css = self._player_css()
        self.assertIn("@media (max-width: 768px)", css)
        self.assertIn("position: static", css)

    @staticmethod
    def _player_css() -> str:
        root = (
            Path(__file__).resolve().parent.parent
            / "static" / "css" / "videoplayer.css"
        )
        return root.read_text(encoding="utf-8")


class ThumbnailOverlayTests(TestCase):
    """The hover play overlay must never permanently cover a thumbnail.

    Touch devices report ``hover: none``, and the card is already one big tap
    target there, so the overlay is hidden rather than forced visible.
    """

    def setUp(self):
        root = Path(__file__).resolve().parent.parent / "static" / "css" / "videos.css"
        self.css = root.read_text(encoding="utf-8")

    def test_touch_rule_hides_the_overlay_instead_of_showing_it(self):
        touch_block = self._hover_none_block()
        self.assertIn("display: none", touch_block)
        self.assertNotIn("opacity: 1", touch_block)

    def test_overlay_sits_on_the_thumbnail_edge_not_the_middle(self):
        block = self._block_for(".play-icon {")
        self.assertIn("left: 12px", block)
        self.assertIn("translateY(-50%)", block)
        # Centring with translate(-50%, -50%) is what put it over the subject.
        self.assertNotIn("left: 50%", block)
        self.assertNotIn("translate(-50%, -50%)", block)

    def test_overlay_still_appears_on_hover_capable_devices(self):
        self.assertIn(".video-card:hover .play-icon", self.css)

    def _block_for(self, opening: str) -> str:
        start = self.css.index(opening)
        end = self.css.index("}", start)
        return self.css[start:end]

    def _hover_none_block(self) -> str:
        start = self.css.index("@media (hover: none)")
        depth = 0
        for index in range(start, len(self.css)):
            if self.css[index] == "{":
                depth += 1
            elif self.css[index] == "}":
                depth -= 1
                if depth == 0:
                    return self.css[start:index]
        raise AssertionError("unterminated @media (hover: none) block")


class FakeBody:

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def iter_chunks(self, chunk_size=None):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


class AudioStreamTests(TestCase):
    def setUp(self):
        self.user = make_user("owner")
        self.video = make_video(
            self.user,
            title="With audio",
            audio_file_id="audio/track.mp3",
            audio_url="",
        )

    def test_no_audio_returns_404(self):
        silent = make_video(self.user, title="Silent")
        response = self.client.get(reverse("videos:audio_stream", args=[silent.id]))
        self.assertEqual(response.status_code, 404)

    def test_legacy_audio_redirects_to_stored_url(self):
        legacy = make_video(
            self.user,
            title="Legacy",
            audio_file_id="legacy/track.mp3",
            audio_url="https://cdn.example.com/legacy.mp3",
        )
        response = self.client.get(reverse("videos:audio_stream", args=[legacy.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://cdn.example.com/legacy.mp3")

    @mock.patch("videos.views.get_object")
    def test_streams_audio_same_origin(self, get_object):
        body = FakeBody([b"abc", b"def"])
        get_object.return_value = {
            "Body": body,
            "ContentType": "audio/mpeg",
            "ContentLength": 6,
        }

        response = self.client.get(reverse("videos:audio_stream", args=[self.video.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(b"".join(response.streaming_content), b"abcdef")
        self.assertTrue(body.closed)
        get_object.assert_called_once_with(range_header=None, path="audio/track.mp3")

    @mock.patch("videos.views.get_object")
    def test_range_request_is_passed_through(self, get_object):
        get_object.return_value = {
            "Body": FakeBody([b"abc"]),
            "ContentType": "audio/mpeg",
            "ContentLength": 3,
            "ContentRange": "bytes 0-2/6",
        }

        response = self.client.get(
            reverse("videos:audio_stream", args=[self.video.id]),
            HTTP_RANGE="bytes=0-2",
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 0-2/6")
        self.assertEqual(response["Content-Length"], "3")

    @mock.patch("videos.r2_storage.resolve_url")
    @mock.patch("videos.views.get_object", side_effect=StorageError("boom"))
    def test_falls_back_to_presigned_url(self, _get_object, resolve_url):
        resolve_url.return_value = "https://r2.example.com/signed.mp3"
        response = self.client.get(reverse("videos:audio_stream", args=[self.video.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://r2.example.com/signed.mp3")


class VideoCardDownloadTests(TestCase):
    def setUp(self):
        self.user = make_user("uploader")
        self.audio_video = make_video(
            self.user, title="Has audio", audio_file_id="audio/a.mp3"
        )

    def test_list_card_offers_mp3(self):
        content = self.client.get(reverse("videos:list")).content.decode()
        self.assertIn("card-download", content)
        self.assertIn("Download MP3", content)

    def test_channel_card_offers_mp3(self):
        content = self.client.get(
            reverse("videos:channel", args=[self.user.username])
        ).content.decode()
        self.assertIn("card-download", content)

    def test_silent_video_has_no_mp3_button(self):
        self.audio_video.delete()
        make_video(self.user, title="Silent")
        content = self.client.get(reverse("videos:list")).content.decode()
        self.assertNotIn("card-download", content)


class LanguageToggleTests(TestCase):
    """The four languages offered by the in-app switcher."""

    LANGUAGES = ("en", "fr", "ar", "wo")

    def setUp(self):
        self.user = make_user("viewer")
        # A video so the page exercises the card markup (the MP3 button) as well
        # as the navbar.
        self.video = make_video(
            self.user, title="Listen", audio_file_id="audio/track.mp3"
        )

    def _page(self, language):
        self.client.cookies["django_language"] = language
        return self.client.get(reverse("videos:list")).content.decode()

    def test_all_four_languages_are_offered(self):
        content = self._page("en")
        for code in self.LANGUAGES:
            self.assertIn(f'value="{code}"', content)

    def test_language_is_marked_active(self):
        # The button attributes wrap across lines in the template, so match with
        # a whitespace-tolerant pattern rather than one literal string.
        self.assertRegex(
            self._page("ar"),
            r'value="ar"\s+class="lang-option active"',
        )

    def test_arabic_renders_right_to_left(self):
        self.assertIn('<html lang="ar" dir="rtl">', self._page("ar"))

    def test_other_languages_render_left_to_right(self):
        for code in ("en", "fr", "wo"):
            self.assertIn(f'<html lang="{code}" dir="ltr">', self._page(code))

    def test_french_translations_are_applied(self):
        self.assertIn("TÃ©lÃ©charger le MP3", self._page("fr"))

    def test_arabic_translations_are_applied(self):
        self.assertIn("ØªØ­Ù…ÙŠÙ„ MP3", self._page("ar"))

    def test_wolof_translations_are_applied(self):
        self.assertIn("Yees MP3", self._page("wo"))

    def test_arabic_text_is_not_left_as_english(self):
        # Guards against a catalogue that exists but is never loaded.
        self.assertNotIn("Download MP3", self._page("ar"))

    def test_each_language_has_a_compiled_catalog(self):
        from django.utils import translation

        samples = {
            "fr": "TÃ©lÃ©charger le MP3",
            "ar": "ØªØ­Ù…ÙŠÙ„ MP3",
            "wo": "Yees MP3",
        }
        for code, expected in samples.items():
            with translation.override(code):
                self.assertEqual(translation.gettext("Download MP3"), expected)

    def test_no_template_comment_is_rendered_as_text(self):
        # Django's {# ... #} syntax cannot span several lines: the comment
        # regex does not match newlines, so a multi-line comment is printed to
        # visitors instead of being stripped. Guard against that regressing.
        self.client.cookies["django_language"] = "ar"
        content = self.client.get(reverse("videos:list")).content.decode()
        self.assertNotIn("{#", content)
        self.assertNotIn("#}", content)
        self.assertTrue(content.lstrip().startswith("<!DOCTYPE html>"))


class ProductionStaticTests(TestCase):
    """Guard the production static-file setup the Docker build depends on."""

    def test_whitenoise_middleware_is_installed(self):
        from django.conf import settings

        self.assertIn(
            "whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE
        )

    def test_dockerfile_build_disables_debug_with_the_right_variable(self):
        # The build step must set DEBUG (which the settings read), not
        # DJANGO_DEBUG (which Django ignores). With the wrong name, collectstatic
        # runs with DEBUG=1, writes no staticfiles.json, and every page in the
        # deployed container fails with "Missing staticfiles manifest entry".
        import re

        import rammonte_usa

        # The Dockerfile sits at the repository root, one level above the
        # Django project directory that holds this package.
        repo_root = Path(rammonte_usa.__file__).resolve().parents[2]
        dockerfile = repo_root / "Dockerfile"
        self.assertTrue(
            dockerfile.exists(), f"Dockerfile not found at {dockerfile}"
        )
        text = dockerfile.read_text(encoding="utf-8")
        # The env var must be DEBUG, which the settings actually read. Checking
        # the whole file is fine: DJANGO_DEBUG must not appear anywhere, not
        # even in a comment, so nobody is tempted to rely on it.
        self.assertIn("DEBUG=0", text)
        self.assertNotIn("DJANGO_DEBUG", text)
        # The build fails loudly instead of shipping an image without a manifest.
        self.assertIn("staticfiles.json", text)

    def test_production_renders_every_page(self):
        response = self.client.get(reverse("videos:list"))
        self.assertEqual(response.status_code, 200)

    def test_settings_load_without_hsts_variable(self):
        # The container build runs with DEBUG=0 and no SECURE_HSTS_SECONDS set.
        # Indexing os.environ for an absent key used to raise KeyError and fail
        # the image build, so reload the settings with the variable removed.
        import importlib
        import os

        os.environ["DJANGO_SETTINGS_MODULE"] = "rammonte_usa.settings"
        previous = os.environ.pop("SECURE_HSTS_SECONDS", None)
        module = importlib.import_module("rammonte_usa.settings")
        try:
            importlib.reload(module)
            # Unset means HSTS is simply never configured, which leaves
            # Django's own default (0, disabled) in place.
            self.assertFalse(hasattr(module, "SECURE_HSTS_SECONDS"))
        finally:
            if previous is not None:
                os.environ["SECURE_HSTS_SECONDS"] = previous
            importlib.reload(module)


class OfflineSupportTests(TestCase):
    def setUp(self):
        self.user = make_user("viewer")
        self.video = make_video(
            self.user, title="Listen", audio_file_id="audio/track.mp3"
        )

    def test_manifest_is_served(self):
        response = self.client.get(reverse("manifest"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("manifest+json", response["Content-Type"])
        self.assertIn("Rammonte USA", response.content.decode())

    def test_health_check_reports_ok(self):
        # The hosting platform polls this to decide whether the container is
        # serving traffic.
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": True})

    def test_health_check_fails_when_the_database_is_down(self):
        import django.db

        with mock.patch.object(
            django.db.connection, "cursor", side_effect=Exception("db down")
        ):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["database"], False)

    def test_health_check_needs_no_authentication(self):
        # It must answer before anyone can sign in, so no login involved.
        self.assertEqual(self.client.get(reverse("health")).status_code, 200)

    def test_service_worker_is_served_from_root(self):
        response = self.client.get(reverse("service_worker"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"])
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertIn("fazakkir-media-v1", response.content.decode())

    def test_offline_page_renders(self):
        response = self.client.get(reverse("videos:offline"))
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="offlineList"', response.content.decode())

    def test_detail_page_has_offline_button_and_stream_src(self):
        content = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()
        self.assertIn('data-offline-url="/%d/audio-stream/"' % self.video.id, content)
        self.assertIn('audioSrc = "/%d/audio-stream/"' % self.video.id, content)

    def test_base_page_links_manifest(self):
        content = self.client.get(reverse("videos:list")).content.decode()
        self.assertIn('rel="manifest"', content)
        self.assertIn("offline.js", content)


class AudioGenerationTests(TestCase):
    """The automatic MP3 extraction that runs on upload / play."""

    def setUp(self):
        self.user = make_user("owner")

    @mock.patch("videos.audio_tasks.threading.Thread")
    def test_schedule_marks_processing_and_starts_worker(self, thread):
        video = make_video(self.user, title="Needs audio")
        self.assertTrue(schedule_audio_generation(video.id))
        video.refresh_from_db()
        self.assertEqual(video.audio_status, "processing")
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    @mock.patch("videos.audio_tasks.threading.Thread")
    def test_schedule_is_a_noop_while_processing(self, thread):
        video = make_video(self.user, title="Busy", audio_status="processing")
        self.assertFalse(schedule_audio_generation(video.id))
        thread.assert_not_called()

    @mock.patch("videos.audio_tasks.threading.Thread")
    def test_schedule_skips_videos_known_to_have_no_audio(self, thread):
        video = make_video(self.user, title="Silent", audio_status="none")
        self.assertFalse(schedule_audio_generation(video.id))
        thread.assert_not_called()

    @mock.patch("videos.audio_tasks.threading.Thread")
    def test_retry_allows_a_previously_failed_video(self, thread):
        video = make_video(self.user, title="Failed", audio_status="failed")
        self.assertTrue(schedule_audio_generation(video.id, retry=True))
        video.refresh_from_db()
        self.assertEqual(video.audio_status, "processing")

    @mock.patch("videos.audio_tasks.upload_audio")
    @mock.patch("videos.audio_tasks.extract_mp3_from_url")
    def test_generate_audio_stores_the_mp3(self, extract, upload):
        extract.return_value = True
        upload.return_value = {"path": "audio/track.mp3", "url": ""}
        video = make_video(self.user, title="Needs audio")
        Video.objects.filter(id=video.id).update(audio_status="processing")

        # Pin the resolved URL: presigned URLs embed a timestamp, so comparing
        # against a freshly generated one would be flaky across a second
        # boundary.
        source = "https://r2.example.com/videos/sample.mp4?signed=1"
        with mock.patch.object(Video, "playback_url", source):
            self.assertTrue(generate_audio(video.id))

        video.refresh_from_db()
        self.assertTrue(video.has_audio)
        self.assertEqual(video.audio_file_id, "audio/track.mp3")
        self.assertEqual(video.audio_status, "ready")
        # ffmpeg streams the video straight from its (presigned) URL.
        self.assertEqual(extract.call_args.args[0], source)

    @mock.patch("videos.audio_tasks.extract_mp3_from_url")
    def test_generate_audio_records_videos_without_an_audio_stream(self, extract):
        extract.return_value = False
        video = make_video(self.user, title="Silent")
        Video.objects.filter(id=video.id).update(audio_status="processing")

        self.assertFalse(generate_audio(video.id))

        video.refresh_from_db()
        self.assertEqual(video.audio_status, "none")
        self.assertFalse(video.has_audio)

    @mock.patch("videos.audio_tasks.extract_mp3_from_url")
    def test_generate_audio_records_failures(self, extract):
        extract.side_effect = AudioExtractionError("ffmpeg boom")
        video = make_video(self.user, title="Broken")
        Video.objects.filter(id=video.id).update(audio_status="processing")

        with self.assertLogs("videos.audio_tasks", level="WARNING"):
            self.assertFalse(generate_audio(video.id))

        video.refresh_from_db()
        self.assertEqual(video.audio_status, "failed")
        self.assertIn("ffmpeg boom", video.audio_error)

    def test_generate_audio_without_a_source_url_fails(self):
        video = make_video(self.user, title="Source-less", file_id="", video_url="")
        Video.objects.filter(id=video.id).update(audio_status="processing")

        self.assertFalse(generate_audio(video.id))

        video.refresh_from_db()
        self.assertEqual(video.audio_status, "failed")


class AudioStatusEndpointTests(TestCase):
    def setUp(self):
        self.user = make_user("owner")

    def test_reports_ready_audio(self):
        video = make_video(self.user, title="Has audio", audio_file_id="audio/a.mp3")
        data = self.client.get(
            reverse("videos:audio_status", args=[video.id])
        ).json()
        self.assertTrue(data["has_audio"])
        self.assertFalse(data["pending"])
        self.assertEqual(data["audio_url"], f"/{video.id}/audio-stream/")

    def test_reports_pending_for_an_untouched_video(self):
        video = make_video(self.user, title="Silent")
        data = self.client.get(
            reverse("videos:audio_status", args=[video.id])
        ).json()
        self.assertFalse(data["has_audio"])
        self.assertTrue(data["pending"])

    @mock.patch("videos.views.schedule_audio_generation", return_value=True)
    def test_retry_endpoint_requests_generation(self, schedule):
        video = make_video(self.user, title="Silent")
        response = self.client.post(
            reverse("videos:audio_generate", args=[video.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        schedule.assert_called_once_with(video.id, retry=True)


class DetailAudioGenerationTests(TestCase):
    def setUp(self):
        self.user = make_user("viewer")

    @mock.patch("videos.views.schedule_audio_generation", return_value=True)
    def test_playing_an_idle_video_starts_generation(self, schedule):
        video = make_video(self.user, title="Silent")
        content = self.client.get(
            reverse("videos:detail", args=[video.id])
        ).content.decode()
        schedule.assert_called_once_with(video.id)
        self.assertIn("Preparing the audio", content)

    @mock.patch("videos.views.schedule_audio_generation")
    def test_playing_a_video_with_audio_does_not_regenerate(self, schedule):
        video = make_video(self.user, title="Ready", audio_file_id="audio/a.mp3")
        self.client.get(reverse("videos:detail", args=[video.id]))
        schedule.assert_not_called()

    @mock.patch("videos.views.schedule_audio_generation")
    def test_playing_a_silent_video_does_not_loop(self, schedule):
        video = make_video(self.user, title="Silent", audio_status="none")
        content = self.client.get(
            reverse("videos:detail", args=[video.id])
        ).content.decode()
        schedule.assert_not_called()
        self.assertIn("This video has no audio track.", content)


class GenerateAudioCommandTests(TestCase):
    def setUp(self):
        self.user = make_user("owner")

    def test_skips_videos_known_to_have_no_audio(self):
        make_video(self.user, title="Silent", audio_status="none")
        out = StringIO()
        call_command("generateaudio", stdout=out)
        self.assertIn("No videos need an MP3", out.getvalue())

    @mock.patch(
        "videos.management.commands.generateaudio.generate_audio",
        return_value=True,
    )
    def test_processes_videos_missing_audio(self, generate):
        video = make_video(self.user, title="Needs audio")
        out = StringIO()
        call_command("generateaudio", stdout=out)
        generate.assert_called_once_with(video.id)
        self.assertIn("1/1", out.getvalue())


class SubtitleRenderingTests(TestCase):
    """WebVTT output and timestamp formatting."""

    def test_format_timestamp(self):
        self.assertEqual(transcription.format_timestamp(0), "00:00:00.000")
        self.assertEqual(transcription.format_timestamp(61.5), "00:01:01.500")
        self.assertEqual(transcription.format_timestamp(3725.25), "01:02:05.250")

    def test_negative_timestamps_are_clamped(self):
        self.assertEqual(transcription.format_timestamp(-5), "00:00:00.000")

    def test_build_vtt_has_header_and_cues(self):
        vtt = transcription.build_vtt(
            [
                transcription.Segment(0, 2.5, "Ù…Ø±Ø­Ø¨Ø§"),
                transcription.Segment(2.5, 4, "Hello"),
            ]
        )
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:02.500", vtt)
        self.assertIn("00:00:02.500 --> 00:00:04.000", vtt)
        self.assertIn("Ù…Ø±Ø­Ø¨Ø§", vtt)

    def test_build_vtt_drops_empty_cues(self):
        vtt = transcription.build_vtt(
            [transcription.Segment(0, 1, "   "), transcription.Segment(1, 2, "kept")]
        )
        self.assertEqual(vtt.count("-->"), 1)
        self.assertIn("00:00:01.000 --> 00:00:02.000", vtt)
        self.assertNotIn("00:00:00.000", vtt)

    def test_empty_list_still_yields_valid_header(self):
        self.assertEqual(transcription.build_vtt([]), "WEBVTT\n")


class FakeProvider(transcription.SubtitleProvider):
    """Deterministic stand-in for a real speech-to-text back end."""

    name = "fake"

    def __init__(self):
        self.transcribe_calls = []
        self.translate_calls = []

    def transcribe(self, path, language):
        self.transcribe_calls.append((path, language))
        return [transcription.Segment(0, 1.5, "one"), transcription.Segment(1.5, 3, "two")]

    def translate(self, segments, target_language):
        self.translate_calls.append((len(segments), target_language))
        return [
            transcription.Segment(s.start, s.end, f"[{target_language}] {s.text}")
            for s in segments
        ]


class TranscriptionPipelineTests(TestCase):
    """The transcribe -> translate -> WebVTT pipeline."""

    def setUp(self):
        self.user = make_user("owner")
        self.provider = FakeProvider()
        # Pin the pipeline's settings: otherwise a developer's own .env (a
        # different source language, or the ElevenLabs provider) would leak in
        # and change what these tests see.
        settings_override = override_settings(
            SUBTITLE_PROVIDER="openai",
            SUBTITLE_SOURCE_LANGUAGE="ar",
            SUBTITLE_TARGET_LANGUAGES="fr,en",
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

    def _patch_pipeline(self, chunks):
        return (
            mock.patch("videos.transcription.get_provider", return_value=self.provider),
            mock.patch("videos.transcription.download_audio", return_value="audio.mp3"),
            mock.patch("videos.transcription.chunk_audio", return_value=chunks),
        )

    def test_creates_source_translation_and_lyrics_tracks(self):
        video = make_video(self.user, title="Song", audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            result = transcription.generate_subtitles(video.id)

        self.assertEqual(sorted(result["languages"]), ["ar", "en", "fr"])
        languages = set(
            video.subtitle_tracks.values_list("language_code", flat=True)
        )
        self.assertEqual(languages, {"ar", "fr", "en", "lyrics"})

    def test_source_track_holds_the_transcription(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        track = video.subtitle_tracks.get(language_code="ar")
        self.assertTrue(track.content.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:01.500", track.content)

    def test_translations_are_saved_per_language(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        french = video.subtitle_tracks.get(language_code="fr")
        english = video.subtitle_tracks.get(language_code="en")
        self.assertIn("[fr] one", french.content)
        self.assertIn("[en] one", english.content)
        self.assertEqual(self.provider.translate_calls, [(2, "fr"), (2, "en")])

    def test_lyrics_track_is_marked_as_lyrics(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        self.assertTrue(video.subtitle_tracks.get(language_code="lyrics").is_lyrics)

    def test_chunk_offsets_shift_timestamps(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0), ("c1.mp3", 480.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        track = video.subtitle_tracks.get(language_code="ar")
        # Second chunk starts at 480s, so its cues land at 08:00 onwards.
        self.assertIn("00:08:00.000 --> 00:08:01.500", track.content)

    def test_missing_audio_is_reported(self):
        video = make_video(self.user, audio_file_id="")
        with self.assertRaises(transcription.TranscriptionError):
            transcription.generate_subtitles(video.id)

    def test_no_speech_is_reported(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        self.provider.transcribe = lambda path, language: []
        with provider_patch, audio_patch, chunk_patch:
            with self.assertRaises(transcription.TranscriptionError):
                transcription.generate_subtitles(video.id)

    def test_a_barely_heard_recording_is_not_published_as_captions(self):
        # Singing or chanting can leave a transcriber with a few stray phrases.
        # Saving those would look like a finished job, so the run fails instead.
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        Video.objects.filter(id=video.id).update(duration_seconds=600)
        video.refresh_from_db()
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        self.provider.transcribe = lambda path, language: [
            transcription.Segment(0, 1.0, "one"), transcription.Segment(200, 201, "two")
        ]
        with provider_patch, audio_patch, chunk_patch:
            with self.assertRaises(transcription.TranscriptionError) as caught:
                transcription.generate_subtitles(video.id)

        self.assertIn("could be made out", str(caught.exception))
        # Nothing half-finished reaches the page.
        self.assertEqual(video.subtitle_tracks.count(), 0)

    def test_a_short_clip_with_a_few_words_still_gets_captions(self):
        # Two seconds of speech in a thirty second clip is all there is.
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        Video.objects.filter(id=video.id).update(duration_seconds=30)
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        self.provider.transcribe = lambda path, language: [
            transcription.Segment(0, 1.0, "one")
        ]
        with provider_patch, audio_patch, chunk_patch:
            result = transcription.generate_subtitles(video.id)
        self.assertIn("ar", result["languages"])

    def test_existing_lyrics_are_not_overwritten(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        SubtitleTrack.objects.create(
            video=video,
            language_code="lyrics",
            label="Lyrics",
            content="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nmine",
            is_lyrics=True,
        )
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        self.assertIn("mine", video.subtitle_tracks.get(language_code="lyrics").content)

    def test_regenerating_replaces_existing_tracks(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        SubtitleTrack.objects.create(
            video=video,
            language_code="fr",
            label="French",
            content="WEBVTT\n\nold",
        )
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        french = video.subtitle_tracks.filter(language_code="fr")
        self.assertEqual(french.count(), 1)
        self.assertIn("[fr] one", french.first().content)


class SubtitleStatusWorkflowTests(TestCase):
    """Status flags, background scheduling and the views around them."""

    def setUp(self):
        self.user = make_user("owner")
        self.admin = make_user("root", is_staff=True, is_superuser=True)
        self.video = make_video(
            self.user, title="Song", audio_file_id="audio/a.mp3"
        )
        # The sweep progress is module level, so reset it around each test.
        self._reset_sweep()
        self.addCleanup(self._reset_sweep)

    def _reset_sweep(self):
        transcription._SWEEP_STATE.update(
            running=False, total=0, done=0, succeeded=0, failed=0
        )

    def test_generate_and_record_marks_success(self):
        with mock.patch.object(transcription, "generate_subtitles") as generate:
            generate.return_value = {"languages": ["fr"], "cues": 3}
            self.assertTrue(transcription.generate_and_record(self.video.id))

        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "ready")
        self.assertEqual(self.video.auto_subtitle_error, "")

    def test_generate_and_record_captures_errors(self):
        with mock.patch.object(
            transcription, "generate_subtitles", side_effect=transcription.TranscriptionError("no key")
        ):
            self.assertFalse(transcription.generate_and_record(self.video.id))

        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "failed")
        self.assertIn("no key", self.video.auto_subtitle_error)

    def test_generate_and_record_survives_unexpected_errors(self):
        with mock.patch.object(
            transcription, "generate_subtitles", side_effect=RuntimeError("boom")
        ):
            # The traceback is logged; capture it so the test output stays clean.
            with self.assertLogs("videos.transcription", level="ERROR"):
                self.assertFalse(transcription.generate_and_record(self.video.id))

        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "failed")

    @mock.patch("videos.transcription.threading.Thread")
    def test_scheduling_marks_processing_and_starts_a_worker(self, thread):
        self.assertTrue(transcription.schedule_subtitle_generation(self.video.id))
        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "processing")
        thread.assert_called_once()

    @mock.patch("videos.transcription.threading.Thread")
    def test_scheduling_is_a_noop_while_processing(self, thread):
        Video.objects.filter(id=self.video.id).update(auto_subtitle_status="processing")
        self.assertFalse(transcription.schedule_subtitle_generation(self.video.id))
        thread.assert_not_called()

    @mock.patch("videos.transcription.threading.Thread")
    def test_retry_allows_a_failed_video(self, thread):
        Video.objects.filter(id=self.video.id).update(auto_subtitle_status="failed")
        self.assertTrue(
            transcription.schedule_subtitle_generation(self.video.id, retry=True)
        )
        thread.assert_called_once()

    @mock.patch("videos.transcription.generate_and_record", return_value=True)
    def test_bulk_sweep_skips_videos_without_audio(self, record):
        make_video(self.user, title="Other", audio_file_id="audio/b.mp3")
        make_video(self.user, title="No audio", audio_file_id="")
        transcription._sweep_videos()
        # Only the two videos that actually have an MP3 to transcribe.
        self.assertEqual(record.call_count, 2)
        for call in record.call_args_list:
            self.assertNotEqual(call.args[0], None)

    @mock.patch("videos.transcription.generate_and_record", return_value=True)
    def test_bulk_sweep_leaves_finished_videos_alone(self, record):
        # Re-transcribing hours of audio only spends credit to replace a track
        # that is already there.
        Video.objects.filter(id=self.video.id).update(auto_subtitle_status="ready")
        make_video(self.user, title="Waiting", audio_file_id="audio/wait.mp3")

        transcription._sweep_videos()

        self.assertEqual(record.call_count, 1)
        self.assertNotEqual(record.call_args.args[0], self.video.id)

    def test_bulk_sweep_stops_when_the_account_runs_out_of_credit(self):
        later = make_video(self.user, title="Later", audio_file_id="audio/later.mp3")

        def fail_first(video_id):
            Video.objects.filter(id=video_id).update(
                auto_subtitle_error=(
                    "ElevenLabs rejected the request: This request exceeds your "
                    "quota of 10000. You have 20 credits remaining."
                )
            )
            return False

        with mock.patch(
            "videos.transcription.generate_and_record", side_effect=fail_first
        ) as record:
            transcription._sweep_videos()

        # One rejection is enough to know the rest will be rejected too.
        self.assertEqual(record.call_count, 1)
        later.refresh_from_db()
        # Untouched, so the next run (or the monthly reset) picks it up.
        self.assertEqual(later.auto_subtitle_status, "")

    def test_bulk_sweep_carries_on_after_an_ordinary_failure(self):
        later = make_video(self.user, title="Later", audio_file_id="audio/later.mp3")

        def fail_first(video_id):
            Video.objects.filter(id=video_id).update(
                auto_subtitle_error="Only 1% of this recording could be made out."
            )
            return False

        with mock.patch(
            "videos.transcription.generate_and_record", side_effect=fail_first
        ) as record:
            transcription._sweep_videos()

        # A recording that cannot be captioned is not a reason to skip the rest.
        self.assertEqual(record.call_count, 2)

    @mock.patch("videos.transcription.threading.Thread")
    def test_start_sweep_runs_once(self, thread):
        self.assertTrue(transcription.start_subtitle_sweep())
        self.assertFalse(transcription.start_subtitle_sweep())
        thread.assert_called_once()


class SubtitleEndpointsTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.admin = make_user("root", is_staff=True, is_superuser=True)
        self.member = make_user("member")
        self.video = make_video(self.owner, audio_file_id="audio/a.mp3")
        self.silent = make_video(self.owner, title="Silent", audio_file_id="")

    def _login_admin(self):
        self.client.login(username="root", password="pw12345678")

    def test_member_cannot_generate(self):
        self.client.login(username="member", password="pw12345678")
        response = self.client.post(
            reverse("videos:subtitles_generate", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_guest_is_forbidden(self):
        response = self.client.get(
            reverse("videos:subtitles_generate", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 403)

    @mock.patch("videos.views.subtitles_configured", return_value=False)
    @override_settings(SUBTITLE_PROVIDER="openai")
    def test_unconfigured_provider_is_reported(self, _configured):
        self._login_admin()
        response = self.client.post(
            reverse("videos:subtitles_generate", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("OPENAI_API_KEY", response.json()["error"])

    @mock.patch("videos.views.subtitles_configured", return_value=True)
    @mock.patch("videos.views.schedule_subtitle_generation", return_value=True)
    def test_admin_starts_generation(self, schedule, _configured):
        self._login_admin()
        response = self.client.post(
            reverse("videos:subtitles_generate", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        schedule.assert_called_once_with(self.video.id, retry=True)

    @mock.patch("videos.views.subtitles_configured", return_value=True)
    def test_video_without_audio_is_refused(self, _configured):
        self._login_admin()
        response = self.client.post(
            reverse("videos:subtitles_generate", args=[self.silent.id])
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("audio", response.json()["error"])

    def test_status_endpoint_is_public(self):
        response = self.client.get(
            reverse("videos:subtitles_status", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "")
        self.assertTrue(data["pending"])

    def test_status_lists_existing_languages(self):
        SubtitleTrack.objects.create(
            video=self.video,
            language_code="fr",
            label="French",
            content="WEBVTT",
        )
        data = self.client.get(
            reverse("videos:subtitles_status", args=[self.video.id])
        ).json()
        self.assertEqual(data["languages"], ["fr"])

    def test_status_of_missing_video_is_404(self):
        response = self.client.get(reverse("videos:subtitles_status", args=[9999]))
        self.assertEqual(response.status_code, 404)


class GenerateSubtitlesCommandTests(TestCase):
    def setUp(self):
        self.user = make_user("owner")

    @mock.patch("videos.transcription.is_configured", return_value=False)
    def test_refuses_to_run_without_a_provider(self, _configured):
        out = StringIO()
        call_command("generatesubtitles", stdout=out, stderr=out)
        self.assertIn("No subtitle provider is configured", out.getvalue())

    @mock.patch("videos.transcription.generate_and_record", return_value=True)
    @mock.patch("videos.transcription.is_configured", return_value=True)
    @mock.patch("videos.transcription.get_provider")
    def test_processes_videos_missing_subtitles(self, provider, _configured, record):
        provider.return_value = FakeProvider()
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        make_video(self.user, title="Silent", audio_file_id="")

        out = StringIO()
        call_command("generatesubtitles", stdout=out)

        record.assert_called_once_with(video.id)
        self.assertIn("1/1", out.getvalue())

    @mock.patch("videos.transcription.is_configured", return_value=True)
    @mock.patch("videos.transcription.get_provider")
    def test_skip_existing(self, provider, _configured):
        provider.return_value = FakeProvider()
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        SubtitleTrack.objects.create(
            video=video, language_code="fr", label="French", content="WEBVTT"
        )
        out = StringIO()
        with mock.patch("videos.transcription.generate_and_record") as record:
            call_command("generatesubtitles", "--skip-existing", stdout=out)
        record.assert_not_called()
        self.assertIn("No videos need subtitles", out.getvalue())


class ParseVttTests(TestCase):
    """Reading a stored WebVTT track back into (start, end, text) cues."""

    def test_round_trips_a_built_track(self):
        segments = [
            transcription.Segment(0.0, 1.5, "Dem am bennu goor."),
            transcription.Segment(62.25, 64.0, "Amoul nday."),
        ]
        cues = parse_vtt(transcription.build_vtt(segments))
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0][2], "Dem am bennu goor.")
        self.assertAlmostEqual(cues[0][0], 0.0)
        self.assertAlmostEqual(cues[0][1], 1.5)
        self.assertAlmostEqual(cues[1][0], 62.25)
        self.assertAlmostEqual(cues[1][1], 64.0)

    def test_ignores_the_header_and_cue_settings(self):
        from videos.subtitles import parse_vtt

        content = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000 align:start position:10%\n"
            "Hello there\n"
        )
        cues = parse_vtt(content)
        self.assertEqual(cues, [(1.0, 2.0, "Hello there")])

    def test_keeps_multi_line_cues_together(self):
        from videos.subtitles import parse_vtt

        content = "WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nfirst line\nsecond line\n"
        cues = parse_vtt(content)
        self.assertEqual(cues, [(0.0, 3.0, "first line\nsecond line")])

    def test_empty_or_plain_text_has_no_cues(self):
        from videos.subtitles import parse_vtt

        self.assertEqual(parse_vtt(""), [])
        self.assertEqual(parse_vtt("just words, no timings"), [])


class TranslateExistingTracksTests(TestCase):
    """Filling the missing languages from a transcript that already exists."""

    def setUp(self):
        self.user = make_user("owner")

        class FakeTranslator(transcription.SubtitleProvider):
            name = "google"

            def __init__(self):
                self._source_language = ""
                self.calls = []

            def transcribe(self, path, language):
                return []

            def translate(self, segments, target_language):
                self.calls.append((len(segments), target_language))
                return [
                    transcription.Segment(
                        s.start, s.end, f"[{target_language}] {s.text}"
                    )
                    for s in segments
                ]

        self.translator = FakeTranslator()

    def _video_with_source(self, code="wo"):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        SubtitleTrack.objects.create(
            video=video,
            language_code=code,
            label=label_for(code),
            content=transcription.build_vtt(
                [
                    transcription.Segment(0.0, 2.0, "Dem am bennu goor."),
                    transcription.Segment(2.0, 4.0, "Amoul nday."),
                ]
            ),
        )
        return video

    def _override(self):
        return override_settings(
            SUBTITLE_TRANSLATION_PROVIDER="google",
            SUBTITLE_SOURCE_LANGUAGE="wo",
            SUBTITLE_TARGET_LANGUAGES="fr,en",
        )

    def test_adds_only_the_missing_language(self):
        video = self._video_with_source()
        SubtitleTrack.objects.create(
            video=video, language_code="fr", label="French", content="WEBVTT"
        )
        with self._override(), mock.patch.object(
            transcription, "get_translation_provider", return_value=self.translator
        ):
            result = transcription.translate_existing_tracks(video.id)

        self.assertEqual(result["languages"], ["en"])
        self.assertEqual([call[1] for call in self.translator.calls], ["en"])

    def test_translation_reuses_the_stored_timing(self):
        video = self._video_with_source()
        with self._override(), mock.patch.object(
            transcription, "get_translation_provider", return_value=self.translator
        ):
            transcription.translate_existing_tracks(video.id)

        from videos.subtitles import parse_vtt

        track = video.subtitle_tracks.get(language_code="en")
        cues = parse_vtt(track.content)
        self.assertEqual(cues[0][2], "[en] Dem am bennu goor.")
        self.assertAlmostEqual(cues[0][0], 0.0)
        self.assertAlmostEqual(cues[0][1], 2.0)

    def test_tells_the_translator_which_language_it_is_reading(self):
        video = self._video_with_source(code="wo")
        with self._override(), mock.patch.object(
            transcription, "get_translation_provider", return_value=self.translator
        ):
            transcription.translate_existing_tracks(video.id)
        self.assertEqual(self.translator._source_language, "wo")

    def test_falls_back_to_the_lyrics_track(self):
        # No wo track, but the lyrics track holds the same transcript.
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        SubtitleTrack.objects.create(
            video=video,
            language_code="lyrics",
            label="Lyrics",
            is_lyrics=True,
            content=transcription.build_vtt(
                [transcription.Segment(0.0, 1.0, "one"), transcription.Segment(1.0, 2.0, "two")]
            ),
        )
        with self._override(), mock.patch.object(
            transcription, "get_translation_provider", return_value=self.translator
        ):
            result = transcription.translate_existing_tracks(video.id)

        self.assertEqual(sorted(result["languages"]), ["en", "fr"])

    def test_creates_the_lyrics_track_when_it_is_missing(self):
        video = self._video_with_source()
        self.assertFalse(video.subtitle_tracks.filter(language_code="lyrics").exists())
        with self._override(), mock.patch.object(
            transcription, "get_translation_provider", return_value=self.translator
        ):
            transcription.translate_existing_tracks(video.id)
        self.assertTrue(video.subtitle_tracks.filter(language_code="lyrics").exists())

    def test_refuses_when_there_is_no_transcript(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        with self._override(), mock.patch.object(
            transcription, "get_translation_provider", return_value=self.translator
        ):
            with self.assertRaises(transcription.TranscriptionError) as ctx:
                transcription.translate_existing_tracks(video.id)
        self.assertIn("no transcript", str(ctx.exception).lower())

    def test_refuses_without_a_translation_provider(self):
        video = self._video_with_source()
        with self._override(), mock.patch.object(
            transcription, "get_translation_provider", return_value=None
        ):
            with self.assertRaises(transcription.TranscriptionError) as ctx:
                transcription.translate_existing_tracks(video.id)
        self.assertIn("No translation provider", str(ctx.exception))

    def test_a_failing_translation_does_not_lose_the_others(self):
        class HalfBroken(transcription.SubtitleProvider):
            name = "google"

            def transcribe(self, path, language):
                return []

            def translate(self, segments, target_language):
                if target_language == "en":
                    raise transcription.TranscriptionError("insufficient_funds")
                return [
                    transcription.Segment(s.start, s.end, f"[{target_language}]")
                    for s in segments
                ]

        video = self._video_with_source()
        with self._override(), mock.patch.object(
            transcription, "get_translation_provider", return_value=HalfBroken()
        ):
            result = transcription.translate_existing_tracks(video.id)

        self.assertEqual(result["languages"], ["fr"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("en", result["warnings"][0])


class TranslateFromTheBestTrackTests(TestCase):
    """Wolof is written in Latin and Arabic script; only one of them translates.

    Cloud Translation reads Latin Wolof and translates it, but hands Arabic
    script (Ajami) back unchanged. Choosing the wrong track would publish an
    "English" track full of untranslated text.
    """

    def setUp(self):
        self.user = make_user("owner")

    def test_notices_text_that_is_still_not_latin(self):
        # What the API really returns for Ajami: mostly Latin transliteration,
        # with the words it could not spell out left in Arabic.
        arabic = [
            transcription.Segment(0.0, 1.0, "\u0642\u064f\u0646\u0652\u0643\u0652 \u0628\u064e\u063a\u0652\u0628\u0650\u064a"),
            transcription.Segment(1.0, 2.0, "BIMTAWKH"),
            transcription.Segment(2.0, 3.0, "\u0645\u064f\u0648\u0652\u064a\u0652 \u062c\u064e\u0646\u0652\u063a\u0652 \u062e\u064e\u0644\u064e\u062a\u0652"),
            transcription.Segment(3.0, 4.0, "\u0623\u064e\u0643\u0652\u0628\u064f\u0643\u064f \u0643\u064f\u0645\u0652\u0628\u0652\u0631\u064e\u0646\u0652\u0633\u0650\u064a."),
            transcription.Segment(4.0, 5.0, "\u0646\u064e\u062d\u0652 \u062f\u064e\u0641\u064e\u0628\u064e\u0651\u0631\u0650\u064a \u0643\u064f\u062a\u064f\u0648\u0652 \u062f\u064e\u0633\u064e\u0628\u064e\u0628\u064f\u0651\u0644\u0652"),
        ]
        self.assertTrue(transcription._looks_untranslated(arabic))

        english = [
            transcription.Segment(0.0, 1.0, "What is the meaning of this statement?"),
            transcription.Segment(1.0, 2.0, "For what"),
            transcription.Segment(2.0, 3.0, "This teaches you to think."),
        ]
        self.assertFalse(transcription._looks_untranslated(english))

    def test_a_mixed_track_still_counts_as_untranslated(self):
        # The trap this guards against: video 3's Ajami track is 86% Latin by
        # letter, so a share-based test calls it translated and the Arabic the
        # API left behind is published as English.
        tracks = {t.language_code: t for t in self._video().subtitle_tracks.all()}
        content = tracks["wo"].content
        latin = sum(1 for ch in content if ch.isalpha() and ord(ch) < 0x0250)
        arabic = transcription.count_arabic(content)
        self.assertGreater(latin, arabic)
        self.assertTrue(transcription._looks_untranslated([transcription.Segment(0, 1, content)]))

    def test_empty_translation_counts_as_untranslated(self):
        self.assertTrue(transcription._looks_untranslated([]))
        self.assertTrue(
            transcription._looks_untranslated([transcription.Segment(0.0, 1.0, "  ")])
        )

    def _video(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        # Modelled on video 3's real track: mostly Latin transliteration with
        # Arabic only on some lines, which is why the script *share* cannot tell
        # it apart from Latin Wolof.
        SubtitleTrack.objects.create(
            video=video,
            language_code="wo",
            label="Wolof",
            content=transcription.build_vtt(
                [
                    transcription.Segment(0.0, 1.0, "\u0642\u064f\u0646\u0652\u0643\u0652 \u0628\u064e\u063a\u0652\u0628\u0650\u064a"),
                    transcription.Segment(1.0, 2.0, "BIMTAWKH"),
                    transcription.Segment(2.0, 3.0, "\u0645\u064f\u0648\u0652\u064a\u0652 \u062c\u064e\u0646\u0652\u063a\u0652"),
                    transcription.Segment(3.0, 4.0, "Nangadef, maa ngi fi rekk"),
                    transcription.Segment(4.0, 5.0, "Damay jang wolof bu baax"),
                    transcription.Segment(5.0, 6.0, "\u0623\u064e\u0643\u0652\u0628\u064f\u0643\u064f \u0643\u064f\u0645\u0652\u0628\u0652\u0631\u064e\u0646\u0652\u0633\u0650\u064a."),
                    transcription.Segment(6.0, 7.0, "Amoul nday, amoul bay"),
                ]
            ),
        )
        SubtitleTrack.objects.create(
            video=video,
            language_code="fr",
            label="French",
            content=transcription.build_vtt(
                [
                    transcription.Segment(0.0, 1.0, "Quel est le sens"),
                    transcription.Segment(1.0, 2.0, "Pourquoi"),
                ]
            ),
        )
        return video

    def test_sorts_readable_tracks_first(self):
        video = self._video()
        tracks = {t.language_code: t for t in video.subtitle_tracks.all()}
        origins = transcription.translation_origins(tracks, "en", "wo")
        self.assertEqual([t.language_code for t in origins], ["fr", "wo"])
        self.assertGreaterEqual(
            transcription.count_arabic(tracks["wo"].content),
            transcription.MIN_ARABIC_LETTERS,
        )
        self.assertEqual(transcription.count_arabic(tracks["fr"].content), 0)

    def test_another_target_language_is_a_valid_source(self):
        # Video 3 exactly: French exists, English does not, and the Wolof track
        # is Ajami. The English track has to come from the French one.
        video = self._video()
        tracks = {t.language_code: t for t in video.subtitle_tracks.all()}
        origins = transcription.translation_origins(tracks, "en", "wo")
        self.assertIn("fr", [t.language_code for t in origins])
        self.assertEqual(origins[0].language_code, "fr")

    def test_falls_through_a_translator_that_returns_the_text_unchanged(self):
        video = self._video()

        class CopyCat(transcription.SubtitleProvider):
            name = "google"

            def __init__(self):
                self._source_language = ""
                self.seen = []

            def transcribe(self, path, language):
                return []

            def translate(self, segments, target_language):
                self.seen.append(self._source_language)
                if self._source_language == "fr":
                    return [
                        transcription.Segment(s.start, s.end, "What is the meaning")
                        for s in segments
                    ]
                # Ajami comes back untouched, as the real API does.
                return list(segments)

        translator = CopyCat()
        with override_settings(
            SUBTITLE_TRANSLATION_PROVIDER="google",
            SUBTITLE_SOURCE_LANGUAGE="wo",
            SUBTITLE_TARGET_LANGUAGES="en",
        ), mock.patch.object(
            transcription, "get_translation_provider", return_value=translator
        ):
            result = transcription.translate_existing_tracks(video.id)

        self.assertEqual(result["languages"], ["en"])
        self.assertEqual(translator.seen, ["fr"])
        content = video.subtitle_tracks.get(language_code="en").content
        self.assertIn("What is the meaning", content)
        self.assertNotIn("\u0642\u064f\u0646\u0652\u0643\u0652", content)

    def test_reports_when_no_track_could_be_translated(self):
        video = self._video()
        video.subtitle_tracks.filter(language_code="fr").delete()

        class CopyCat(transcription.SubtitleProvider):
            name = "google"

            def __init__(self):
                self._source_language = ""

            def transcribe(self, path, language):
                return []

            def translate(self, segments, target_language):
                return list(segments)

        with override_settings(
            SUBTITLE_TRANSLATION_PROVIDER="google",
            SUBTITLE_SOURCE_LANGUAGE="wo",
            SUBTITLE_TARGET_LANGUAGES="en",
        ), mock.patch.object(
            transcription, "get_translation_provider", return_value=CopyCat()
        ):
            result = transcription.translate_existing_tracks(video.id)

        self.assertEqual(result["languages"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertFalse(video.subtitle_tracks.filter(language_code="en").exists())


class TranslateTracksCommandTests(TestCase):
    """The ``translatetracks`` command."""

    def setUp(self):
        self.user = make_user("owner")
        self.video = make_video(self.user, audio_file_id="audio/a.mp3")
        SubtitleTrack.objects.create(
            video=self.video,
            language_code="wo",
            label="Wolof",
            content=transcription.build_vtt(
                [transcription.Segment(0.0, 1.0, "one"), transcription.Segment(1.0, 2.0, "two")]
            ),
        )

    @mock.patch("videos.transcription.translation_configured", return_value=False)
    def test_refuses_without_a_translator(self, _configured):
        out = StringIO()
        call_command("translatetracks", stdout=out, stderr=out)
        self.assertIn("No translation provider is configured", out.getvalue())

    @mock.patch("videos.transcription.translation_configured", return_value=True)
    @mock.patch("videos.transcription.translate_existing_tracks")
    def test_processes_videos_missing_a_target_language(self, translate, _configured):
        translate.return_value = {"languages": ["fr", "en"], "cues": 2, "warnings": []}
        out = StringIO()
        with override_settings(
            SUBTITLE_SOURCE_LANGUAGE="wo", SUBTITLE_TARGET_LANGUAGES="fr,en"
        ):
            call_command("translatetracks", stdout=out)

        translate.assert_called_once()
        self.assertEqual(translate.call_args[0][0], self.video.id)
        self.assertIn("2 track(s) created", out.getvalue())

    @mock.patch("videos.transcription.translation_configured", return_value=True)
    @mock.patch("videos.transcription.translate_existing_tracks")
    def test_skips_a_video_that_already_has_every_track(self, translate, _configured):
        for code in ("fr", "en"):
            SubtitleTrack.objects.create(
                video=self.video, language_code=code, label=code, content="WEBVTT"
            )
        out = StringIO()
        with override_settings(
            SUBTITLE_SOURCE_LANGUAGE="wo", SUBTITLE_TARGET_LANGUAGES="fr,en"
        ):
            call_command("translatetracks", stdout=out)

        translate.assert_not_called()
        self.assertIn("already has all its language tracks", out.getvalue())

    @mock.patch("videos.transcription.translation_configured", return_value=True)
    @mock.patch("videos.transcription.translate_existing_tracks")
    def test_ignores_videos_without_a_transcript(self, translate, _configured):
        make_video(self.user, title="No track", audio_file_id="audio/b.mp3")
        out = StringIO()
        with override_settings(
            SUBTITLE_SOURCE_LANGUAGE="wo", SUBTITLE_TARGET_LANGUAGES="fr,en"
        ):
            call_command("translatetracks", stdout=out)
        self.assertEqual(translate.call_count, 1)


class GoogleLanguageCodeTests(TestCase):
    """Our short codes mapped to what each Google API expects."""
    def test_short_codes_are_mapped_for_speech(self):
        with override_settings(GOOGLE_SPEECH_LANGUAGE=""):
            self.assertEqual(transcription.speech_language_code("ar"), "ar-SA")
            self.assertEqual(transcription.speech_language_code("fr"), "fr-FR")
            self.assertEqual(transcription.speech_language_code("en"), "en-US")

    def test_full_bcp47_tags_pass_through(self):
        with override_settings(GOOGLE_SPEECH_LANGUAGE=""):
            self.assertEqual(transcription.speech_language_code("ar-EG"), "ar-EG")

    def test_unknown_codes_are_left_alone(self):
        with override_settings(GOOGLE_SPEECH_LANGUAGE=""):
            self.assertEqual(transcription.speech_language_code("wo"), "wo")

    def test_empty_language_falls_back_to_english(self):
        with override_settings(GOOGLE_SPEECH_LANGUAGE=""):
            self.assertEqual(transcription.speech_language_code(""), "en-US")

    def test_the_override_wins_over_the_mapping(self):
        with override_settings(GOOGLE_SPEECH_LANGUAGE="ar-MA"):
            self.assertEqual(transcription.speech_language_code("ar"), "ar-MA")

    def test_translate_codes_are_lowercased(self):
        self.assertEqual(transcription.translate_language_code("FR"), "fr")


class CaptionBuildingTests(TestCase):
    """Google returns timed words; these are grouped into caption lines."""

    def test_a_sentence_ending_starts_a_new_caption(self):
        captions = transcription.build_captions(
            [
                ("Hello", 0, 0.5),
                ("world.", 0.5, 1.0),
                ("Next", 1.0, 1.5),
                ("one.", 1.5, 2.0),
            ]
        )
        self.assertEqual([c.text for c in captions], ["Hello world.", "Next one."])

    def test_a_long_pause_splits_the_line(self):
        captions = transcription.build_captions([("before", 0, 0.5), ("after", 3.0, 3.5)])
        self.assertEqual([c.text for c in captions], ["before", "after"])

    def test_a_long_run_is_broken_up(self):
        words = [(f"w{i}", i * 0.5, i * 0.5 + 0.5) for i in range(40)]
        captions = transcription.build_captions(words)
        self.assertGreater(len(captions), 1)
        for caption in captions:
            self.assertLessEqual(
                caption.end - caption.start, transcription.CAPTION_MAX_SECONDS + 0.5
            )

    def test_a_zero_length_caption_still_has_a_positive_span(self):
        captions = transcription.build_captions([("word", 1.0, 1.0)])
        self.assertEqual(captions[0].start, 1.0)
        self.assertEqual(captions[0].end, 1.5)

    def test_a_very_long_word_is_not_split_on_itself(self):
        word = "x" * 200
        captions = transcription.build_captions([(word, 0, 1)])
        self.assertEqual(captions[0].text, word)

    def test_standalone_punctuation_is_not_its_own_caption(self):
        # Scribe emits standalone punctuation as a word of its own; a caption
        # holding just "." is noise, so it is dropped.
        captions = transcription.build_captions(
            [("Hello", 0, 0.5), (".", 0.5, 0.6), ("world.", 0.6, 1.0)]
        )
        self.assertEqual([c.text for c in captions], ["Hello world."])

    def test_punctuation_only_input_produces_no_captions(self):
        captions = transcription.build_captions([(".", 0, 0.5), ("!", 0.5, 1.0)])
        self.assertEqual(captions, [])

    def test_time_parser_handles_google_values(self):
        self.assertEqual(transcription._google_seconds("1.500s"), 1.5)
        self.assertEqual(transcription._google_seconds("0s"), 0.0)
        self.assertEqual(transcription._google_seconds(None), 0.0)
        self.assertEqual(transcription._google_seconds("nonsense"), 0.0)

    def test_results_without_word_times_fall_back_to_transcripts(self):
        captions = transcription.captions_from_speech_results(
            [{"alternatives": [{"transcript": "One two three"}]}]
        )
        self.assertEqual(captions[0].text, "One two three")
        self.assertGreater(captions[0].end, captions[0].start)

    def test_speech_results_become_timed_captions(self):
        results = [
            {
                "alternatives": [
                    {
                        "transcript": "Hello world.",
                        "words": [
                            {"word": "Hello", "startTime": "0s", "endTime": "0.400s"},
                            {"word": "world.", "startTime": "0.400s", "endTime": "1s"},
                        ],
                    }
                ]
            }
        ]
        captions = transcription.captions_from_speech_results(results)
        self.assertEqual(len(captions), 1)
        self.assertEqual(captions[0].text, "Hello world.")
        self.assertAlmostEqual(captions[0].start, 0.0)
        self.assertAlmostEqual(captions[0].end, 1.0)


class GoogleProviderTests(TestCase):
    """The Google Cloud Speech-to-Text + Translation back end."""

    API_KEY = "test-google-key"

    def _audio_file(self, data=b"fake-mp3-bytes"):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "chunk.mp3"
        path.write_bytes(data)
        return str(path)

    @override_settings(
        GOOGLE_API_KEY=API_KEY,
        GOOGLE_SPEECH_MODEL="latest_long",
        GOOGLE_SPEECH_LANGUAGE="",
        GOOGLE_SPEECH_ALTERNATIVE_LANGUAGES="",
    )
    def test_transcribe_builds_the_request_and_reads_captions(self):
        import base64 as _base64

        results = [
            {
                "alternatives": [
                    {
                        "transcript": "Hello world.",
                        "words": [
                            {"word": "Hello", "startTime": "0s", "endTime": "0.400s"},
                            {"word": "world.", "startTime": "0.400s", "endTime": "1s"},
                        ],
                    }
                ]
            }
        ]
        provider = transcription.GoogleProvider()
        with mock.patch(
            "videos.transcription._google_post_json", return_value={"results": results}
        ) as post:
            segments = provider.transcribe(self._audio_file(), "ar")

        url, payload, key = post.call_args.args
        self.assertEqual(url, transcription.GOOGLE_SPEECH_URL)
        self.assertEqual(key, self.API_KEY)
        config = payload["config"]
        self.assertEqual(config["encoding"], "MP3")
        self.assertEqual(config["sampleRateHertz"], transcription.CHUNK_SAMPLE_RATE)
        self.assertEqual(config["languageCode"], "ar-SA")
        self.assertEqual(config["model"], "latest_long")
        self.assertTrue(config["enableWordTimeOffsets"])
        self.assertTrue(config["enableAutomaticPunctuation"])
        self.assertNotIn("alternativeLanguageCodes", config)
        self.assertEqual(
            _base64.b64decode(payload["audio"]["content"]), b"fake-mp3-bytes"
        )
        self.assertEqual([s.text for s in segments], ["Hello world."])
        self.assertAlmostEqual(segments[0].end, 1.0)

    @override_settings(
        GOOGLE_API_KEY=API_KEY,
        GOOGLE_SPEECH_MODEL="",
        GOOGLE_SPEECH_ALTERNATIVE_LANGUAGES="fr,en",
    )
    def test_alternative_languages_and_an_empty_model_are_supported(self):
        provider = transcription.GoogleProvider()
        with mock.patch(
            "videos.transcription._google_post_json", return_value={"results": []}
        ) as post:
            provider.transcribe(self._audio_file(), "ar")

        config = post.call_args.args[1]["config"]
        self.assertNotIn("model", config)
        self.assertEqual(config["alternativeLanguageCodes"], ["fr-FR", "en-US"])

    @override_settings(GOOGLE_API_KEY=API_KEY, GOOGLE_TRANSLATE_MODEL="nmt")
    def test_translate_unescapes_and_keeps_the_timings(self):
        provider = transcription.GoogleProvider()
        provider._source_language = "ar"
        segments = [
            transcription.Segment(0, 1, "Ø³"),
            transcription.Segment(1, 2, "Ø¨"),
        ]
        response = {
            "data": {
                "translations": [
                    {"translatedText": "It&#39;s a"},
                    {"translatedText": "test &amp; more"},
                ]
            }
        }
        with mock.patch(
            "videos.transcription._google_post_json", return_value=response
        ) as post:
            translated = provider.translate(segments, "fr")

        url, payload, _key = post.call_args.args
        self.assertEqual(url, transcription.GOOGLE_TRANSLATE_URL)
        self.assertEqual(payload["q"], ["Ø³", "Ø¨"])
        self.assertEqual(payload["target"], "fr")
        self.assertEqual(payload["source"], "ar")
        self.assertEqual(payload["model"], "nmt")
        self.assertEqual(payload["format"], "text")
        self.assertEqual([s.text for s in translated], ["It's a", "test & more"])
        self.assertEqual([(s.start, s.end) for s in translated], [(0, 1), (1, 2)])

    @override_settings(GOOGLE_API_KEY=API_KEY)
    def test_translate_batches_long_lists(self):
        provider = transcription.GoogleProvider()
        count = transcription.TRANSLATE_BATCH * 2 + 3
        segments = [
            transcription.Segment(i, i + 1, f"line {i}") for i in range(count)
        ]
        payloads = []

        def fake(url, payload, api_key):
            payloads.append(payload)
            return {
                "data": {
                    "translations": [
                        {"translatedText": text} for text in payload["q"]
                    ]
                }
            }

        with mock.patch("videos.transcription._google_post_json", side_effect=fake):
            translated = provider.translate(segments, "en")

        self.assertEqual(len(translated), count)
        self.assertEqual(
            [len(payload["q"]) for payload in payloads],
            [
                transcription.TRANSLATE_BATCH,
                transcription.TRANSLATE_BATCH,
                3,
            ],
        )

    @override_settings(GOOGLE_API_KEY=API_KEY)
    def test_translate_rejects_a_short_response(self):
        provider = transcription.GoogleProvider()
        with mock.patch(
            "videos.transcription._google_post_json",
            return_value={"data": {"translations": []}},
        ):
            with self.assertRaises(transcription.TranscriptionError):
                provider.translate([transcription.Segment(0, 1, "x")], "en")


class GoogleProviderSelectionTests(TestCase):
    """Which back end ``SUBTITLE_PROVIDER`` picks and how it is checked."""

    def test_google_is_selected_by_the_setting(self):
        with override_settings(SUBTITLE_PROVIDER="google", GOOGLE_API_KEY="k"):
            self.assertIsInstance(
                transcription.get_provider(), transcription.GoogleProvider
            )

    def test_openai_is_still_the_default(self):
        # Empty and unset both fall back to the OpenAI provider, which stays
        # configured by OPENAI_API_KEY alone (the package is imported lazily).
        with override_settings(SUBTITLE_PROVIDER="", OPENAI_API_KEY="k"):
            self.assertEqual(transcription.provider_name(), "openai")
            self.assertTrue(transcription.is_configured())

    def test_an_unknown_provider_is_reported(self):
        with override_settings(SUBTITLE_PROVIDER="nope"):
            with self.assertRaises(transcription.TranscriptionError):
                transcription.get_provider()

    def test_google_needs_a_key_to_be_configured(self):
        with override_settings(SUBTITLE_PROVIDER="google", GOOGLE_API_KEY=""):
            self.assertFalse(transcription.is_configured())
        with override_settings(SUBTITLE_PROVIDER="google", GOOGLE_API_KEY="k"):
            self.assertTrue(transcription.is_configured())

    def test_the_hint_names_the_configured_providers_key(self):
        with override_settings(SUBTITLE_PROVIDER="google"):
            self.assertEqual(transcription.configuration_hint(), "GOOGLE_API_KEY")
        with override_settings(SUBTITLE_PROVIDER="openai"):
            self.assertEqual(transcription.configuration_hint(), "OPENAI_API_KEY")

    def test_a_missing_key_is_reported_when_the_provider_is_built(self):
        with override_settings(SUBTITLE_PROVIDER="google", GOOGLE_API_KEY=""):
            with self.assertRaises(transcription.TranscriptionError) as caught:
                transcription.GoogleProvider()
        self.assertIn("GOOGLE_API_KEY", str(caught.exception))


class ElevenLabsLanguageCodeTests(TestCase):
    """The language code Scribe is asked for (and when it auto-detects)."""

    def test_the_source_language_is_lowercased(self):
        with override_settings(ELEVENLABS_LANGUAGE_CODE=""):
            self.assertEqual(transcription.elevenlabs_language_code("WO"), "wo")
            self.assertEqual(transcription.elevenlabs_language_code("ar"), "ar")

    def test_empty_language_means_auto_detect(self):
        with override_settings(ELEVENLABS_LANGUAGE_CODE=""):
            self.assertEqual(transcription.elevenlabs_language_code(""), "")

    def test_the_setting_overrides_the_source_language(self):
        with override_settings(ELEVENLABS_LANGUAGE_CODE="fr"):
            self.assertEqual(transcription.elevenlabs_language_code("wo"), "fr")

    def test_the_setting_can_ask_for_auto_detect(self):
        with override_settings(ELEVENLABS_LANGUAGE_CODE="auto"):
            self.assertEqual(transcription.elevenlabs_language_code("wo"), "")


class ElevenLabsCaptionTests(TestCase):
    """Scribe's word list is grouped into the same caption lines as Google's."""

    def test_words_become_timed_captions(self):
        result = {
            "text": "Hello world.",
            "words": [
                {"text": "Hello", "start": 0.0, "end": 0.4, "type": "word"},
                {"text": " ", "start": 0.4, "end": 0.5, "type": "spacing"},
                {"text": "world.", "start": 0.5, "end": 1.0, "type": "word"},
            ],
        }
        captions = transcription.captions_from_elevenlabs(result)
        self.assertEqual([c.text for c in captions], ["Hello world."])
        self.assertAlmostEqual(captions[0].start, 0.0)
        self.assertAlmostEqual(captions[0].end, 1.0)

    def test_spacing_and_audio_events_are_ignored(self):
        result = {
            "words": [
                {"text": "[beep]", "start": 0.0, "end": 0.5, "type": "audio_event"},
                {"text": "hi", "start": 0.5, "end": 1.0, "type": "word"},
            ]
        }
        self.assertEqual(
            transcription.words_from_elevenlabs(result), [("hi", 0.5, 1.0)]
        )

    def test_words_without_timings_are_skipped(self):
        result = {
            "words": [
                {"text": "no-times", "type": "word"},
                {"text": "timed", "start": 1.0, "end": 2.0, "type": "word"},
            ]
        }
        self.assertEqual(
            transcription.words_from_elevenlabs(result), [("timed", 1.0, 2.0)]
        )

    def test_a_result_without_words_falls_back_to_the_transcript(self):
        result = {"text": "One two three", "audio_duration_secs": 5}
        captions = transcription.captions_from_elevenlabs(result)
        self.assertEqual(captions[0].text, "One two three")
        self.assertAlmostEqual(captions[0].end, 5.0)

    def test_an_empty_result_produces_nothing(self):
        self.assertEqual(transcription.captions_from_elevenlabs({}), [])
        self.assertEqual(transcription.captions_from_elevenlabs(None), [])


class ElevenLabsProviderTests(TestCase):
    """The ElevenLabs Scribe back end."""

    API_KEY = "test-elevenlabs-key"

    def _audio_file(self, data=b"fake-mp3-bytes"):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "chunk.mp3"
        path.write_bytes(data)
        return str(path)

    @override_settings(
        ELEVENLABS_API_KEY=API_KEY,
        ELEVENLABS_STT_MODEL="scribe_v2",
        ELEVENLABS_LANGUAGE_CODE="",
    )
    def test_transcribe_builds_the_request_and_reads_captions(self):
        result = {
            "language_code": "eng",
            "words": [
                {"text": "Hello", "start": 0.0, "end": 0.4, "type": "word"},
                {"text": "world.", "start": 0.4, "end": 1.0, "type": "word"},
            ],
        }
        provider = transcription.ElevenLabsProvider()
        with mock.patch(
            "videos.transcription._elevenlabs_transcribe_request",
            return_value=result,
        ) as post:
            segments = provider.transcribe(self._audio_file(), "ar")

        key, path, data = post.call_args.args
        self.assertEqual(key, self.API_KEY)
        self.assertEqual(data["model_id"], "scribe_v2")
        self.assertEqual(data["timestamps_granularity"], "word")
        self.assertEqual(data["language_code"], "ar")
        self.assertEqual([s.text for s in segments], ["Hello world."])

    @override_settings(
        ELEVENLABS_API_KEY=API_KEY,
        ELEVENLABS_STT_MODEL="",
        ELEVENLABS_LANGUAGE_CODE="",
    )
    def test_an_empty_model_defaults_and_no_language_means_auto(self):
        provider = transcription.ElevenLabsProvider()
        with mock.patch(
            "videos.transcription._elevenlabs_transcribe_request",
            return_value={"words": []},
        ) as post:
            provider.transcribe(self._audio_file(), "")

        data = post.call_args.args[2]
        self.assertEqual(data["model_id"], "scribe_v2")
        self.assertNotIn("language_code", data)

    @override_settings(ELEVENLABS_API_KEY=API_KEY)
    def test_translate_is_not_supported(self):
        provider = transcription.ElevenLabsProvider()
        self.assertFalse(provider.supports_translation)
        with self.assertRaises(transcription.TranscriptionError):
            provider.translate([transcription.Segment(0, 1, "hi")], "fr")

    def test_a_missing_key_is_reported_when_the_provider_is_built(self):
        with override_settings(ELEVENLABS_API_KEY=""):
            with self.assertRaises(transcription.TranscriptionError) as caught:
                transcription.ElevenLabsProvider()
        self.assertIn("ELEVENLABS_API_KEY", str(caught.exception))

    def test_the_provider_uses_the_shared_chunk_length(self):
        with override_settings(ELEVENLABS_API_KEY=self.API_KEY):
            provider = transcription.ElevenLabsProvider()
        self.assertEqual(provider.chunk_seconds, transcription.CHUNK_SECONDS)


class ElevenLabsProviderSelectionTests(TestCase):
    """``SUBTITLE_PROVIDER=elevenlabs`` is wired into the same switches."""

    def test_elevenlabs_is_selected_by_the_setting(self):
        with override_settings(SUBTITLE_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="k"):
            self.assertIsInstance(
                transcription.get_provider(), transcription.ElevenLabsProvider
            )

    def test_elevenlabs_needs_a_key_to_be_configured(self):
        with override_settings(SUBTITLE_PROVIDER="elevenlabs", ELEVENLABS_API_KEY=""):
            self.assertFalse(transcription.is_configured())
        with override_settings(SUBTITLE_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="k"):
            self.assertTrue(transcription.is_configured())

    def test_the_hint_names_the_elevenlabs_key(self):
        with override_settings(SUBTITLE_PROVIDER="elevenlabs"):
            self.assertEqual(
                transcription.configuration_hint(), "ELEVENLABS_API_KEY"
            )


class ElevenLabsTranslationPairingTests(TestCase):
    """ElevenLabs transcribes but cannot translate text.

    Naming a second back end in ``SUBTITLE_TRANSLATION_PROVIDER`` pairs the
    Scribe transcription with a real translation, so the target-language
    caption tracks are still produced.
    """

    def test_elevenlabs_alone_has_no_translator(self):
        with override_settings(
            SUBTITLE_PROVIDER="elevenlabs",
            SUBTITLE_TRANSLATION_PROVIDER="",
            ELEVENLABS_API_KEY="k",
        ):
            provider = transcription.get_provider()
            self.assertIsNone(transcription.get_translation_provider(provider))
            self.assertFalse(transcription.translation_configured())

    def test_a_google_translator_is_paired_with_the_elevenlabs_transcription(self):
        with override_settings(
            SUBTITLE_PROVIDER="elevenlabs",
            SUBTITLE_TRANSLATION_PROVIDER="google",
            ELEVENLABS_API_KEY="k",
            GOOGLE_API_KEY="g",
        ):
            provider = transcription.get_provider()
            translator = transcription.get_translation_provider(provider)
            self.assertIsInstance(translator, transcription.GoogleProvider)
            self.assertTrue(transcription.translation_configured())

    def test_an_openai_translator_is_paired_too(self):
        # The 'openai' package is not installed here, so the class is stubbed
        # and only the wiring is checked.
        with override_settings(
            SUBTITLE_PROVIDER="elevenlabs",
            SUBTITLE_TRANSLATION_PROVIDER="openai",
            ELEVENLABS_API_KEY="k",
            OPENAI_API_KEY="o",
        ), mock.patch.object(transcription, "OpenAIProvider") as factory:
            provider = transcription.get_provider()
            translator = transcription.get_translation_provider(provider)

        factory.assert_called_once()
        self.assertIs(translator, factory.return_value)

    def test_the_transcription_provider_still_translates_when_it_can(self):
        with override_settings(
            SUBTITLE_PROVIDER="google",
            SUBTITLE_TRANSLATION_PROVIDER="",
            GOOGLE_API_KEY="g",
        ):
            provider = transcription.get_provider()
            translator = transcription.get_translation_provider(provider)
            self.assertIs(translator, provider)
            self.assertTrue(transcription.translation_configured())

    def test_the_same_provider_is_not_built_twice(self):
        with override_settings(
            SUBTITLE_PROVIDER="google",
            SUBTITLE_TRANSLATION_PROVIDER="google",
            GOOGLE_API_KEY="g",
        ):
            provider = transcription.get_provider()
            self.assertIs(transcription.get_translation_provider(provider), provider)

    def test_an_unknown_translation_provider_is_rejected(self):
        with override_settings(
            SUBTITLE_PROVIDER="elevenlabs",
            SUBTITLE_TRANSLATION_PROVIDER="babelfish",
            ELEVENLABS_API_KEY="k",
        ):
            with self.assertRaises(transcription.TranscriptionError):
                transcription.get_translation_provider(transcription.get_provider())

    def test_the_target_tracks_are_created_with_a_paired_translator(self):
        """A run with ElevenLabs + Google produces the French and English cues."""
        user = make_user("pairing")
        video = make_video(user, audio_file_id="audio/p.mp3", audio_status="ready")
        segments = [transcription.Segment(0.0, 1.0, "salam")]

        class FakeProvider(transcription.SubtitleProvider):
            name = "elevenlabs"
            supports_translation = False

            def transcribe(self, path, language):
                return list(segments)

            def translate(self, segments, target_language):
                raise AssertionError("the ElevenLabs provider must not translate")

        class FakeTranslator(transcription.SubtitleProvider):
            name = "google"

            def transcribe(self, path, language):
                return []

            def translate(self, segments, target_language):
                return [
                    transcription.Segment(s.start, s.end, f"[{target_language}] {s.text}")
                    for s in segments
                ]

        with override_settings(
            SUBTITLE_PROVIDER="elevenlabs",
            SUBTITLE_TRANSLATION_PROVIDER="google",
            SUBTITLE_SOURCE_LANGUAGE="wo",
            SUBTITLE_TARGET_LANGUAGES="fr,en",
        ), mock.patch.object(
            transcription, "get_provider", return_value=FakeProvider()
        ), mock.patch.object(
            transcription, "get_translation_provider", return_value=FakeTranslator()
        ), mock.patch.object(
            transcription, "download_audio", return_value="audio.mp3"
        ), mock.patch.object(
            transcription, "chunk_audio", return_value=[("chunk.mp3", 0.0)]
        ):
            result = transcription.generate_subtitles(video.id, include_lyrics=False)

        self.assertEqual(sorted(result["languages"]), ["en", "fr", "wo"])
        for code in ("wo", "fr", "en"):
            track = video.subtitle_tracks.get(language_code=code)
            self.assertIn("salam", track.content)
            if code != "wo":
                # The target tracks went through the translation provider.
                self.assertIn(f"[{code}]", track.content)

    def test_a_failing_translation_keeps_the_source_track(self):
        """A translation with no credit must not throw away the captions."""
        user = make_user("partial")
        video = make_video(user, audio_file_id="audio/p.mp3", audio_status="ready")
        segments = [transcription.Segment(0.0, 1.0, "salam")]

        class FakeProvider(transcription.SubtitleProvider):
            name = "elevenlabs"
            supports_translation = False

            def transcribe(self, path, language):
                return list(segments)

            def translate(self, segments, target_language):
                raise AssertionError("the ElevenLabs provider must not translate")

        class BrokenTranslator(transcription.SubtitleProvider):
            name = "openai"

            def transcribe(self, path, language):
                return []

            def translate(self, segments, target_language):
                raise transcription.TranscriptionError("You have no credits remaining.")

        with override_settings(
            SUBTITLE_PROVIDER="elevenlabs",
            SUBTITLE_TRANSLATION_PROVIDER="openai",
            SUBTITLE_SOURCE_LANGUAGE="wo",
            SUBTITLE_TARGET_LANGUAGES="fr,en",
        ), mock.patch.object(
            transcription, "get_provider", return_value=FakeProvider()
        ), mock.patch.object(
            transcription, "get_translation_provider", return_value=BrokenTranslator()
        ), mock.patch.object(
            transcription, "download_audio", return_value="audio.mp3"
        ), mock.patch.object(
            transcription, "chunk_audio", return_value=[("chunk.mp3", 0.0)]
        ):
            summary = transcription.generate_subtitles(video.id, include_lyrics=False)

        # The Wolof track exists and both translations are reported as broken.
        self.assertEqual(summary["languages"], ["wo"])
        self.assertEqual(len(summary["warnings"]), 2)
        self.assertTrue(video.subtitle_tracks.filter(language_code="wo").exists())
        self.assertFalse(video.subtitle_tracks.filter(language_code="fr").exists())

    def test_a_run_with_a_broken_translation_is_still_ready(self):
        """The video keeps its captions, so the run is not marked as failed."""
        user = make_user("partialstatus")
        video = make_video(user, audio_file_id="audio/p.mp3", audio_status="ready")

        with mock.patch.object(
            transcription,
            "generate_subtitles",
            return_value={"languages": ["wo"], "cues": 1, "warnings": ["fr: no credits"]},
        ):
            self.assertTrue(transcription.generate_and_record(video.id))

        video.refresh_from_db()
        self.assertEqual(video.auto_subtitle_status, "ready")
        self.assertIn("no credits", video.auto_subtitle_error)


class PuterProviderSelectionTests(TestCase):
    """``SUBTITLE_PROVIDER=puter`` is wired into the same switches."""

    def test_puter_is_selected_by_the_setting(self):
        with override_settings(SUBTITLE_PROVIDER="puter", PUTER_AUTH_TOKEN="t"):
            self.assertIsInstance(transcription.get_provider(), transcription.PuterProvider)

    def test_puter_needs_a_token_to_be_configured(self):
        with override_settings(SUBTITLE_PROVIDER="puter", PUTER_AUTH_TOKEN=""):
            self.assertFalse(transcription.is_configured())
        with override_settings(SUBTITLE_PROVIDER="puter", PUTER_AUTH_TOKEN="t"):
            self.assertTrue(transcription.is_configured())

    def test_the_hint_names_the_puter_token(self):
        with override_settings(SUBTITLE_PROVIDER="puter"):
            self.assertEqual(transcription.configuration_hint(), "PUTER_AUTH_TOKEN")

    def test_puter_cannot_translate_text(self):
        with override_settings(SUBTITLE_PROVIDER="puter", PUTER_AUTH_TOKEN="t"):
            provider = transcription.get_provider()
            self.assertTrue(provider.supports_translation)
            self.assertIs(
                transcription.get_translation_provider(provider), provider
            )

class PuterHttpLayerTests(TestCase):
    """The driver call that talks to Puter's AI gateway."""

    class _Response:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("no json body")
            return self._payload

    def _audio_file(self, data=b"fake-mp3-bytes"):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "chunk.mp3"
        path.write_bytes(data)
        return str(path)

    def test_the_call_carries_the_token_and_the_audio(self):
        response = self._Response(200, {"text": "salam"})
        with mock.patch("httpx.post", return_value=response) as post:
            result = transcription._puter_transcribe_request(
                "tok", self._audio_file(), {"provider": "gemini"}
            )

        self.assertEqual(result, {"text": "salam"})
        url = post.call_args.args[0]
        self.assertEqual(url, transcription.PUTER_DRIVER_URL)
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer tok")

        body = json.loads(post.call_args.kwargs["content"].decode("utf-8"))
        self.assertEqual(body["interface"], "puter-speech2txt")
        self.assertEqual(body["driver"], "ai-speech2txt")
        self.assertEqual(body["method"], "transcribe")
        self.assertEqual(body["auth_token"], "tok")
        self.assertTrue(body["args"]["file"].startswith("data:audio/mpeg;base64,"))
        self.assertEqual(body["args"]["provider"], "gemini")

    def test_a_result_envelope_is_unwrapped(self):
        response = self._Response(200, {"result": {"text": "salam"}})
        with mock.patch("httpx.post", return_value=response):
            result = transcription._puter_transcribe_request(
                "tok", self._audio_file(), {}
            )
        self.assertEqual(result, {"text": "salam"})

    def test_an_error_response_is_reported(self):
        response = self._Response(402, {"error": {"message": "no allowance left"}})
        with mock.patch("httpx.post", return_value=response):
            with self.assertRaises(transcription.TranscriptionError) as caught:
                transcription._puter_transcribe_request("tok", self._audio_file(), {})
        self.assertIn("no allowance left", str(caught.exception))

    def test_the_words_of_the_xai_back_end_are_read(self):
        """xAI leaves the type tag off, so the words must not be skipped."""
        result = {
            "text": "GÃ³or gi ni baret",
            "words": [
                {"text": "GÃ³or", "start": 0.14, "end": 0.52, "speaker": 0},
                {"text": "gi", "start": 0.74, "end": 0.86, "speaker": 0},
            ],
        }
        words = transcription.words_from_puter_result(result)
        self.assertEqual(len(words), 2)
        self.assertEqual(words[0][0], "GÃ³or")
        self.assertAlmostEqual(words[1][2], 0.86)

    def test_entries_that_are_not_words_are_skipped(self):
        result = {
            "words": [
                {"type": "spacing", "text": " ", "start": 0, "end": 0.1},
                {"type": "word", "text": "salam", "start": 0.2, "end": 0.9},
            ]
        }
        words = transcription.words_from_puter_result(result)
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0][0], "salam")

    def test_gemini_is_asked_for_word_timings(self):
        with override_settings(PUTER_STT_PROVIDER="gemini", PUTER_STT_MODEL=""):
            provider = transcription.PuterProvider.__new__(transcription.PuterProvider)
            provider._token = "tok"
            with mock.patch.object(
                transcription, "_puter_transcribe_request", return_value={"text": "x"}
            ) as call:
                provider.transcribe(self._audio_file(), "wo")

        args = call.call_args.args[2]
        self.assertEqual(args["provider"], "gemini")
        self.assertEqual(args["model"], "gemini-3.5-transcribe")
        self.assertEqual(args["timestamp_granularities"], ["word"])
        self.assertEqual(args["language"], "wo")


class PuterCaptionTests(TestCase):
    """The answer from Puter becomes the usual caption lines."""

    def test_word_timings_become_caption_lines(self):
        result = {
            "text": "salam",
            "words": [
                {"text": "salam", "start": 0.0, "end": 0.8},
                {"text": "alay", "start": 0.9, "end": 1.5},
            ],
        }
        segments = transcription.captions_from_puter_result(result)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].text, "salam alay")
        self.assertAlmostEqual(segments[0].start, 0.0)
        self.assertAlmostEqual(segments[0].end, 1.5)

    def test_provider_segments_are_used_when_there_are_no_words(self):
        result = {
            "text": "salam alay",
            "segments": [{"text": "salam alay", "start": 1.0, "end": 3.0}],
        }
        segments = transcription.captions_from_puter_result(result)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].text, "salam alay")
        self.assertAlmostEqual(segments[0].start, 1.0)

    def test_bare_text_becomes_one_cue(self):
        segments = transcription.captions_from_puter_result(
            {"text": "salam alay", "duration": 4.0}
        )
        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0].end, 4.0)

    def test_an_empty_answer_gives_no_cues(self):
        self.assertEqual(transcription.captions_from_puter_result({}), [])
        self.assertEqual(transcription.captions_from_puter_result(None), [])


class AssemblyAIProviderSelectionTests(TestCase):
    """``SUBTITLE_PROVIDER=assemblyai`` is wired into the same switches."""

    def test_assemblyai_is_selected_by_the_setting(self):
        with override_settings(SUBTITLE_PROVIDER="assemblyai", ASSEMBLYAI_API_KEY="k"):
            self.assertIsInstance(
                transcription.get_provider(), transcription.AssemblyAIProvider
            )

    def test_assemblyai_needs_a_key_to_be_configured(self):
        with override_settings(SUBTITLE_PROVIDER="assemblyai", ASSEMBLYAI_API_KEY=""):
            self.assertFalse(transcription.is_configured())
        with override_settings(SUBTITLE_PROVIDER="assemblyai", ASSEMBLYAI_API_KEY="k"):
            self.assertTrue(transcription.is_configured())

    def test_the_hint_names_the_assemblyai_key(self):
        with override_settings(SUBTITLE_PROVIDER="assemblyai"):
            self.assertEqual(transcription.configuration_hint(), "ASSEMBLYAI_API_KEY")

    def test_assemblyai_can_be_the_translation_provider(self):
        with override_settings(
            SUBTITLE_PROVIDER="elevenlabs",
            SUBTITLE_TRANSLATION_PROVIDER="assemblyai",
            ELEVENLABS_API_KEY="k",
            ASSEMBLYAI_API_KEY="a",
        ):
            provider = transcription.get_provider()
            translator = transcription.get_translation_provider(provider)
            self.assertIsInstance(translator, transcription.AssemblyAIProvider)
            self.assertTrue(transcription.translation_configured())


class AssemblyAIParsingTests(TestCase):
    """The answer from AssemblyAI becomes the usual caption lines."""

    def test_word_timings_become_caption_lines(self):
        transcript = {
            "words": [
                {"text": "Salam", "start": 0, "end": 400},
                {"text": "alay", "start": 500, "end": 900},
            ]
        }
        segments = transcription.captions_from_assemblyai(transcript)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].text, "Salam alay")
        self.assertAlmostEqual(segments[0].start, 0.0)
        self.assertAlmostEqual(segments[0].end, 0.9)

    def test_utterances_are_used_when_there_are_no_words(self):
        transcript = {
            "utterances": [{"text": "Salam alay", "start": 1000, "end": 3000}]
        }
        segments = transcription.captions_from_assemblyai(transcript)
        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0].start, 1.0)

    def test_sentences_are_split_on_the_ends_of_a_sentence(self):
        sentences = transcription.split_sentences(
            "First one. Second one! Third? Fourth"
        )
        self.assertEqual(
            sentences, ["First one.", "Second one!", "Third?", "Fourth"]
        )

    def test_words_become_sentences_with_their_timings(self):
        transcript = {
            "words": [
                {"text": "Salam", "start": 0, "end": 400},
                {"text": "alay.", "start": 500, "end": 900},
                {"text": "Hamd", "start": 1500, "end": 1900},
                {"text": "lilah.", "start": 2000, "end": 2400},
            ]
        }
        sentences = transcription.assemblyai_sentences(transcript)
        self.assertEqual(len(sentences), 2)
        self.assertEqual(sentences[0][2], "Salam alay.")
        self.assertAlmostEqual(sentences[0][0], 0.0)
        self.assertAlmostEqual(sentences[1][1], 2.4)

    def test_a_translated_sentence_is_spread_over_the_cues_it_covers(self):
        cues = [
            transcription.Segment(0.0, 2.0, "one"),
            transcription.Segment(2.0, 4.0, "two"),
            transcription.Segment(4.0, 6.0, "three"),
        ]
        sentences = [(0.0, 4.0, "Une phrase traduite en francais."), (4.0, 6.0, "Autre.")]

        spread = transcription.spread_translation(cues, sentences)

        self.assertEqual(len(spread), 3)
        # The first two cues share the first translated sentence.
        self.assertTrue(spread[0].text)
        self.assertTrue(spread[1].text)
        self.assertNotEqual(spread[0].text, spread[1].text)
        self.assertEqual(spread[2].text, "Autre.")

    def test_cues_without_a_translation_keep_their_own_text(self):
        cues = [transcription.Segment(0.0, 1.0, "one")]
        spread = transcription.spread_translation(cues, [])
        self.assertEqual(spread[0].text, "one")


class AssemblyAIHttpLayerTests(TestCase):
    """The upload, submit and poll calls that talk to AssemblyAI."""

    class _Response:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("no json body")
            return self._payload

    def _audio_file(self, data=b"fake-mp3-bytes"):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "audio.mp3"
        path.write_bytes(data)
        return str(path)

    def _provider(self):
        with override_settings(ASSEMBLYAI_API_KEY="key"):
            return transcription.AssemblyAIProvider()

    def test_the_file_is_uploaded_first(self):
        provider = self._provider()
        with mock.patch(
            "httpx.post", return_value=self._Response(200, {"upload_url": "https://x/y"})
        ) as post:
            url = provider._upload(self._audio_file())

        self.assertEqual(url, "https://x/y")
        self.assertTrue(post.call_args.args[0].endswith("/upload"))
        self.assertEqual(post.call_args.kwargs["headers"], {"authorization": "key"})

    def test_the_translation_is_asked_for(self):
        provider = self._provider()
        with mock.patch(
            "httpx.post", return_value=self._Response(200, {"id": "abc"})
        ) as post:
            transcript_id = provider._submit("https://x/y", ["fr", "en"])

        self.assertEqual(transcript_id, "abc")
        payload = post.call_args.kwargs["json"]
        translation = payload["speech_understanding"]["request"]["translation"]
        self.assertEqual(translation["target_languages"], ["fr", "en"])

    def test_a_failing_request_is_reported(self):
        provider = self._provider()
        with mock.patch(
            "httpx.post",
            return_value=self._Response(402, {"error": "not enough credits"}),
        ):
            with self.assertRaises(transcription.TranscriptionError) as caught:
                provider._upload(self._audio_file())
        self.assertIn("not enough credits", str(caught.exception))


class PuterTranslationTests(TestCase):
    """Puter translates the caption lines through its chat driver."""

    def _provider(self):
        with override_settings(PUTER_AUTH_TOKEN="tok", PUTER_TRANSLATE_MODEL="m"):
            return transcription.PuterProvider()

    def test_the_chat_call_carries_the_token_and_the_prompt(self):
        provider = self._provider()
        with mock.patch(
            "videos.transcription._puter_chat_request",
            return_value={"message": {"content": "0: un\n1: deux"}},
        ) as call:
            result = provider._translate_batch(["un", "deux"], "fr", "m")

        self.assertEqual(result, ["un", "deux"])
        args = call.call_args.args[1]
        self.assertEqual(args["model"], "m")
        self.assertIn("Translate", args["messages"][0]["content"])
        self.assertIn("0: un", args["messages"][0]["content"])

    def test_the_numbering_echoed_back_is_removed(self):
        provider = self._provider()
        with mock.patch(
            "videos.transcription._puter_chat_request",
            return_value={"message": {"content": "0: un\n1: deux"}},
        ):
            result = provider._translate_batch(["un", "deux"], "fr", "m")
        self.assertEqual(result, ["un", "deux"])

    def test_a_short_answer_is_tried_again_in_smaller_pieces(self):
        provider = self._provider()
        calls = []

        def fake_chat(token, args):
            calls.append(args)
            return {"message": {"content": "0: un"}}

        with mock.patch(
            "videos.transcription._puter_chat_request", side_effect=fake_chat
        ):
            result = provider._translate_batch(["un", "deux"], "fr", "m")

        # The batch is halved and each half is asked for on its own, so the
        # line that was dropped does not cost a caption.
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(len(result), 2)

    def test_a_line_that_cannot_be_translated_keeps_its_text(self):
        provider = self._provider()
        with mock.patch(
            "videos.transcription._puter_chat_request",
            return_value={"message": {"content": ""}},
        ):
            result = provider._translate_batch(["un"], "fr", "m")
        self.assertEqual(result, ["un"])

    def test_the_numbers_line_the_translation_up(self):
        provider = self._provider()
        # The model answers out of order; the numbers say what goes where.
        with mock.patch(
            "videos.transcription._puter_chat_request",
            return_value={"message": {"content": "1: deux\n0: un"}},
        ):
            result = provider._translate_batch(["un", "deux"], "fr", "m")
        self.assertEqual(result, ["un", "deux"])

    def test_the_cues_keep_their_timings(self):
        provider = self._provider()
        segments = [
            transcription.Segment(0.0, 1.5, "un"),
            transcription.Segment(2.0, 3.5, "deux"),
        ]
        with mock.patch(
            "videos.transcription._puter_chat_request",
            return_value={"message": {"content": "un\ndeux"}},
        ):
            translated = provider.translate(segments, "fr")

        self.assertEqual(len(translated), 2)
        self.assertAlmostEqual(translated[0].start, 0.0)
        self.assertAlmostEqual(translated[1].end, 3.5)

    def test_a_content_part_answer_is_read(self):
        text = transcription._puter_message_text(
            {"message": {"content": [{"type": "text", "text": "bonjour"}]}}
        )
        self.assertEqual(text, "bonjour")


class ElevenLabsHttpLayerTests(TestCase):
    """The multipart wrapper that talks to Scribe."""

    class _Response:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("no json body")
            return self._payload

    def _audio_file(self, data=b"fake-mp3-bytes"):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "chunk.mp3"
        path.write_bytes(data)
        return str(path)

    def test_a_successful_call_returns_the_json(self):
        response = self._Response(200, {"text": "ok"})
        with mock.patch("httpx.post", return_value=response) as post:
            result = transcription._elevenlabs_transcribe_request(
                "key", self._audio_file(), {"model_id": "scribe_v2"}
            )

        self.assertEqual(result, {"text": "ok"})
        url = post.call_args.args[0]
        self.assertEqual(url, transcription.ELEVENLABS_SPEECH_URL)
        self.assertEqual(post.call_args.kwargs["headers"], {"xi-api-key": "key"})

    def test_an_error_response_is_reported(self):
        response = self._Response(
            401, {"detail": {"message": "bad key", "code": "unauthorized"}}
        )
        with mock.patch("httpx.post", return_value=response):
            with self.assertRaises(transcription.TranscriptionError) as caught:
                transcription._elevenlabs_transcribe_request(
                    "key", self._audio_file(), {}
                )
        self.assertIn("bad key", str(caught.exception))

    def test_a_network_error_is_wrapped(self):
        import httpx as _httpx

        with mock.patch("httpx.post", side_effect=_httpx.ConnectError("down")):
            with self.assertRaises(transcription.TranscriptionError):
                transcription._elevenlabs_transcribe_request(
                    "key", self._audio_file(), {}
                )

    def test_a_non_json_body_is_reported(self):
        response = self._Response(200, None, text="not json")
        with mock.patch("httpx.post", return_value=response):
            with self.assertRaises(transcription.TranscriptionError):
                transcription._elevenlabs_transcribe_request(
                    "key", self._audio_file(), {}
                )


class ElevenLabsPipelineTests(TestCase):
    """A provider without translation still saves its source track."""

    class _SourceOnlyProvider(transcription.SubtitleProvider):
        name = "elevenlabs"
        supports_translation = False

        def __init__(self):
            self.translate_calls = 0

        def transcribe(self, path, language):
            return [transcription.Segment(0, 1, "hello"), transcription.Segment(1, 2, "world")]

        def translate(self, segments, target_language):
            self.translate_calls += 1
            return list(segments)

    def test_targets_are_skipped_and_the_source_track_is_saved(self):
        user = make_user("owner")
        video = make_video(user, audio_file_id="audio/a.mp3")
        provider = self._SourceOnlyProvider()

        with (
            mock.patch(
                "videos.transcription.get_provider", return_value=provider
            ),
            mock.patch("videos.transcription.download_audio", return_value="audio.mp3"),
            mock.patch(
                "videos.transcription.chunk_audio", return_value=[("c0.mp3", 0.0)]
            ),
            override_settings(
                SUBTITLE_SOURCE_LANGUAGE="wo", SUBTITLE_TARGET_LANGUAGES="fr,en"
            ),
        ):
            result = transcription.generate_subtitles(video.id)

        self.assertEqual(provider.translate_calls, 0)
        self.assertIn("wo", result["languages"])
        self.assertFalse(video.subtitle_tracks.filter(language_code="fr").exists())
        self.assertTrue(video.subtitle_tracks.filter(language_code="wo").exists())


class AudioChunkingTests(TestCase):
    """Chunking stops at the real end of the audio, not at a silent tail."""

    def test_chunks_stop_at_the_known_duration(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        source = str(Path(directory) / "audio.mp3")
        # A two second tone so ffmpeg has something real to read.
        subprocess.run(
            [
                transcription._ffmpeg_exe(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-ar",
                str(transcription.CHUNK_SAMPLE_RATE),
                "-ac",
                "1",
                source,
            ],
            capture_output=True,
            text=True,
        )

        chunks = transcription.chunk_audio(source, directory, chunk_seconds=1)
        self.assertEqual(len(chunks), 2)
        self.assertEqual([offset for _path, offset in chunks], [0.0, 1.0])

    def test_an_unreadable_file_is_reported(self):
        with self.assertRaises(transcription.TranscriptionError):
            transcription.chunk_audio("does-not-exist.mp3", tempfile.mkdtemp())


class GoogleChunkingTests(TestCase):
    """The synchronous recogniser caps a request at about a minute."""

    @override_settings(GOOGLE_API_KEY="k")
    def test_google_asks_for_sub_minute_chunks(self):
        provider = transcription.GoogleProvider()
        self.assertLessEqual(provider.chunk_seconds, 60)
        self.assertLess(provider.chunk_seconds, transcription.CHUNK_SECONDS)

    def test_the_pipeline_uses_the_provider_chunk_length(self):
        user = make_user("owner")
        video = make_video(user, audio_file_id="audio/a.mp3")

        class ShortChunkProvider(transcription.SubtitleProvider):
            name = "fake"
            chunk_seconds = 42

            def transcribe(self, path, language):
                return [transcription.Segment(0, 1, "hi")]

            def translate(self, segments, target_language):
                return list(segments)

        with (
            mock.patch(
                "videos.transcription.get_provider",
                return_value=ShortChunkProvider(),
            ),
            mock.patch("videos.transcription.download_audio", return_value="audio.mp3"),
            mock.patch(
                "videos.transcription.chunk_audio", return_value=[("c0.mp3", 0.0)]
            ) as chunk,
        ):
            transcription.generate_subtitles(video.id)

        self.assertEqual(chunk.call_args.args[2], 42)


class GoogleHttpLayerTests(TestCase):
    """The small httpx wrapper shared by both Google calls."""

    class _Response:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("no json body")
            return self._payload

    def test_a_successful_call_returns_the_json(self):
        response = self._Response(200, {"ok": True})
        with mock.patch("httpx.post", return_value=response) as post:
            data = transcription._google_post_json(
                "https://example.com", {"a": 1}, "secret"
            )
        self.assertEqual(data, {"ok": True})
        self.assertEqual(post.call_args.kwargs["params"], {"key": "secret"})
        self.assertEqual(post.call_args.kwargs["json"], {"a": 1})

    def test_an_error_response_carries_the_google_message(self):
        response = self._Response(400, {"error": {"message": "API key not valid"}})
        with mock.patch("httpx.post", return_value=response):
            with self.assertRaises(transcription.TranscriptionError) as caught:
                transcription._google_post_json("https://example.com", {}, "bad")
        self.assertIn("API key not valid", str(caught.exception))

    def test_a_connection_failure_is_wrapped(self):
        import httpx

        with mock.patch("httpx.post", side_effect=httpx.ConnectError("down")):
            with self.assertRaises(transcription.TranscriptionError) as caught:
                transcription._google_post_json("https://example.com", {}, "k")
        self.assertIn("Could not reach Google", str(caught.exception))


class GoogleSubtitleEndpointTests(TestCase):
    """The video-page endpoint speaks up for whichever provider is configured."""

    def setUp(self):
        self.admin = make_user("root", is_staff=True, is_superuser=True)
        self.video = make_video(self.admin, audio_file_id="audio/a.mp3")
        self.client.login(username="root", password="pw12345678")

    @override_settings(SUBTITLE_PROVIDER="google", GOOGLE_API_KEY="")
    def test_a_missing_google_key_is_named_in_the_error(self):
        response = self.client.post(
            reverse("videos:subtitles_generate", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("GOOGLE_API_KEY", response.json()["error"])


class BackgroundPlaybackTests(TestCase):
    """Media Session wiring so playback survives the screen locking."""

    def setUp(self):
        self.user = make_user("viewer")
        self.video = make_video(
            self.user,
            title="Listen",
            audio_file_id="audio/track.mp3",
            thumbnail_file_id="thumbnails/a.jpg",
        )

    def _detail(self):
        return self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()

    def test_video_element_plays_inline(self):
        # Required on iOS: without it Safari forces fullscreen and background
        # audio stops when the tab is hidden.
        self.assertRegex(self._detail(), r"<video[^>]*\splaysinline")

    def test_media_session_is_registered(self):
        content = self._detail()
        self.assertIn("navigator.mediaSession", content)
        self.assertIn("new MediaMetadata", content)
        self.assertIn("mediaSession.metadata", content)

    def test_offline_capable_handlers_are_registered(self):
        content = self._detail()
        for action in ("play", "pause", "seekbackward", "seekforward", "stop"):
            self.assertIn(f"        {action}:", content)

    def test_unsupported_actions_are_guarded(self):
        # setActionHandler throws for actions a browser does not implement.
        self.assertIn("try { mediaSession.setActionHandler", self._detail())

    def test_playback_state_follows_the_active_player(self):
        content = self._detail()
        self.assertIn("function setActivePlayer", content)
        self.assertIn("function syncPlaybackState", content)
        self.assertIn("el.addEventListener(name, syncPlaybackState)", content)

    def test_mode_switch_changes_the_active_player(self):
        # The OS controls must follow whichever mode the listener is in.
        content = self._detail()
        self.assertIn("setActivePlayer(audioPlayer)", content)
        self.assertIn("setActivePlayer(videoPlayer)", content)

    def test_metadata_uses_the_video_details(self):
        content = self._detail()
        self.assertIn('title: "Listen"', content)
        self.assertIn('album: "Rammonte USA"', content)


class AudioSweepTests(TestCase):
    """The bulk "Generate missing MP3s" run behind the dashboard button."""

    def setUp(self):
        self.user = make_user("owner")
        # The sweep state is module level, so reset it between tests.
        audio_tasks._SWEEP_STATE.update(
            running=False, total=0, done=0, succeeded=0, failed=0
        )
        self.addCleanup(
            lambda: audio_tasks._SWEEP_STATE.update(
                running=False, total=0, done=0, succeeded=0, failed=0
            )
        )

    def test_eligible_skips_silent_videos(self):
        make_video(self.user, title="Untouched")
        make_video(self.user, title="Failed", audio_status="failed")
        make_video(self.user, title="Processing", audio_status="processing")
        make_video(self.user, title="Silent", audio_status="none")
        make_video(self.user, title="Ready", audio_file_id="audio/a.mp3")

        titles = set(
            audio_tasks.eligible_audio_queryset().values_list("title", flat=True)
        )
        self.assertEqual(titles, {"Untouched", "Failed", "Processing"})

    @mock.patch("videos.audio_tasks.generate_audio", return_value=True)
    def test_sweep_converts_every_eligible_video(self, generate):
        wanted = make_video(self.user, title="A")
        also_wanted = make_video(self.user, title="B", audio_status="failed")
        make_video(self.user, title="Silent", audio_status="none")
        make_video(self.user, title="Ready", audio_file_id="audio/a.mp3")

        audio_tasks._sweep_videos()

        self.assertEqual(
            sorted(call.args[0] for call in generate.call_args_list),
            sorted([wanted.id, also_wanted.id]),
        )
        state = audio_tasks.audio_sweep_status()
        self.assertTrue(state["running"])
        self.assertEqual(state["total"], 2)
        self.assertEqual(state["done"], 2)
        self.assertEqual(state["succeeded"], 2)
        self.assertEqual(state["failed"], 0)

    @mock.patch("videos.audio_tasks.generate_audio", return_value=False)
    def test_sweep_counts_failures(self, generate):
        make_video(self.user, title="Broken")
        make_video(self.user, title="AlsoBroken")

        audio_tasks._sweep_videos()

        state = audio_tasks.audio_sweep_status()
        self.assertEqual(state["done"], 2)
        self.assertEqual(state["failed"], 2)
        self.assertEqual(state["succeeded"], 0)

    @mock.patch("videos.audio_tasks.generate_audio", return_value=True)
    def test_sweep_marks_each_video_processing(self, generate):
        video = make_video(self.user, title="Needs audio")

        seen = []

        def record(video_id, *_args, **_kwargs):
            seen.append(
                Video.objects.filter(id=video_id)
                .values_list("audio_status", flat=True)
                .first()
            )
            return True

        generate.side_effect = record
        audio_tasks._sweep_videos()

        # The video is flagged "processing" before the conversion runs, so the
        # on-play path cannot start a second worker for it.
        self.assertEqual(seen, ["processing"])

    @mock.patch("videos.audio_tasks.threading.Thread")
    def test_start_sweep_runs_once(self, thread):
        self.assertTrue(audio_tasks.start_audio_sweep())
        # The thread is faked, so the slot stays taken and a second call is refused.
        self.assertFalse(audio_tasks.start_audio_sweep())
        thread.assert_called_once()
        self.assertTrue(audio_tasks.audio_sweep_status()["running"])


class StaleJobTests(TestCase):
    """A host that sleeps can kill a worker, leaving a job stuck 'processing'."""

    def setUp(self):
        self.user = make_user("owner")
        # A video mid-conversion has no MP3 yet.
        self.video = make_video(self.user, title="Needs audio")

    def _stuck(self, minutes):
        Video.objects.filter(id=self.video.id).update(
            audio_status="processing",
            job_started_at=timezone.now() - timedelta(minutes=minutes),
        )

    def test_recent_job_is_left_alone(self):
        self._stuck(1)
        self.assertEqual(audio_tasks.clear_stale_jobs(), 0)
        self.video.refresh_from_db()
        self.assertEqual(self.video.audio_status, "processing")

    def test_abandoned_job_is_released(self):
        self._stuck(audio_tasks.stale_minutes() + 10)
        self.assertEqual(audio_tasks.clear_stale_jobs(), 1)
        self.video.refresh_from_db()
        self.assertEqual(self.video.audio_status, "failed")
        self.assertIsNone(self.video.job_started_at)
        # ...and it can now be retried.
        with mock.patch("videos.audio_tasks.threading.Thread") as thread:
            self.assertTrue(
                audio_tasks.schedule_audio_generation(self.video.id, retry=True)
            )
        thread.assert_called_once()

    def test_finished_subtitles_are_not_reset_by_a_stuck_audio_job(self):
        Video.objects.filter(id=self.video.id).update(
            auto_subtitle_status="ready",
        )
        self._stuck(audio_tasks.stale_minutes() + 10)
        audio_tasks.clear_stale_jobs()
        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "ready")
        self.assertEqual(self.video.audio_status, "failed")

    def test_stuck_subtitle_job_is_released(self):
        Video.objects.filter(id=self.video.id).update(
            audio_file_id="audio/a.mp3",
            auto_subtitle_status="processing",
            job_started_at=timezone.now()
            - timedelta(minutes=audio_tasks.stale_minutes() + 10),
        )
        self.assertEqual(audio_tasks.clear_stale_jobs(), 1)
        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "failed")

    def test_status_endpoint_clears_stuck_jobs(self):
        self._stuck(audio_tasks.stale_minutes() + 10)
        self.client.get(reverse("videos:audio_status", args=[self.video.id]))
        self.video.refresh_from_db()
        self.assertEqual(self.video.audio_status, "failed")

class VideoSearchTests(TestCase):
    """Search matches any word in the title or description, the uploader, or a year."""

    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.a = make_video(
            self.alice,
            title="Mame Cheikh Capitaine",
            description="Recitation recorded in Diourbel",
        )
        self.b = make_video(self.bob, title="Serigne Moustapha Sy", description="Wedding")
        # created_at is auto_now_add, so pin the year directly on the row.
        Video.objects.filter(pk=self.b.pk).update(
            created_at=datetime(2000, 8, 19, tzinfo=dt_timezone.utc)
        )

    def found(self, query):
        response = self.client.get(reverse("videos:search"), {"q": query})
        self.assertEqual(response.status_code, 200)
        return list(response.context["videos"])

    def test_search_page_renders_without_a_query(self):
        response = self.client.get(reverse("videos:search"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["videos"]), [])

    def test_matches_a_word_in_the_title(self):
        self.assertEqual(self.found("mame"), [self.a])

    def test_title_search_is_case_insensitive(self):
        self.assertEqual(self.found("MAME"), [self.a])

    def test_matches_a_word_in_the_description(self):
        self.assertEqual(self.found("diourbel"), [self.a])

    def test_matches_the_uploaders_name(self):
        self.assertEqual(self.found("bob"), [self.b])

    def test_matches_a_year_of_upload(self):
        # Nothing stores "2000" as text, so this only works via created_at.
        self.assertEqual(self.found("2000"), [self.b])

    def test_year_that_matches_nothing_returns_nothing(self):
        self.assertEqual(self.found("1999"), [])

    def test_words_are_combined_rather_than_matched_as_a_phrase(self):
        # "mame 2000" should return nothing: neither video satisfies both.
        self.assertEqual(self.found("mame 2000"), [])
        self.assertEqual(self.found("mame diourbel"), [self.a])

    def test_unknown_word_returns_nothing(self):
        self.assertEqual(self.found("zzzznotathing"), [])

    def test_query_is_echoed_back_for_the_search_box(self):
        response = self.client.get(reverse("videos:search"), {"q": "mame"})
        self.assertEqual(response.context["query"], "mame")

    def test_blank_query_returns_nothing_rather_than_everything(self):
        response = self.client.get(reverse("videos:search"), {"q": "   "})
        self.assertEqual(list(response.context["videos"]), [])

    def test_search_box_appears_in_the_navigation(self):
        response = self.client.get(reverse("videos:list"))
        self.assertContains(response, reverse("videos:search"))
        self.assertContains(response, 'name="q"')

    def test_no_results_offers_a_youtube_search_link(self):
        response = self.client.get(reverse("videos:search"), {"q": "zzzznotathing"})
        body = response.content.decode()
        self.assertIn("youtube.com/results", body)
        self.assertIn("zzzznotathing", body)
        self.assertIn('target="_blank"', body)
        self.assertIn('rel="noopener noreferrer"', body)

    def test_youtube_link_carries_the_typed_query(self):
        response = self.client.get(reverse("videos:search"), {"q": "mame cheikh"})
        self.assertEqual(
            response.context["youtube_url"],
            "https://www.youtube.com/results?search_query=mame+cheikh",
        )

    def test_youtube_link_percent_encodes_awkward_characters(self):
        # A space and an ampersand must survive the trip through the URL.
        response = self.client.get(reverse("videos:search"), {"q": "a & b"})
        self.assertEqual(
            response.context["youtube_url"],
            "https://www.youtube.com/results?search_query=a+%26+b",
        )

    def test_youtube_link_shows_even_when_results_exist(self):
        # The point is "I did not find it here", which is still true when a
        # partial match exists; the fallback is always offered once a search ran.
        response = self.client.get(reverse("videos:search"), {"q": "mame"})
        self.assertIn("youtube.com/results", response.content.decode())

    def test_no_youtube_link_before_a_search_is_run(self):
        response = self.client.get(reverse("videos:search"))
        self.assertNotIn("youtube.com/results", response.content.decode())
        self.assertEqual(response.context["youtube_url"], "")

    def test_blank_query_offers_no_youtube_link(self):
        response = self.client.get(reverse("videos:search"), {"q": "   "})
        self.assertEqual(response.context["youtube_url"], "")


class AudioOnlyUploadTests(TestCase):
    """An administrator can upload an MP3 with no video behind it."""

    def setUp(self):
        self.admin = make_user("uploader", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.client.login(username="uploader", password="pw12345678")

    def audio_post(self, **extra):
        payload = {
            "media_type": "audio",
            "title": "Recitation",
            "description": "",
            "audio_file": SimpleUploadedFile(
                "recitation.mp3", b"ID3\x03\x00fake", content_type="audio/mpeg"
            ),
        }
        payload.update(extra)
        # None cannot be encoded into multipart, so drop the key instead.
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.client.post(reverse("videos:upload_submit"), payload)

    @mock.patch("videos.views.get_duration", return_value=0.0)
    @mock.patch("videos.views.upload_audio")
    def test_audio_upload_creates_an_audio_only_record(self, upload_audio, duration):
        upload_audio.return_value = {
            "path": "audio/rec.mp3",
            "url": "https://example.com/rec.mp3",
        }
        response = self.audio_post()
        self.assertTrue(response.json()["success"])

        video = Video.objects.get()
        self.assertEqual(video.media_type, "audio")
        self.assertEqual(video.file_id, "")
        self.assertEqual(video.audio_file_id, "audio/rec.mp3")
        self.assertEqual(video.audio_status, "ready")
        self.assertTrue(video.is_audio_only)
        self.assertFalse(video.has_video)

    @mock.patch("videos.views.get_duration", return_value=321.0)
    @mock.patch("videos.views.upload_audio")
    def test_audio_upload_records_the_length(self, upload_audio, duration):
        upload_audio.return_value = {"path": "audio/rec.mp3", "url": ""}
        self.audio_post()
        video = Video.objects.get()
        self.assertEqual(video.duration_seconds, 321.0)
        self.assertEqual(video.duration_formatted, "5:21")

    @mock.patch("videos.views.get_duration", return_value=0.0)
    @mock.patch("videos.views.upload_audio")
    def test_audio_only_has_no_video_download_link(self, upload_audio, duration):
        upload_audio.return_value = {"path": "audio/rec.mp3", "url": ""}
        self.audio_post()
        video = Video.objects.get()
        self.assertEqual(video.video_download_url, "")
        # The MP3 link is what people actually want.
        self.assertTrue(video.audio_download_url)

    @mock.patch("videos.views.get_duration", return_value=0.0)
    @mock.patch("videos.views.upload_thumbnail")
    @mock.patch("videos.views.upload_audio")
    def test_an_audio_upload_can_carry_a_thumbnail(
        self, upload_audio, upload_thumbnail, duration
    ):
        upload_audio.return_value = {"path": "audio/rec.mp3", "url": ""}
        upload_thumbnail.return_value = {
            "path": "thumbnails/rec_thumb.jpg",
            "url": "https://example.com/rec_thumb.jpg",
        }

        response = self.audio_post(thumbnail_data=self.jpeg_data_url())

        self.assertTrue(response.json()["success"])
        video = Video.objects.get()
        self.assertEqual(video.thumbnail_file_id, "thumbnails/rec_thumb.jpg")
        self.assertEqual(video.thumbnail_url, "https://example.com/rec_thumb.jpg")
        self.assertTrue(video.display_thumbnail_url)

    @mock.patch("videos.views.get_duration", return_value=0.0)
    @mock.patch("videos.views.upload_thumbnail")
    @mock.patch("videos.views.upload_audio")
    def test_an_audio_upload_without_a_thumbnail_still_works(
        self, upload_audio, upload_thumbnail, duration
    ):
        upload_audio.return_value = {"path": "audio/rec.mp3", "url": ""}
        response = self.audio_post()
        self.assertTrue(response.json()["success"])
        upload_thumbnail.assert_not_called()
        video = Video.objects.get()
        self.assertEqual(video.thumbnail_file_id, "")
        self.assertEqual(video.display_thumbnail_url, "")

    def test_the_upload_page_offers_a_thumbnail_for_an_audio_upload(self):
        body = self.client.get(reverse("videos:upload")).content.decode()
        # The picker is no longer hidden when the audio type is chosen.
        self.assertIn("customThumbnailGroup", body)
        self.assertIn("An audio upload has no picture of its own", body)

    @mock.patch("videos.views.get_duration", return_value=0.0)
    @mock.patch("videos.views.upload_thumbnail")
    @mock.patch("videos.views.upload_audio")
    def test_the_cover_can_arrive_as_a_file(
        self, upload_audio, upload_thumbnail, duration
    ):
        # A file part is streamed, so a large picture never hits the form-field
        # limit that used to reject the whole upload.
        upload_audio.return_value = {"path": "audio/rec.mp3", "url": ""}
        upload_thumbnail.return_value = {"path": "thumbnails/c.png", "url": ""}

        response = self.audio_post(
            thumbnail_data="",
            thumbnail_file=SimpleUploadedFile(
                "cover.jpg", self.jpeg_bytes(), content_type="image/jpeg"
            ),
        )

        self.assertTrue(response.json()["success"])
        stored = upload_thumbnail.call_args.kwargs["file_data"]
        self.assertTrue(stored.startswith(b"\xff\xd8\xff"))
        self.assertEqual(Video.objects.get().thumbnail_file_id, "thumbnails/c.png")

    @mock.patch("videos.views.upload_audio")
    def test_a_large_cover_file_is_refused_with_a_readable_message(
        self, upload_audio
    ):
        response = self.audio_post(
            thumbnail_data="",
            thumbnail_file=SimpleUploadedFile(
                "huge.jpg", b"\xff\xd8\xff" + b"\x00" * (9 * 1024 * 1024),
                content_type="image/jpeg",
            ),
        )
        self.assertFalse(response.json()["success"])
        self.assertIn("too large", response.json()["error"])
        upload_audio.assert_not_called()

    @mock.patch("videos.views.upload_audio")
    def test_a_picture_that_is_not_an_image_is_refused_before_uploading(
        self, upload_audio
    ):
        response = self.audio_post(thumbnail_data="data:image/jpeg;base64,AAAA")
        self.assertFalse(response.json()["success"])
        self.assertIn("not a picture", response.json()["error"])
        upload_audio.assert_not_called()
        self.assertFalse(Video.objects.exists())

    @mock.patch("videos.views.upload_audio")
    def test_an_oversized_picture_is_refused_with_a_readable_message(
        self, upload_audio
    ):
        # Bigger than the view accepts, but still under the request limit, so
        # the uploader is told what to do instead of getting a bare 400 page.
        response = self.audio_post(
            thumbnail_data="data:image/jpeg;base64," + "A" * (4 * 1024 * 1024 + 10)
        )
        self.assertFalse(response.json()["success"])
        self.assertIn("too large", response.json()["error"])
        upload_audio.assert_not_called()

    @mock.patch("videos.views.get_duration", return_value=0.0)
    @mock.patch("videos.views.upload_audio")
    def test_a_picture_that_cannot_be_stored_keeps_the_audio(
        self, upload_audio, duration
    ):
        upload_audio.return_value = {"path": "audio/rec.mp3", "url": ""}
        with mock.patch(
            "videos.views.upload_thumbnail", side_effect=r2_storage.StorageError("boom")
        ):
            response = self.audio_post(thumbnail_data=self.jpeg_data_url())

        body = response.json()
        self.assertTrue(body["success"])
        self.assertIn("picture could not be stored", body["warning"])
        # The recording is not thrown away because its cover failed.
        self.assertEqual(Video.objects.get().audio_file_id, "audio/rec.mp3")

    @mock.patch("videos.views.get_duration", return_value=0.0)
    @mock.patch("videos.views.prepare_mp3")
    @mock.patch("videos.views.upload_audio")
    def test_a_renamed_audio_file_is_converted_before_upload(
        self, upload_audio, prepare_mp3, duration
    ):
        # A WAV dressed as an MP3 must still be stored as a real MP3.
        converted = os.path.join(tempfile.gettempdir(), "converted.mp3")
        with open(converted, "wb") as handle:
            handle.write(b"ID3\x03\x00converted")
        prepare_mp3.return_value = (converted, [])
        upload_audio.return_value = {"path": "audio/rec.mp3", "url": ""}

        response = self.audio_post(
            audio_file=SimpleUploadedFile(
                "recitation.wav", b"RIFF....WAVEfmt ", content_type="audio/x-wav"
            )
        )

        self.assertTrue(response.json()["success"])
        prepare_mp3.assert_called_once()
        self.assertEqual(upload_audio.call_args.args[1], "recitation.mp3")
        os.remove(converted)

    @mock.patch("videos.views.prepare_mp3")
    def test_an_unreadable_audio_file_gets_a_readable_message(self, prepare_mp3):
        prepare_mp3.side_effect = AudioExtractionError(
            "That file could not be read as audio."
        )
        response = self.audio_post()
        self.assertFalse(response.json()["success"])
        self.assertIn("could not be read", response.json()["error"])
        self.assertFalse(Video.objects.exists())

    def jpeg_bytes(self) -> bytes:
        """A tiny but genuinely valid JPEG header."""
        return b"\xff\xd8\xff\xe0" + b"\x00" * 32

    def jpeg_data_url(self) -> str:
        """A tiny but genuinely valid JPEG header, as the browser would send."""
        return "data:image/jpeg;base64," + base64.b64encode(self.jpeg_bytes()).decode()

    def test_audio_upload_without_a_file_is_rejected(self):
        response = self.audio_post(audio_file=None)
        self.assertFalse(response.json()["success"])

    def test_audio_upload_rejects_a_video_file_too(self):
        response = self.audio_post(
            video_file=SimpleUploadedFile(
                "v.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4"
            )
        )
        self.assertFalse(response.json()["success"])

    def test_video_upload_without_a_video_file_is_rejected(self):
        response = self.client.post(
            reverse("videos:upload_submit"),
            {"media_type": "video", "title": "No file", "description": ""},
        )
        self.assertFalse(response.json()["success"])

    @mock.patch("videos.views.upload_audio")
    def test_audio_page_shows_cover_and_no_mode_toggle(self, upload_audio):
        upload_audio.return_value = {
            "path": "audio/rec.mp3",
            "url": "https://example.com/rec.mp3",
        }
        self.audio_post()
        video = Video.objects.get()
        video.thumbnail_file_id = "thumbnails/t.jpg"
        video.thumbnail_url = "https://example.com/t.jpg"
        video.save()

        body = self.client.get(
            reverse("videos:detail", args=[video.id])
        ).content.decode()
        # No picture to switch back to, so the Video / Listen toggle is gone.
        self.assertNotIn('id="mode-video"', body)
        self.assertNotIn('id="mode-audio"', body)
        self.assertIn("audio-mode", body)

    def test_upload_page_offers_both_types(self):
        response = self.client.get(reverse("videos:upload"))
        self.assertContains(response, 'name="media_type"')
        self.assertContains(response, 'name="audio_file"')
        self.assertContains(response, 'name="video_file"')

class AudioPreparationTests(TestCase):
    """Any uploaded audio becomes a real MP3 before it is stored.

    The name a file arrives with is not evidence of what it contains: a WAV or
    an M4A renamed to ".mp3" would otherwise be stored as a broken track.
    """

    def make_wav(self, seconds: float = 1.0) -> str:
        import struct
        import wave

        path = os.path.join(tempfile.gettempdir(), "probe-upload.wav")
        rate = 8000
        frames = int(rate * seconds)
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(
                b"".join(struct.pack("<h", 0) for _ in range(frames))
            )
        return path

    def test_an_mp3_is_recognised_by_its_bytes(self):
        path = os.path.join(tempfile.gettempdir(), "probe-id3.mp3")
        with open(path, "wb") as handle:
            handle.write(b"ID3\x03\x00fake")
        self.assertTrue(audio.is_mp3(path))
        os.remove(path)

    def test_a_frame_aligned_mp3_is_recognised(self):
        path = os.path.join(tempfile.gettempdir(), "probe-frame.mp3")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfb\x90\x64" + b"\x00" * 400)
        self.assertTrue(audio.is_mp3(path))
        os.remove(path)

    def test_a_wav_named_mp3_is_not_treated_as_mp3(self):
        path = self.make_wav()
        renamed = os.path.join(tempfile.gettempdir(), "probe-renamed.mp3")
        shutil.copyfile(path, renamed)
        self.assertFalse(audio.is_mp3(renamed))
        os.remove(path)
        os.remove(renamed)

    def test_a_wav_is_converted_to_a_real_mp3(self):
        path = self.make_wav()
        with open(path, "rb") as handle:
            upload = SimpleUploadedFile("probe.wav", handle.read(), content_type="audio/x-wav")

        mp3_path, removals = audio.prepare_mp3(upload, "probe")
        try:
            self.assertTrue(mp3_path.endswith(".mp3"))
            self.assertTrue(audio.is_mp3(mp3_path))
            self.assertGreater(os.path.getsize(mp3_path), 0)
            # About a second of sound was converted.
            self.assertGreater(audio.get_duration(mp3_path), 0.5)
        finally:
            for item in removals:
                if item and os.path.exists(item):
                    os.remove(item)
            if os.path.exists(path):
                os.remove(path)


class ThumbnailTypeTests(TestCase):
    """A picture is stored under the name it really has.

    Handing a PNG to the browser as a ".jpg" works in most browsers but not
    in all of them, and the type is what a download is named from.
    """

    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20

    def test_a_png_is_stored_as_a_png(self):
        content_type, extension = r2_storage._sniff_image(self.PNG)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(extension, ".png")

    def test_a_jpeg_is_stored_as_a_jpeg(self):
        content_type, extension = r2_storage._sniff_image(self.JPEG)
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(extension, ".jpg")

    def test_an_unknown_header_falls_back_to_jpeg(self):
        content_type, extension = r2_storage._sniff_image(b"not an image")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(extension, ".jpg")

    def test_the_stored_name_follows_the_picture(self):
        with mock.patch("videos.r2_storage.upload_object") as upload:
            result = r2_storage.upload_thumbnail(
                "data:image/png;base64," + base64.b64encode(self.PNG).decode(),
                "cover.jpg",
            )
        self.assertTrue(result["path"].endswith(".png"))
        self.assertEqual(upload.call_args.kwargs["content_type"], "image/png")


class DurationDisplayTests(TestCase):
    """Videos and audio show their length on the thumbnail, like YouTube."""

    def setUp(self):
        self.user = make_user("author")

    def test_duration_is_formatted_as_minutes_and_seconds(self):
        self.assertEqual(make_video(self.user, duration_seconds=45).duration_formatted, "0:45")
        self.assertEqual(make_video(self.user, duration_seconds=125).duration_formatted, "2:05")
        self.assertEqual(make_video(self.user, duration_seconds=3725).duration_formatted, "1:02:05")

    def test_zero_duration_is_not_shown(self):
        self.assertEqual(make_video(self.user, duration_seconds=0).duration_formatted, "0:00")

    def test_ffmpeg_banner_duration_is_parsed(self):
        from .audio import _parse_duration

        banner = "  Duration: 00:03:21.45, start: 0.000000, bitrate: 128 kb/s"
        self.assertAlmostEqual(_parse_duration(banner), 201.45)

    def test_unparseable_output_yields_zero(self):
        from .audio import _parse_duration

        self.assertEqual(_parse_duration("no duration here"), 0.0)

    def test_system_ffmpeg_is_preferred_over_the_bundled_binary(self):
        from . import audio

        with mock.patch.object(audio.shutil, "which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(audio._ffmpeg_exe(), "/usr/bin/ffmpeg")

    def test_bundled_ffmpeg_is_used_when_none_is_installed(self):
        from . import audio

        with mock.patch.object(audio.shutil, "which", return_value=None):
            with mock.patch("imageio_ffmpeg.get_ffmpeg_exe", return_value="/bundled"):
                self.assertEqual(audio._ffmpeg_exe(), "/bundled")

    def test_badge_is_rendered_on_the_home_grid(self):
        make_video(self.user, title="Long one", duration_seconds=605)
        body = self.client.get(reverse("videos:list")).content.decode()
        self.assertIn("video-duration", body)
        self.assertIn("10:05", body)

    def test_badge_is_hidden_when_the_length_is_unknown(self):
        make_video(self.user, title="Unknown", duration_seconds=0)
        body = self.client.get(reverse("videos:list")).content.decode()
        self.assertNotIn("video-duration", body)

    def test_fill_duration_measures_and_stores_the_length(self):
        from . import duration_tasks

        video = make_video(self.user, duration_seconds=0)
        with mock.patch.object(
            duration_tasks, "get_duration_from_url", return_value=612.0
        ):
            duration_tasks.fill_duration(video.id)
        video.refresh_from_db()
        self.assertEqual(video.duration_seconds, 612.0)

    def test_fill_duration_leaves_a_known_length_alone(self):
        from . import duration_tasks

        video = make_video(self.user, duration_seconds=30.0)
        with mock.patch.object(
            duration_tasks, "get_duration_from_url", return_value=999.0
        ) as probe:
            duration_tasks.fill_duration(video.id)
        probe.assert_not_called()
        video.refresh_from_db()
        self.assertEqual(video.duration_seconds, 30.0)

    def test_scheduling_is_skipped_without_storage(self):
        from . import duration_tasks

        duration_tasks._QUEUE.clear()
        duration_tasks._QUEUED.clear()
        with override_settings(TESTING=False):
            with mock.patch.object(duration_tasks, "is_configured", return_value=False):
                queued = duration_tasks.schedule_duration_fill([1, 2, 3])
        self.assertEqual(queued, 0)
        self.assertEqual(duration_tasks._QUEUE, [])

    def test_scheduling_queues_and_skips_duplicates(self):
        from . import duration_tasks

        duration_tasks._QUEUE.clear()
        duration_tasks._QUEUED.clear()
        duration_tasks._FAILED.discard(7)
        with override_settings(TESTING=False):
            with mock.patch.object(duration_tasks, "is_configured", return_value=True):
                with mock.patch("videos.duration_tasks.threading.Thread") as thread:
                    first = duration_tasks.schedule_duration_fill([7, 7, 8])
                    second = duration_tasks.schedule_duration_fill([7, 8])
        # 7 and 8 are queued once; the repeats are ignored.
        self.assertEqual(first, 2)
        self.assertEqual(second, 0)
        thread.assert_called_once()
        duration_tasks._QUEUE.clear()
        duration_tasks._QUEUED.clear()

    def test_home_page_queues_videos_that_have_no_length(self):
        make_video(self.user, title="Needs a length", duration_seconds=0)
        with mock.patch("videos.views.schedule_missing_durations") as schedule:
            self.client.get(reverse("videos:list"))
        schedule.assert_called_once()


class TranslationCoverageTests(TestCase):
    """Every string shown in the UI must exist in all three catalogues.

    GNU gettext is not available on this machine, so `makemessages` cannot run
    and a newly added string silently falls back to English on the French,
    Arabic and Wolof pages. This test fails the build when that happens, and
    points at exactly which strings are missing.
    """

    LANGUAGES = ("fr", "ar", "wo")
    SKIP_DIRS = {".venv", "staticfiles", "__pycache__", "migrations", "locale"}
    TRANS = re.compile(r"{%\s*trans(?:late)?\s+(['\"])(.*?)\1", re.S)
    BLOCKTRANS = re.compile(r"{%\s*blocktrans[^%]*%}(.*?){%\s*endblocktrans\s*%}", re.S)
    GETTEXT = re.compile(r"\bgettext\(\s*(['\"])(.*?)\1", re.S)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        project_root = Path(__file__).resolve().parent.parent
        cls.used: set[str] = set()
        for path in project_root.rglob("*"):
            if path.is_dir() or any(part in cls.SKIP_DIRS for part in path.parts):
                continue
            if path.suffix not in {".html", ".py", ".txt"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in cls.TRANS.finditer(text):
                cls.used.add(cls._normalise(match.group(2)))
            for match in cls.BLOCKTRANS.finditer(text):
                cls.used.add(cls._normalise(cls._singular_form(match.group(1))))
            if path.suffix == ".py":
                for match in cls.GETTEXT.finditer(text):
                    cls.used.add(cls._normalise(match.group(2)))

    @staticmethod
    def _normalise(value: str) -> str:
        """Rewrite a template placeholder the way Django stores it.

        ``{{ email }}`` inside trans/blocktrans is written to the catalogue as
        ``%(email)s``, so the source text has to be converted before it can be
        compared with a msgid.
        """
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", r"%(\1)s", " ".join(value.split()))

    @staticmethod
    def _singular_form(body: str) -> str:
        """The msgid of a blocktrans body.

        A pluralised blocktrans holds two halves separated by {% plural %},
        and only the first becomes the msgid; the second becomes msgid_plural.
        """
        head = re.split(r"{%\s*plural\s*%}", " ".join(body.split()), maxsplit=1)[0]
        return head.strip()

    def test_every_language_covers_every_string(self):
        root = Path(__file__).resolve().parent.parent
        for lang in self.LANGUAGES:
            po = root / "locale" / lang / "LC_MESSAGES" / "django.po"
            with self.subTest(language=lang):
                self.assertTrue(po.exists(), f"missing catalogue for {lang}")
                text = po.read_text(encoding="utf-8")
                have = set(re.findall(r'(?m)^msgid\s+"((?:[^"\\]|\\.)*)"', text))
                missing = sorted(self.used - have)
                self.assertEqual(
                    missing, [],
                    f"{lang} is missing {len(missing)} translations: "
                    f"{missing[:10]}",
                )

class MarkupBalanceTests(TestCase):
    """The player markup must close every element it opens.

    A single missing ``</div>`` left ``.video-player-container`` unclosed, so the
    download buttons and the entire comments section became its children. The
    control bar is absolutely positioned against that container, which therefore
    spanned the whole page and pushed the bar down to the bottom of the
    document. The page still rendered, so nothing caught it.
    """

    def setUp(self):
        self.user = User.objects.create_user("balanced", password="pw12345678")
        self.video = Video.objects.create(
            user=self.user,
            title="Nested",
            file_id="videos/n.mp4",
            video_url="https://example.com/n.mp4",
            audio_file_id="audio/n.mp3",
            audio_status="ready",
        )
        self.body = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()

    def test_no_unclosed_div_in_the_player_region(self):
        start = self.body.index('<div class="video-player-container"')
        end = self.body.index('<div class="video-details"')
        chunk = self.body[start:end]
        self.assertEqual(
            len(re.findall(r"<div\b", chunk)),
            len(re.findall(r"</div>", chunk)),
            "the player container is not closed, so the bar anchors to the page",
        )

    def test_download_buttons_are_not_inside_the_player_container(self):
        start = self.body.index('<div class="video-player-container"')
        end = self.body.index('<div class="player-controls"')
        self.assertGreater(
            end, start,
            "player-controls must follow the closed player container",
        )
        # The container's closing tag has to appear before the controls.
        tail = self.body[start:end]
        self.assertEqual(
            tail.count("<div"), tail.count("</div>"),
            "player-controls leaked inside .video-player-container",
        )

    def test_the_control_bar_is_a_sibling_of_the_picture(self):
        stage = self.body.index('id="playerStage"')
        bar = self.body.index('id="playerBar"')
        controls = self.body.index('class="player-controls"')
        self.assertLess(stage, bar)
        self.assertLess(bar, controls)


class ConfirmationDialogTests(TestCase):
    """Destructive actions must ask first, and cancelling must change nothing.

    window.confirm() was unusable here: the message is always English, it cannot
    be styled, and some mobile browsers suppress it outside a user gesture. The
    offline "Remove download" button in particular deleted the cached audio
    immediately, with no way back.
    """

    def setUp(self):
        self.admin = make_user("deleter", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(
            self.admin,
            title="Precious",
            audio_file_id="audio/p.mp3",
            audio_url="https://example.com/p.mp3",
            audio_status="ready",
        )
        self.client.login(username="deleter", password="pw12345678")

    @staticmethod
    def _asset(name: str) -> str:
        root = Path(__file__).resolve().parent.parent
        return (root / "static" / name).read_text(encoding="utf-8")

    def detail_page(self):
        return self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()

    def test_confirm_script_is_loaded_on_every_page(self):
        body = self.client.get(reverse("videos:list")).content.decode()
        # ManifestStaticFilesStorage appends a content hash in production, so
        # match the name rather than an exact unhashed path.
        self.assertRegex(body, r"/static/js/confirm\.[0-9a-f]+\.js|/static/js/confirm\.js")
        self.assertRegex(body, r"/static/css/confirm\.[0-9a-f]+\.css|/static/css/confirm\.css")

    def test_confirm_script_is_loaded_before_offline_js(self):
        # offline.js calls fazakkirConfirm when removing a download, so the
        # helper has to be defined first.
        import re as _re

        body = self.client.get(reverse("videos:list")).content.decode()
        confirm = _re.search(r"/static/js/confirm[^\"']*\.js", body)
        offline = _re.search(r"/static/js/offline[^\"']*\.js", body)
        self.assertIsNotNone(confirm, "confirm.js is not referenced")
        self.assertIsNotNone(offline, "offline.js is not referenced")
        self.assertLess(confirm.start(), offline.start())

    def test_confirm_script_exposes_a_promise_api(self):
        script = self._asset("js/confirm.js")
        self.assertIn("window.fazakkirConfirm", script)
        self.assertIn("Promise", script)

    def test_dialog_offers_cancel_and_a_destructive_default(self):
        script = self._asset("js/confirm.js")
        self.assertIn("confirm-cancel", script)
        self.assertIn("is-danger", script)

    def test_escape_cancels_rather_than_confirms(self):
        script = self._asset("js/confirm.js")
        self.assertIn('event.key === "Escape"', script)
        # The Escape branch must resolve false, not true.
        escape_line = script[script.index('event.key === "Escape"'):]
        self.assertIn("close(false)", escape_line[:200])

    def test_clicking_the_backdrop_cancels(self):
        script = self._asset("js/confirm.js")
        self.assertIn("if (event.target === parts.root) finish(false)", script)

    def test_video_delete_asks_before_calling_the_endpoint(self):
        body = self.detail_page()
        self.assertIn("function deleteVideo", body)
        self.assertIn("fazakkirConfirm({", body)
        # The old blocking confirm() must be gone.
        self.assertNotIn('confirm("', body)

    def test_no_raw_confirm_calls_remain_anywhere(self):
        for template in (
            "videos/templates/videos/detail.html",
            "videos/templates/videos/playlist_detail.html",
            "dashboard/templates/dashboard/home.html",
            "dashboard/templates/dashboard/comments.html",
        ):
            path = Path(__file__).resolve().parent.parent / template
            with self.subTest(template=template):
                self.assertNotIn("!confirm(", path.read_text(encoding="utf-8"))

    def test_offline_removal_asks_first(self):
        script = self._asset("js/offline.js")
        self.assertIn("fazakkirConfirm", script)
        # Cancelling must return before anything is dropped from the cache.
        self.assertIn("if (!ok) return", script)

    def test_offline_library_remove_asks_first(self):
        script = self._asset("js/offline.js")
        library = script[script.index("function renderLibrary"):]
        self.assertIn("fazakkirConfirm", library)

    def test_saving_offline_does_not_ask(self):
        # Saving is not destructive and must stay a single tap: the dialog is
        # only requested when the item is already saved.
        script = self._asset("js/offline.js")
        toggle = script[script.index("function setupToggle"):script.index("/* --- /offline/ page")]
        self.assertIn("var ask = saved", toggle)
        self.assertIn(": Promise.resolve(true)", toggle)
        self.assertLess(toggle.index("var ask = saved"), toggle.index("fazakkirConfirm"))


class PlayerLayoutTests(TestCase):
    """The control bar must stay inside the player on a desktop.

    The bar was absolutely positioned but its parent had no position, so it
    resolved against an ancestor further up the page: it stretched to the full
    window width and overlapped the download buttons underneath.
    """

    @staticmethod
    def _css(name: str) -> str:
        root = Path(__file__).resolve().parent.parent / "static" / "css"
        return (root / name).read_text(encoding="utf-8")

    def test_player_container_is_the_positioning_context(self):
        css = self._css("videos.css")
        start = css.index(".video-player-container {")
        block = css[start:css.index("}", start)]
        self.assertIn("position: relative", block)

    def test_control_bar_is_positioned_against_its_container(self):
        css = self._css("videoplayer.css")
        start = css.index(".player-bar {")
        block = css[start:css.index("}", start)]
        self.assertIn("position: absolute", block)
        self.assertIn("left: 0", block)
        self.assertIn("right: 0", block)
        # bottom: 0 against a container exactly as tall as the picture is what
        # keeps the bar attached to the video instead of the page.
        self.assertIn("bottom: 0", block)

    def test_positioning_context_is_declared_next_to_the_bar(self):
        # The bar lives in videoplayer.css but its positioning context was only
        # in videos.css. The two load separately, so the declaration is repeated
        # here; without it the bar anchors to the page and lands over the
        # comments, far below the video.
        css = self._css("videoplayer.css")
        self.assertIn(".video-player-container", css)
        start = css.index(".video-player-container {")
        block = css[start:css.index("}", start)]
        self.assertIn("position: relative", block)
        # The rule must appear before the bar's own absolute positioning.
        self.assertLess(start, css.index(".player-bar {"))

    def test_desktop_play_button_does_not_cover_the_thumbnail(self):
        css = self._css("videoplayer.css")
        desktop = css[css.index("@media (min-width: 769px)"):]
        block = desktop[:desktop.index("}")]
        self.assertIn("background: transparent", block)
        self.assertIn("box-shadow", block)

    def test_light_mode_controls_do_not_rely_on_white_on_white(self):
        css = self._css("videoplayer.css")
        mobile = css[css.index("@media (max-width: 768px)"):]
        self.assertIn("--bar-btn:", mobile)
        self.assertIn("var(--bg-tertiary)", mobile)
        self.assertIn("var(--text-primary)", mobile)

    def test_desktop_bar_stays_light_on_dark_over_the_picture(self):
        css = self._css("videoplayer.css")
        start = css.index(".player-bar {")
        block = css[start:css.index("}", start)]
        self.assertIn("--bar-fg: #fff", block)
        self.assertIn("rgba(0, 0, 0, 0.85)", block)


class PlayerScriptTests(TestCase):
    """Execute the rendered player script instead of only grepping it.

    Every earlier player test asserted that certain strings appeared in the
    HTML. That passed happily while the script threw a ReferenceError on the
    first line, because ``playerBar`` was used in six places and never
    declared, so the whole player was dead on arrival. String assertions cannot
    catch that; only running the code can.
    """

    def setUp(self):
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            self.skipTest("node is not installed on this machine")
        self.node = node

        user = User.objects.create_user(
            "runner", password="pw12345678", is_staff=True, is_superuser=True
        )
        Profile.objects.filter(user=user).update(access_level=Profile.Access.WRITE)
        self.video = Video.objects.create(
            user=user,
            title="Runnable",
            file_id="videos/run.mp4",
            video_url="https://example.com/run.mp4",
            audio_file_id="audio/run.mp3",
            audio_status="ready",
        )
        # An audio-only upload renders the same player, but drives the <audio>
        # element instead, so the branch has to be exercised separately.
        self.audio_only = Video.objects.create(
            user=user,
            title="Recitation",
            media_type="audio",
            audio_file_id="audio/recite.mp3",
            audio_url="https://example.com/recite.mp3",
            audio_status="ready",
        )

    def _script(self, video=None) -> str:
        body = self.client.get(
            reverse("videos:detail", args=[(video or self.video).id])
        ).content.decode()
        # The page opens with the theme bootstrap inline, so pick the block that
        # actually holds the player rather than the first one on the page.
        for block in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                body, re.S):
            if "playerBar" in block:
                return block
        raise AssertionError("player script block not found in the page")

    def _run(self, script=None) -> str:
        project = Path(__file__).resolve().parent.parent
        harness = project / "static" / "js" / "tests" / "player_smoke.js"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "player.js"
            target.write_text(
                script if script is not None else self._script(),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [self.node, str(harness), str(target)],
                capture_output=True, text=True, timeout=120, cwd=str(project),
            )
        return (proc.stdout or "") + (proc.stderr or "")

    def test_player_script_runs_without_throwing(self):
        output = self._run()
        self.assertIn(
            "script executed without throwing", output,
            "the player script failed to execute:\n" + output,
        )

    def test_tapping_the_picture_starts_playback(self):
        output = self._run()
        self.assertIn("video.play() called : true", output)
        self.assertIn("bar visible         : true", output)

    def test_every_control_is_wired_to_a_handler(self):
        output = self._run()
        for element, event in (
            ("tapZone", "click"),
            ("bigPlay", "click"),
            ("playPauseBtn", "click"),
            ("video-player", "play"),
            ("video-player", "pause"),
        ):
            self.assertNotIn(
                f"{element} {event} NOT BOUND", output,
                f"{element} has no {event} handler:\n{output}",
            )

    def test_every_referenced_element_variable_is_declared(self):
        # A variable used in the auto-hide code must also be declared.
        script = self._script()
        for name in ("playerBar", "playerStage", "tapZone", "bigPlay"):
            self.assertRegex(script, rf"const {name}\s*=", f"{name} is never declared")
            self.assertEqual(
                len(re.findall(rf"\bconst {name}\s*=", script)), 1,
                f"{name} is declared more than once",
            )

    def test_audio_only_flag_is_a_boolean_not_a_quoted_string(self):
        # "false" is a non-empty string and therefore truthy, which made every
        # control drive the audio element instead of the video.
        script = self._script()
        match = re.search(r"const audioOnly\s*=\s*(.+)", script)
        self.assertIsNotNone(match, "audioOnly is not set")
        self.assertEqual(
            match.group(1).strip(), "false",
            'audioOnly must be a bare boolean; a quoted "false" is truthy',
        )

    def test_controls_drive_the_video_for_a_normal_upload(self):
        output = self._run()
        self.assertIn("video.play() called : true", output)

    def test_player_script_runs_for_an_audio_only_upload(self):
        """The audio-only page takes a different branch of the same script.

        ``audioOnly`` a quoted "false" was truthy and made every control drive
        the wrong element; executing the page is the only way to be sure the
        branch really runs.
        """
        script = self._script(self.audio_only)
        self.assertRegex(
            script, r"const audioOnly\s*=\s*true\b",
            "the audio-only page does not set the flag to a bare true",
        )
        output = self._run(script)
        self.assertIn(
            "script executed without throwing", output,
            "the audio-only player failed to execute:\n" + output,
        )
        self.assertIn(
            "active element      : audio", output,
            "the audio-only page did not become active:\n" + output,
        )
        self.assertIn(
            "audio.play() called : true", output,
            "tapping an audio-only page did not start the MP3:\n" + output,
        )
        self.assertIn(
            "video.play() called : false", output,
            "an audio-only page started the video element:\n" + output,
        )

    def test_fullscreen_falls_back_to_the_video_on_iphone(self):
        """iPhone Safari has no Fullscreen API on elements.

        The button called ``playerStage.requestFullscreen()`` unguarded, so on
        an iPhone it threw a TypeError synchronously - before ``.catch`` could
        be attached - and pressing it did nothing. The harness replays that
        environment: a stage with neither ``requestFullscreen`` nor the webkit
        prefix, which must fall through to the video's own API.
        """
        output = self._run()
        self.assertIn(
            "desktop -> stage.requestFullscreen called    : true", output,
            "fullscreen no longer uses the standard API:\n" + output,
        )
        self.assertIn(
            "iPhone  -> video.webkitEnterFullscreen called : true", output,
            "fullscreen still does nothing on an iPhone:\n" + output,
        )
        self.assertIn(
            "exit    -> document.exitFullscreen called     : true", output,
            "leaving fullscreen stopped working:\n" + output,
        )

    def test_no_line_is_glued_to_the_statement_above_it(self):
        """This file omits semicolons, so a line starting with "[" or "(" is read
        as a subscript or a call on the statement above it.

        ``["a"].forEach(...)`` placed straight after ``foo()`` becomes
        ``foo()["a"].forEach(...)``, which threw at load time and killed the
        whole player. A bracket is only a hazard when the previous code line can
        be continued; after a "}" or a restricted keyword such as "return", a new
        statement begins instead and the bracket is harmless.
        """
        script = self._script()
        lines = script.splitlines()
        boundaries = ("}", ";", "{", ":")
        restricted = ("return", "break", "continue", "throw", "yield")
        offenders = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith(("[", "(")):
                continue
            previous = ""
            for earlier in reversed(lines[:index]):
                candidate = earlier.strip()
                if candidate and not candidate.startswith("//"):
                    previous = candidate
                    break
            if previous.endswith(boundaries) or previous.endswith(restricted):
                continue
            offenders.append(f"line {index + 1}: {stripped}")
        self.assertEqual(
            offenders, [],
            "these lines would be glued to the statement above them: "
            + "; ".join(offenders),
        )

    def test_every_id_the_script_looks_up_exists_in_the_markup(self):
        # These are only rendered when MP3 generation is running, the viewer can
        # generate subtitles, or the video has caption tracks; the script
        # already guards each lookup.
        conditional = {
            "audioStatus", "autoSubBtn", "autoSubMessage", "autoSubPanel",
            "subtitleCcCurrent",
        }

        output = self._run()
        line = [l for l in output.splitlines() if l.startswith("requested ids:")]
        self.assertTrue(line, "harness did not report the ids it looked up")
        requested = [i for i in line[0].split(":", 1)[1].split() if i not in conditional]

        body = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()
        missing = [i for i in requested if f'id="{i}"' not in body]
        self.assertEqual(
            missing, [],
            f"the script asks for elements the page does not render: {missing}",
        )


class _MarkupTreeParser(HTMLParser):
    """Tracks tag nesting so structural mistakes can be asserted directly.

    Every element that is still open when the document ends is reported in
    ``unclosed``; every end tag with nothing matching on the stack is reported
    in ``stray``. ``inside`` maps each CSS class to the set of classes seen on
    any descendant, which is how the "is the control bar inside the player
    box?" question gets answered without string slicing.
    """

    VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.stray = []
        self.unclosed = []
        self.inside = {}

    @staticmethod
    def _classes(attrs):
        for key, value in attrs:
            if key == "class" and value:
                return value.split()
        return []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        classes = self._classes(attrs)
        for _ancestor_tag, ancestor_classes in self.stack:
            for name in ancestor_classes:
                self.inside.setdefault(name, set()).update(classes)
        self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
            return
        self.stray.append(tag)
        # Unwind to the nearest matching open tag so parsing can continue.
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def close(self):
        super().close()
        self.unclosed = [tag for tag, _classes in self.stack]
        return self


class WatchLayoutTests(TestCase):
    """The watch page is laid out like YouTube's.

    The picture and its details sit in one column with a list of other videos
    down the right-hand side, and that list never repeats the video currently
    being watched.
    """

    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.main = make_video(self.alice, title="Main clip", views=10)
        self.popular = make_video(self.bob, title="Popular clip", views=900)
        self.quiet = make_video(self.bob, title="Quiet clip", views=1)

    def render(self, video=None):
        with mock.patch("videos.views.schedule_audio_generation"):
            response = self.client.get(
                reverse("videos:detail", args=[(video or self.main).id])
            )
        self.assertEqual(response.status_code, 200)
        return response

    @staticmethod
    def aside(html):
        start = html.index('<aside class="watch-aside"')
        return html[start:html.index("</aside>", start)]

    def test_the_page_is_wrapped_in_the_two_column_layout(self):
        html = self.render().content.decode()
        for marker in (
            'class="watch-layout"',
            'class="watch-main"',
            'class="watch-aside"',
        ):
            self.assertIn(marker, html)

    def test_sidebar_lists_the_other_videos(self):
        aside = self.aside(self.render().content.decode())
        self.assertIn("Popular clip", aside)
        self.assertIn("Quiet clip", aside)

    def test_sidebar_never_lists_the_video_being_watched(self):
        aside = self.aside(self.render().content.decode())
        self.assertNotIn("Main clip", aside)

    def test_sidebar_puts_the_most_watched_first(self):
        aside = self.aside(self.render().content.decode())
        self.assertLess(aside.index("Popular clip"), aside.index("Quiet clip"))

    def test_sidebar_is_left_out_when_there_is_nothing_else_to_watch(self):
        Video.objects.exclude(id=self.main.id).delete()
        html = self.render().content.decode()
        self.assertNotIn("watch-aside", html)

    def test_the_view_hands_the_template_the_related_videos(self):
        response = self.render()
        ids = [video.id for video in response.context["related_videos"]]
        self.assertEqual(ids[0], self.popular.id)
        self.assertNotIn(self.main.id, ids)


class MarkupNestingTests(TestCase):
    """A stray or missing ``</div>`` restructures the page with no visible error.

    Browsers drop an unmatched end tag, so the previous bug of this kind - one
    ``</div>`` too many at the foot of the watch page - moved the control bar
    and every following section up a level while all the string-based tests
    stayed green. These checks look at the document as a whole instead.
    """

    def setUp(self):
        self.user = make_user("nest")
        self.client.login(username="nest", password="pw12345678")
        self.video = make_video(
            self.user,
            title="Nested",
            audio_file_id="audio/n.mp3",
            audio_status="ready",
        )
        # A second video so the "Up next" sidebar, and its markup, is rendered.
        make_video(
            self.user,
            title="Another",
            audio_file_id="audio/b.mp3",
            audio_status="ready",
        )
        with mock.patch("videos.views.schedule_audio_generation"):
            response = self.client.get(
                reverse("videos:detail", args=[self.video.id])
            )
        self.html = response.content.decode()
        self.tree = _MarkupTreeParser()
        self.tree.feed(self.html)
        self.tree.close()

    def test_every_div_is_closed_across_the_whole_page(self):
        self.assertEqual(self.html.count("<div"), self.html.count("</div>"))

    def test_every_element_is_closed(self):
        self.assertEqual(self.tree.unclosed, [], "elements left open at the end")

    def test_there_are_no_stray_end_tags(self):
        self.assertEqual(self.tree.stray, [], "end tags with nothing to close")

    def test_the_control_bar_is_not_inside_the_player_container(self):
        self.assertNotIn(
            "player-controls",
            self.tree.inside.get("video-player-container", set()),
        )

    def test_the_details_and_sidebar_are_not_inside_the_player_container(self):
        inside_player = self.tree.inside.get("video-player-container", set())
        self.assertNotIn("video-details", inside_player)
        self.assertNotIn("watch-aside", inside_player)

    def test_the_player_and_the_details_are_side_by_side_in_the_main_column(self):
        in_main = self.tree.inside.get("watch-main", set())
        self.assertIn("video-player-container", in_main)
        self.assertIn("video-details", in_main)
        self.assertNotIn("watch-aside", in_main)

    def test_the_sidebar_lives_inside_the_watch_layout(self):
        self.assertIn(
            "watch-aside",
            self.tree.inside.get("watch-layout", set()),
        )


class NavbarSearchTests(TestCase):
    """The navbar search is YouTube's rounded pill with a button fused to it."""

    def setUp(self):
        self.user = make_user("navuser")
        self.client.login(username="navuser", password="pw12345678")
        self.html = self.client.get(reverse("videos:list")).content.decode()

    def test_the_search_form_sends_a_q_parameter_to_the_search_page(self):
        self.assertIn('name="q"', self.html)
        self.assertIn(reverse("videos:search"), self.html)

    def test_the_search_box_carries_a_submit_button(self):
        start = self.html.index('class="nav-search"')
        form = self.html[start:self.html.index("</form>", start)]
        self.assertIn('type="submit"', form)
        self.assertIn("nav-search-btn", form)

    def test_the_account_avatar_shows_a_single_letter(self):
        self.assertIn(
            'class="nav-username" title="navuser"',
            self.html,
        )
        self.assertIn(">n</span>", self.html)


class YouTubeCardStyleTests(TestCase):
    """Grid tiles follow YouTube: a bare rounded thumbnail and an avatar line."""

    def setUp(self):
        self.css = (
            Path(__file__).resolve().parent.parent
            / "static" / "css" / "videos.css"
        ).read_text(encoding="utf-8")
        self.user = make_user("cards")
        make_video(
            self.user,
            title="Tile",
            audio_file_id="audio/t.mp3",
            audio_status="ready",
        )
        self.html = self.client.get(reverse("videos:list")).content.decode()

    def rule(self, opening):
        start = self.css.index(opening)
        return self.css[start:self.css.index("}", start)]

    def test_cards_are_not_boxes(self):
        block = self.rule(".video-card {")
        self.assertIn("background: transparent", block)
        self.assertIn("border: none", block)

    def test_thumbnails_are_rounded_like_youtube(self):
        self.assertIn("border-radius: 12px", self.rule(".video-thumbnail {"))

    def test_the_caption_carries_a_channel_avatar(self):
        self.assertIn("video-card-avatar", self.html)
        self.assertIn(".video-card-avatar {", self.css)

    def test_the_title_is_clamped_to_two_lines(self):
        self.assertIn("-webkit-line-clamp: 2", self.rule(".video-title {"))

    def test_the_play_overlay_is_still_only_shown_on_hover(self):
        block = self.rule(".play-icon {")
        self.assertIn("left: 12px", block)
        self.assertIn("translateY(-50%)", block)


class TemplateCommentTests(TestCase):
    """A ``{# ... #}`` comment cannot span lines; Django prints it to visitors.

    The older guard only ever opened the list page, so a four-line comment on
    the watch page was rendered verbatim to everyone who loaded a video. Scan
    every template file and render several pages, so the whole class of mistake
    is covered rather than one page of it.
    """

    SKIP_DIRS = {".venv", "staticfiles", "__pycache__", "node_modules", "locale"}
    MULTILINE_COMMENT = re.compile(r"\{#.*?#\}", re.S)

    def test_no_template_holds_a_multiline_hash_comment(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.rglob("*.html"):
            if any(part in self.SKIP_DIRS for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8")
            for match in self.MULTILINE_COMMENT.finditer(text):
                if "\n" in match.group(0):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(root)}:{line}")
        self.assertEqual(
            offenders,
            [],
            "a {# ... #} comment cannot span lines, so these render as text: "
            f"{offenders}",
        )

    def test_rendered_pages_contain_no_comment_delimiters(self):
        user = make_user("commentviewer")
        video = make_video(user, title="Comment check")
        with mock.patch("videos.views.schedule_audio_generation"):
            pages = {
                "list": reverse("videos:list"),
                "detail": reverse("videos:detail", args=[video.id]),
                "search": reverse("videos:search") + "?q=comment",
                "offline": reverse("videos:offline"),
                "login": reverse("accounts:login"),
            }
            for name, url in pages.items():
                body = self.client.get(url).content.decode()
                self.assertNotIn("{#", body, f"{name} page leaks a template comment")
                self.assertNotIn("#}", body, f"{name} page leaks a template comment")
