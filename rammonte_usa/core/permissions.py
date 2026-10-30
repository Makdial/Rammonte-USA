"""Shared permission helpers and view decorators.

The site has three levels of access, stored on :class:`accounts.models.Profile`:

* members (default) — browse, comment, keep playlists;
* read-only staff — may open the management dashboard and view users;
* read & write staff — may upload/delete videos and manage users.

Superusers always have full access.
"""
from __future__ import annotations

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _


def get_profile(user):
    """Return ``user.profile``, creating it if it is somehow missing."""
    from accounts.models import Profile

    profile, _created = Profile.objects.get_or_create(user=user)
    return profile


def user_can_write(user) -> bool:
    """True when ``user`` may modify the site."""
    return bool(user and user.is_authenticated and get_profile(user).can_write)


def user_can_read(user) -> bool:
    """True when ``user`` may at least view the management dashboard."""
    return bool(user and user.is_authenticated and get_profile(user).can_read)


def _gate(check, message):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not check(request.user):
                raise PermissionDenied(message)
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def write_required(view_func):
    """Allow only read & write staff (and superusers)."""
    return _gate(user_can_write, _("Only administrators can manage videos."))(view_func)


def read_required(view_func):
    """Allow read-only staff, read & write staff and superusers."""
    return _gate(user_can_read, _("Only administrators can view this page."))(view_func)


def login_required(view_func):
    """Like Django's ``login_required`` but keeps the original query string."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return view_func(request, *args, **kwargs)

    return _wrapped
