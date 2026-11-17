from django.contrib.auth.backends import ModelBackend

from .models import Profile


class AdminOnlyBackend(ModelBackend):
    """Authenticate only the superuser (and throw-away guest accounts)."""

    def user_can_authenticate(self, user):
        if not super().user_can_authenticate(user):
            return False
        # Guest accounts are created by the middleware with an unusable
        # password, so they can never be reached through the login form; allow
        # them so the automatic visitor identity keeps working.
        try:
            if user.profile.is_guest:
                return True
        except Profile.DoesNotExist:
            pass
        return bool(getattr(user, "is_superuser", False))