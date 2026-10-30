"""Automatic, no-password accounts for visitors who are not signed in.

The site no longer asks viewers to sign in before they can comment, save a
playlist or like a video. Instead, each browser session is attached to a real
``User`` row marked as a guest. The rest of the code (comments, playlists,
likes, permission helpers and templates) can then keep using ``request.user``
unchanged, while administrators still sign in normally.
"""
from __future__ import annotations

import secrets

from django.contrib.auth import login
from django.contrib.auth.models import User

from .models import Profile

GUEST_FIRST_NAME = "Guest"


def is_guest_user(user) -> bool:
    """True when ``user`` is one of the automatic visitor accounts."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    try:
        profile = Profile.objects.get(user=user)
    except Profile.DoesNotExist:
        return False
    return bool(profile.is_guest)


def create_guest_user() -> User:
    """Create a fresh account that belongs to one browser session."""
    # ``create_user`` with no password stores an unusable password, so nobody
    # can ever sign in to this account with credentials.
    user = User.objects.create_user(
        username=f"guest_{secrets.token_hex(8)}",
        password=None,
        first_name=GUEST_FIRST_NAME,
    )
    # The post_save signal has already created the profile; make sure the
    # in-memory instance is refreshed too, so callers see is_guest=True
    # without an extra query.
    profile, _created = Profile.objects.get_or_create(user=user)
    profile.is_guest = True
    profile.save(update_fields=["is_guest"])
    return user


def login_guest(request) -> User:
    """Attach a new guest account to the current session."""
    user = create_guest_user()
    login(
        request,
        user,
        backend="django.contrib.auth.backends.ModelBackend",
    )
    return user
