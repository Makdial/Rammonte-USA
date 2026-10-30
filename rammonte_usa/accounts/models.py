from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _


class Profile(models.Model):
    """Extra per-user information managed by administrators.

    ``access_level`` is what makes the "read and write" distinction possible:

    * ``member`` — a normal, registered user. Can comment, keep playlists and
      like videos, but cannot modify anything on the site.
    * ``read``   — read-only staff. Can open the management dashboard and see
      the user list, but cannot change or delete anything.
    * ``write``  — read & write staff. Can upload/delete videos, moderate
      comments and manage other users.

    Superusers always have full access regardless of this value.
    """

    class Access(models.TextChoices):
        MEMBER = "member", _("Member")
        READ = "read", _("Read-only")
        WRITE = "write", _("Read & write")

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    access_level = models.CharField(
        max_length=10,
        choices=Access.choices,
        default=Access.MEMBER,
    )
    # Visitors no longer have to create an account to comment, keep playlists
    # or like videos. Each browser session is given an automatic account marked
    # here, so the rest of the site keeps working exactly as it did for a
    # signed-in member. These accounts are hidden from the user management page
    # and from the dashboard's user count.
    is_guest = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Profile")
        verbose_name_plural = _("Profiles")

    def __str__(self):
        return f"{self.user.username} ({self.get_access_level_display()})"

    @property
    def can_write(self) -> bool:
        """True when the user may modify the site (upload, moderate, manage)."""
        return bool(self.user.is_superuser) or self.access_level == self.Access.WRITE

    @property
    def can_read(self) -> bool:
        """True when the user may at least open the management dashboard."""
        return bool(self.user.is_superuser) or self.access_level in (
            self.Access.READ,
            self.Access.WRITE,
        )


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile_for_new_user(sender, instance, created, **kwargs):
    """Make sure every account always has a ``Profile``."""
    if created:
        Profile.objects.get_or_create(user=instance)
