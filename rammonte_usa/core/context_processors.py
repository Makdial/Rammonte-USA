"""Template context shared with every page.

Exposes ``can_read`` / ``can_write`` so templates never have to touch
``user.profile`` directly (which would break for anonymous visitors) and can
show or hide management controls accordingly.
"""
from __future__ import annotations


def access(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"can_read": False, "can_write": False, "is_guest": False}

    from core.permissions import get_profile

    profile = get_profile(user)
    return {
        "can_read": profile.can_read,
        "can_write": profile.can_write,
        "is_guest": profile.is_guest,
    }
