import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.models import Profile
from videos.models import Video

from . import storage
from .models import StorageMonitor


def make_user(username, **kwargs):
    return User.objects.create_user(username, password="pw12345678", **kwargs)


def set_access(user, level):
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.access_level = level
    profile.save(update_fields=["access_level"])
    user.is_staff = level == Profile.Access.WRITE or user.is_superuser
    user.save(update_fields=["is_staff"])
    user.profile = profile


def bucket(total=0, sizes=None, configured=True, error=""):
    sizes = sizes or {}
    return {
        "configured": configured,
        "error": error,
        "total": total,
        "count": len(sizes),
        "sizes": sizes,
        "by_prefix": {},
    }


class DashboardBaseTests(TestCase):
    """Fake the R2 listing so no test hits the network."""

    def setUp(self):
        self.bucket = bucket()
        patcher = mock.patch(
            "dashboard.storage.measure_bucket", side_effect=lambda: self.bucket
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.admin = make_user("root", is_staff=True, is_superuser=True)
        self.member = make_user("member")
        self.reader = make_user("reader")
        set_access(self.reader, Profile.Access.READ)


class StorageModelTests(DashboardBaseTests):
    def test_load_creates_singleton_with_defaults(self):
        monitor = StorageMonitor.load()
        self.assertEqual(monitor.pk, 1)
        self.assertEqual(monitor.alert_state, StorageMonitor.AlertState.OK)
        self.assertGreater(monitor.quota_bytes, 0)

    def test_load_is_idempotent(self):
        first = StorageMonitor.load()
        second = StorageMonitor.load()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(StorageMonitor.objects.count(), 1)

    def test_format_bytes(self):
        self.assertEqual(storage.format_bytes(0), "0 B")
        self.assertEqual(storage.format_bytes(1024), "1.0 KB")
        self.assertEqual(storage.format_bytes(1024 ** 3), "1.0 GB")

    def test_percent(self):
        self.assertEqual(storage.percent(50, 100), 50.0)
        self.assertEqual(storage.percent(10, 0), 0.0)


class DashboardAccessTests(DashboardBaseTests):
    def test_member_cannot_open_dashboard(self):
        self.client.login(username="member", password="pw12345678")
        self.assertEqual(self.client.get(reverse("dashboard:home")).status_code, 403)

    def test_anonymous_is_redirected(self):
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_reader_can_open_dashboard(self):
        self.client.login(username="reader", password="pw12345678")
        self.assertEqual(self.client.get(reverse("dashboard:home")).status_code, 200)

    def test_reader_cannot_cleanup(self):
        self.client.login(username="reader", password="pw12345678")
        self.assertEqual(self.client.post(reverse("dashboard:cleanup")).status_code, 403)

    def test_videos_and_comments_pages_render(self):
        self.client.login(username="root", password="pw12345678")
        self.assertEqual(self.client.get(reverse("dashboard:videos")).status_code, 200)
        self.assertEqual(self.client.get(reverse("dashboard:comments")).status_code, 200)


class CaptionColumnTests(DashboardBaseTests):
    """The videos table says which captions each video actually has."""

    def setUp(self):
        super().setUp()
        self.client.login(username="root", password="pw12345678")

    def render(self) -> str:
        return self.client.get(reverse("dashboard:videos")).content.decode()

    def test_a_video_with_captions_lists_the_languages(self):
        from videos.models import SubtitleTrack

        video = Video.objects.create(user=self.admin, title="With captions")
        for code in ("wo", "fr", "lyrics"):
            SubtitleTrack.objects.create(
                video=video, language_code=code, label=code, content="WEBVTT\n"
            )

        body = self.render()

        self.assertIn("lyrics", body)
        self.assertIn("fr, wo", body)

    def test_a_failed_video_shows_the_reason(self):
        Video.objects.create(
            user=self.admin,
            title="Failed captions",
            auto_subtitle_status="failed",
            auto_subtitle_error="This request exceeds your quota of 10000.",
        )
        body = self.render()
        self.assertIn("Failed", body)
        # The reason is available on hover, which is where the owner finds out
        # that the account, not the video, is the problem.
        self.assertIn("quota of 10000", body)

    def test_a_video_that_was_never_attempted_is_muted(self):
        Video.objects.create(user=self.admin, title="No captions yet")
        self.assertIn(">None<", self.render().replace("\n", ""))


class CleanupTests(DashboardBaseTests):
    def test_cleanup_removes_only_orphans(self):
        Video.objects.create(
            user=self.admin,
            title="Keep",
            file_id="videos/keep.mp4",
            video_url="https://example.com/keep.mp4",
        )
        self.bucket = bucket(
            total=1009, sizes={"videos/keep.mp4": 10, "audio/orphan.mp3": 999}
        )

        self.client.login(username="root", password="pw12345678")
        with mock.patch("videos.r2_storage.delete_file") as delete:
            response = self.client.post(reverse("dashboard:cleanup"))

        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["removed"], 1)
        delete.assert_called_once_with("audio/orphan.mp3")


class AlertTests(DashboardBaseTests):
    def _cross_threshold(self):
        monitor = StorageMonitor.load()
        monitor.quota_bytes = 100
        monitor.alert_threshold_percent = 90
        monitor.save(update_fields=["quota_bytes", "alert_threshold_percent"])
        return monitor

    def test_email_sent_when_crossing_threshold(self):
        self._cross_threshold()
        self.bucket = bucket(total=95, sizes={"videos/a.mp4": 95})

        with mock.patch("dashboard.notifications.send_mail") as send:
            report = storage.check_and_notify()

        self.assertEqual(report.alert_state, "warning")
        send.assert_called_once()
        recipients = send.call_args[0][3]
        self.assertEqual(recipients, ["makhoutech@gmail.com"])

    def test_no_duplicate_email_while_still_full(self):
        self._cross_threshold()
        self.bucket = bucket(total=95, sizes={"videos/a.mp4": 95})

        with mock.patch("dashboard.notifications.send_mail") as send:
            storage.check_and_notify()
            storage.check_and_notify()
        send.assert_called_once()

    def test_alert_rearms_after_space_freed(self):
        self._cross_threshold()
        self.bucket = bucket(total=95, sizes={"videos/a.mp4": 95})

        with mock.patch("dashboard.notifications.send_mail") as send:
            storage.check_and_notify()
            self.bucket = bucket(total=0)
            storage.check_and_notify()
            self.bucket = bucket(total=95, sizes={"videos/a.mp4": 95})
            storage.check_and_notify()

        self.assertEqual(send.call_count, 2)

    def test_no_email_below_threshold(self):
        self._cross_threshold()
        self.bucket = bucket(total=50, sizes={"videos/a.mp4": 50})

        with mock.patch("dashboard.notifications.send_mail") as send:
            storage.check_and_notify()
        send.assert_not_called()


class SettingsAndEmailTests(DashboardBaseTests):
    def test_update_settings(self):
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(
            reverse("dashboard:settings"),
            {
                "quota_gb": "5",
                "db_quota_gb": "2",
                "alert_threshold_percent": "80",
                "notify_email": "ops@example.com",
            },
        )
        self.assertTrue(response.json()["success"])

        monitor = StorageMonitor.load()
        self.assertEqual(monitor.quota_bytes, 5 * 1024 ** 3)
        self.assertEqual(monitor.db_quota_bytes, 2 * 1024 ** 3)
        self.assertEqual(monitor.alert_threshold_percent, 80)
        self.assertEqual(monitor.notify_email, "ops@example.com")

    def test_test_email_endpoint(self):
        self.client.login(username="root", password="pw12345678")
        with mock.patch("dashboard.notifications.send_mail") as send:
            response = self.client.post(reverse("dashboard:test_email"))
        self.assertTrue(response.json()["success"])
        send.assert_called_once()


class SubtitleSweepDashboardTests(DashboardBaseTests):
    """The "Generate subtitles for all" control on the dashboard."""

    def test_a_reader_cannot_start_a_run(self):
        self.client.login(username="reader", password="pw12345678")
        response = self.client.post(reverse("dashboard:subtitle_generate"))
        self.assertEqual(response.status_code, 403)

    def test_an_administrator_starts_a_run(self):
        self.client.login(username="root", password="pw12345678")
        with mock.patch("dashboard.views.start_subtitle_sweep", return_value=True):
            response = self.client.post(reverse("dashboard:subtitle_generate"))
        self.assertTrue(response.json()["success"])
        self.assertTrue(response.json()["started"])

    def test_a_run_that_is_already_going_says_so(self):
        self.client.login(username="root", password="pw12345678")
        with mock.patch("dashboard.views.start_subtitle_sweep", return_value=False):
            response = self.client.post(reverse("dashboard:subtitle_generate"))
        self.assertFalse(response.json()["started"])

    def test_the_status_counts_the_videos_in_the_database(self):
        # Each worker answers from its own memory, so the counters must come
        # from the database: the worker that started the run is not necessarily
        # the one answering this request.
        self.client.login(username="root", password="pw12345678")
        for index, status in enumerate(("ready", "ready", "failed", "processing")):
            Video.objects.create(
                user=self.admin,
                title=f"video {index}",
                audio_file_id=f"audio/{index}.mp3",
                auto_subtitle_status=status,
            )
        # A video with no audio can never be captioned and is not counted.
        Video.objects.create(user=self.admin, title="silent", file_id="videos/x.mp4")

        data = self.client.get(reverse("dashboard:subtitle_status")).json()

        self.assertTrue(data["running"])  # one video is in flight right now
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["done"], 3)
        self.assertEqual(data["succeeded"], 2)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["processing"], 1)

    def test_a_run_that_ended_is_not_reported_as_running(self):
        self.client.login(username="root", password="pw12345678")
        Video.objects.create(
            user=self.admin,
            title="done",
            audio_file_id="audio/done.mp3",
            auto_subtitle_status="ready",
        )
        with mock.patch(
            "dashboard.views.subtitle_sweep_status",
            return_value={"running": False, "total": 0, "done": 0,
                          "succeeded": 0, "failed": 0},
        ):
            data = self.client.get(reverse("dashboard:subtitle_status")).json()
        self.assertFalse(data["running"])
        self.assertEqual(data["done"], 1)

    def test_the_start_needs_a_post(self):
        self.client.login(username="root", password="pw12345678")
        self.assertEqual(
            self.client.get(reverse("dashboard:subtitle_generate")).status_code, 405
        )


class AudioDashboardTests(DashboardBaseTests):
    """The "Generate missing MP3s" control on the dashboard videos tab."""

    def _video(self, title, **kwargs):
        kwargs.setdefault("file_id", f"videos/{title}.mp4")
        kwargs.setdefault("video_url", f"https://example.com/{title}.mp4")
        return Video.objects.create(user=self.admin, title=title, **kwargs)

    def test_summary_and_button_shown_to_writer(self):
        self._video("Needs audio")
        self.client.login(username="root", password="pw12345678")
        content = self.client.get(reverse("dashboard:videos")).content.decode()
        self.assertIn("Generate missing MP3s", content)
        self.assertIn("video needs an MP3", content)

    def test_writer_who_can_write_but_not_staff_still_sees_button(self):
        user = make_user("mod")
        set_access(user, Profile.Access.WRITE)
        self._video("Needs audio")
        self.client.login(username="mod", password="pw12345678")
        content = self.client.get(reverse("dashboard:videos")).content.decode()
        self.assertIn("Generate missing MP3s", content)

    def test_reader_sees_counts_but_no_button(self):
        self._video("Needs audio")
        self.client.login(username="reader", password="pw12345678")
        content = self.client.get(reverse("dashboard:videos")).content.decode()
        self.assertNotIn("Generate missing MP3s", content)
        self.assertIn("video needs an MP3", content)

    def test_no_button_when_every_video_has_audio(self):
        self._video("Ready", audio_file_id="audio/a.mp3", audio_status="ready")
        self.client.login(username="root", password="pw12345678")
        content = self.client.get(reverse("dashboard:videos")).content.decode()
        self.assertNotIn("Generate missing MP3s", content)
        self.assertIn("Every video has an MP3", content)

    def test_table_shows_mp3_state_per_video(self):
        self._video("Ready", audio_file_id="audio/a.mp3", audio_status="ready")
        self._video("Silent", audio_status="none")
        self.client.login(username="root", password="pw12345678")
        content = self.client.get(reverse("dashboard:videos")).content.decode()
        self.assertIn("No audio track", content)
        self.assertIn(">Ready<", content)

    def test_reader_cannot_start_generation(self):
        self.client.login(username="reader", password="pw12345678")
        response = self.client.post(reverse("dashboard:audio_generate"))
        self.assertEqual(response.status_code, 403)

    @mock.patch("dashboard.views.start_audio_sweep", return_value=True)
    def test_admin_starts_generation(self, start):
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(reverse("dashboard:audio_generate"))
        self.assertTrue(response.json()["success"])
        self.assertTrue(response.json()["started"])
        start.assert_called_once()

    @mock.patch("dashboard.views.start_audio_sweep", return_value=False)
    def test_second_run_is_reported_as_already_running(self, start):
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(reverse("dashboard:audio_generate"))
        self.assertTrue(response.json()["success"])
        self.assertFalse(response.json()["started"])

    def test_generate_requires_post(self):
        self.client.login(username="root", password="pw12345678")
        self.assertEqual(
            self.client.get(reverse("dashboard:audio_generate")).status_code, 405
        )

    def test_status_endpoint_reports_missing_videos(self):
        self._video("Untouched")
        self._video("Broken", audio_status="failed")
        self._video("Silent", audio_status="none")
        self._video("Ready", audio_file_id="audio/a.mp3", audio_status="ready")

        self.client.login(username="reader", password="pw12345678")
        data = self.client.get(reverse("dashboard:audio_status")).json()

        self.assertEqual(data["missing"], 2)
        self.assertEqual(data["no_audio"], 1)
        self.assertEqual(data["processing"], 0)
        self.assertIn("running", data)
        self.assertIn("total", data)


class VideoOrderTests(DashboardBaseTests):
    """The "Home order" page that rearranges the home page for everybody."""

    def _video(self, title, **kwargs):
        kwargs.setdefault("file_id", f"videos/{title}.mp4")
        kwargs.setdefault("video_url", f"https://example.com/{title}.mp4")
        return Video.objects.create(user=self.admin, title=title, **kwargs)

    def home_order(self):
        return list(Video.objects.values_list("title", flat=True))

    def test_reader_can_open_the_page_but_not_save(self):
        self._video("A")
        self.client.login(username="reader", password="pw12345678")
        self.assertEqual(
            self.client.get(reverse("dashboard:video_order")).status_code, 200
        )
        self.assertEqual(
            self.client.post(reverse("dashboard:video_order_save")).status_code, 403
        )

    def test_page_lists_videos_in_home_order(self):
        first = self._video("First")
        second = self._video("Second")
        # Nothing has been arranged yet, so the newest upload leads.
        self.assertEqual(self.home_order(), ["Second", "First"])
        self.client.login(username="root", password="pw12345678")
        body = self.client.get(reverse("dashboard:video_order")).content.decode()
        self.assertLess(
            body.index(f'data-id="{second.id}"'), body.index(f'data-id="{first.id}"')
        )

    def test_saving_a_new_order_changes_the_home_page(self):
        first = self._video("First")
        second = self._video("Second")
        self.client.login(username="root", password="pw12345678")

        # Put the older video back on top, which the default order never does.
        response = self.client.post(
            reverse("dashboard:video_order_save"),
            data=json.dumps([first.id, second.id]),
            content_type="application/json",
        )

        self.assertTrue(response.json()["success"])
        self.assertEqual(self.home_order(), ["First", "Second"])

    def test_order_survives_for_every_visitor(self):
        first = self._video("First")
        second = self._video("Second")
        self.client.login(username="root", password="pw12345678")
        self.client.post(
            reverse("dashboard:video_order_save"),
            data=json.dumps([first.id, second.id]),
            content_type="application/json",
        )
        self.client.logout()

        # An anonymous visitor to the home page sees the arranged order.
        body = self.client.get(reverse("videos:list")).content.decode()
        self.assertLess(body.index("First"), body.index("Second"))

    def test_new_upload_leads_the_arranged_rows(self):
        first = self._video("First")
        second = self._video("Second")
        self.client.login(username="root", password="pw12345678")
        self.client.post(
            reverse("dashboard:video_order_save"),
            data=json.dumps([first.id, second.id]),
            content_type="application/json",
        )
        # A brand-new upload is not numbered, so it still comes first.
        self._video("Newest")
        self.assertEqual(self.home_order(), ["Newest", "First", "Second"])

    def test_unknown_ids_are_ignored(self):
        first = self._video("First")
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(
            reverse("dashboard:video_order_save"),
            data=json.dumps(["999999", first.id]),
            content_type="application/json",
        )
        self.assertTrue(response.json()["success"])
        self.assertEqual(self.home_order(), ["First"])

    def test_empty_payload_is_rejected(self):
        self._video("First")
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(
            reverse("dashboard:video_order_save"),
            data=json.dumps([]),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_malformed_payload_is_rejected(self):
        self._video("First")
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(
            reverse("dashboard:video_order_save"),
            data="{not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_save_requires_post(self):
        self.client.login(username="root", password="pw12345678")
        self.assertEqual(
            self.client.get(reverse("dashboard:video_order_save")).status_code, 405
        )
