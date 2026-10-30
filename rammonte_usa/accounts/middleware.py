"""Give every visitor a guest identity without asking them to sign in."""
from __future__ import annotations

from django.contrib.auth import logout

from .guests import is_guest_user, login_guest

# Sign-in, registration, the Django admin and the management dashboard keep
# working normally: an existing guest is signed out there so an administrator
# can enter their own credentials. Static files and probes never need an
# account.
_EXEMPT_PREFIXES = (
    "/accounts/",
    "/admin/",
    "/manage/",
    "/static/",
    "/media/",
    "/i18n/",
)
_EXEMPT_PATHS = {
    "/healthz/",
    "/manifest.webmanifest",
    "/sw.js",
    "/favicon.ico",
}


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


class GuestAccessMiddleware:
    """Sign anonymous visitors in as a throw-away guest account.

    This runs after ``AuthenticationMiddleware`` so ``request.user`` is already
    available. Existing real accounts are left untouched.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _is_exempt(request.path_info):
            # On the account/admin pages a guest must step aside so the login
            # form is reachable (Django redirects already-authenticated users
            # away from the login page).
            if is_guest_user(getattr(request, "user", None)):
                logout(request)
            return self.get_response(request)

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            login_guest(request)

        return self.get_response(request)
