"""Project-wide views that are not tied to a single app.

Serving the web app manifest and the service worker from the project root keeps
the service worker's scope at ``/`` (a worker served from ``/static/`` could
only control that folder).
"""
from __future__ import annotations

import json

from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@require_GET
def health(request):
    """Liveness/readiness probe used by the host (Northflank, Render, ...).

    Checks that the database answers as well, because a container can be up
    while its database addon is still starting; failing here lets the platform
    retry instead of serving 500s to visitors.
    """
    database_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - any database error is a failed probe
        database_ok = False

    return JsonResponse(
        {"status": "ok" if database_ok else "degraded", "database": database_ok},
        status=200 if database_ok else 503,
    )


@require_GET
def manifest(request):
    """The Web App Manifest that makes Rammonte USA installable."""
    data = {
        "name": "Rammonte USA",
        "short_name": "Rammonte USA",
        "description": _("Watch and listen to videos, online or offline."),
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0f0f0f",
        "theme_color": "#0f0f0f",
        "icons": [
            {
                "src": static("icons/icon.svg"),
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            },
            {
                "src": static("icons/icon.svg"),
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "maskable",
            },
        ],
    }
    return HttpResponse(
        json.dumps(data, indent=2, ensure_ascii=False),
        content_type="application/manifest+json",
    )


@require_GET
@never_cache
def service_worker(request):
    """Serve the service worker from the site root with the right headers."""
    content = render_to_string("core/sw.js")
    response = HttpResponse(content, content_type="application/javascript")
    # Allow the worker to control the whole site even though it is served from
    # the root file ``/sw.js`` rather than a directory.
    response["Service-Worker-Allowed"] = "/"
    return response
