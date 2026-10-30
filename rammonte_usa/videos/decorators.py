"""Reusable view decorators.

Kept as a thin re-export of :mod:`core.permissions` so that older imports keep
working. ``admin_required`` is a backwards-compatible alias for
``write_required``.
"""
from core.permissions import (  # noqa: F401
    login_required,
    read_required,
    user_can_read,
    user_can_write,
    write_required,
)

admin_required = write_required

__all__ = [
    "admin_required",
    "login_required",
    "read_required",
    "user_can_read",
    "user_can_write",
    "write_required",
]
