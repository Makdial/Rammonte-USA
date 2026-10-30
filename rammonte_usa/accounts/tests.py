from unittest import mock

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import Profile


def make_user(username, password="pw12345678", **kwargs):
    return User.objects.create_user(username, password=password, **kwargs)


def set_access(user, level):
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.access_level = level
    profile.save(update_fields=["access_level"])
    user.is_staff = level == Profile.Access.WRITE or user.is_superuser
    user.save(update_fields=["is_staff"])
    # Keep the reverse one-to-one cache on ``user`` in sync with the update.
    user.profile = profile


class ProfileTests(TestCase):
    def test_profile_created_for_new_user(self):
        user = make_user("newbie")
        self.assertTrue(Profile.objects.filter(user=user).exists())

    def test_superuser_can_write(self):
        admin = make_user("root", is_staff=True, is_superuser=True)
        self.assertTrue(admin.profile.can_write)
        self.assertTrue(admin.profile.can_read)

    def test_read_level_cannot_write(self):
        user = make_user("reader")
        set_access(user, Profile.Access.READ)
        self.assertTrue(user.profile.can_read)
        self.assertFalse(user.profile.can_write)

    def test_write_level_can_write(self):
        user = make_user("writer")
        set_access(user, Profile.Access.WRITE)
        self.assertTrue(user.profile.can_write)
        self.assertTrue(user.is_staff)


class ManageUsersTests(TestCase):
    def setUp(self):
        self.admin = make_user("root", is_staff=True, is_superuser=True)
        set_access(self.admin, Profile.Access.WRITE)
        self.member = make_user("member")
        self.reader = make_user("reader")
        set_access(self.reader, Profile.Access.READ)

    def test_member_cannot_open_manage_page(self):
        self.client.login(username="member", password="pw12345678")
        response = self.client.get(reverse("accounts:manage_users"))
        self.assertEqual(response.status_code, 403)

    def test_reader_can_open_manage_page(self):
        self.client.login(username="reader", password="pw12345678")
        response = self.client.get(reverse("accounts:manage_users"))
        self.assertEqual(response.status_code, 200)

    def test_reader_cannot_change_access(self):
        self.client.login(username="reader", password="pw12345678")
        response = self.client.post(
            reverse("accounts:user_access", args=[self.member.id]),
            {"access_level": Profile.Access.WRITE},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_grants_write(self):
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(
            reverse("accounts:user_access", args=[self.member.id]),
            {"access_level": Profile.Access.WRITE},
        )
        self.assertTrue(response.json()["success"])
        self.member.refresh_from_db()
        profile = Profile.objects.get(user=self.member)
        self.assertTrue(profile.can_write)
        self.assertTrue(self.member.is_staff)

    def test_invalid_access_level(self):
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(
            reverse("accounts:user_access", args=[self.member.id]),
            {"access_level": "wizard"},
        )
        self.assertEqual(response.status_code, 400)

    def test_block_and_unblock(self):
        self.client.login(username="root", password="pw12345678")
        url = reverse("accounts:user_block", args=[self.member.id])

        self.client.post(url, {"blocked": "1"})
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

        self.client.post(url, {"blocked": "0"})
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)

    def test_cannot_block_self(self):
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(
            reverse("accounts:user_block", args=[self.admin.id]), {"blocked": "1"}
        )
        self.assertEqual(response.status_code, 403)

    def test_blocked_user_cannot_login(self):
        self.client.login(username="root", password="pw12345678")
        self.client.post(reverse("accounts:user_block", args=[self.member.id]), {"blocked": "1"})
        self.client.logout()
        self.assertFalse(self.client.login(username="member", password="pw12345678"))

    def test_member_cannot_open_upload(self):
        self.client.login(username="member", password="pw12345678")
        self.assertEqual(self.client.get(reverse("videos:upload")).status_code, 403)

    @mock.patch("videos.r2_storage.delete_file")
    def test_delete_user_removes_videos(self, _delete):
        from videos.models import Video

        Video.objects.create(
            user=self.member,
            title="Mine",
            file_id="videos/mine.mp4",
            video_url="https://example.com/mine.mp4",
        )
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(reverse("accounts:user_delete", args=[self.member.id]))
        self.assertTrue(response.json()["success"])
        self.assertFalse(User.objects.filter(id=self.member.id).exists())
        self.assertEqual(Video.objects.count(), 0)

class PasswordResetTests(TestCase):
    """A locked-out user can set a new password by email."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="forgetful",
            password="oldpassword123",
            email="forgetful@example.com",
        )

    def confirm_url(self, user=None, token=None):
        user = user or self.user
        token = token or default_token_generator.make_token(user)
        return reverse(
            "accounts:password_reset_confirm",
            args=[urlsafe_base64_encode(force_bytes(user.pk)), token],
        )

    def test_login_page_offers_a_reset_link(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertContains(response, reverse("accounts:password_reset"))

    def test_request_form_renders(self):
        response = self.client.get(reverse("accounts:password_reset"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="email"')

    def test_reset_sends_an_email_with_a_link(self):
        response = self.client.post(
            reverse("accounts:password_reset"), {"email": self.user.email}
        )
        self.assertRedirects(
            response,
            reverse("accounts:password_reset_done"),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.email, mail.outbox[0].to)
        self.assertIn("/reset/", mail.outbox[0].body)

    def test_unknown_address_does_not_leak_that_it_is_unknown(self):
        # The response must look identical either way, so the form cannot be
        # used to discover which addresses have an account.
        known = self.client.post(
            reverse("accounts:password_reset"), {"email": self.user.email}
        )
        unknown = self.client.post(
            reverse("accounts:password_reset"), {"email": "nobody@example.com"}
        )
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known["Location"], unknown["Location"])

    def test_following_the_link_sets_a_new_password(self):
        url = self.confirm_url()
        # Django 6 parks the token in the session and redirects to the
        # set-password path, so follow that to reach the form.
        landing = self.client.get(url)
        self.assertEqual(landing.status_code, 302)
        form_url = landing["Location"]
        self.assertIn("set-password", form_url)
        self.assertEqual(self.client.get(form_url).status_code, 200)

        response = self.client.post(
            form_url,
            {"new_password1": "brandnewpass99", "new_password2": "brandnewpass99"},
        )
        self.assertRedirects(
            response,
            reverse("accounts:password_reset_complete"),
            fetch_redirect_response=False,
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("brandnewpass99"))
        self.assertFalse(self.user.check_password("oldpassword123"))

    def test_mismatched_confirmation_is_rejected(self):
        form_url = self.client.get(self.confirm_url())["Location"]
        response = self.client.post(
            form_url,
            {"new_password1": "brandnewpass99", "new_password2": "somethingelse99"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error-list")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("oldpassword123"))

    def test_forged_token_is_refused(self):
        url = self.confirm_url(token="not-a-real-token")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Offers a fresh link instead of a password form.
        self.assertContains(response, reverse("accounts:password_reset"))

    def test_new_password_can_be_used_to_sign_in(self):
        form_url = self.client.get(self.confirm_url())["Location"]
        self.client.post(
            form_url,
            {"new_password1": "brandnewpass99", "new_password2": "brandnewpass99"},
        )
        self.assertTrue(
            self.client.login(username="forgetful", password="brandnewpass99")
        )