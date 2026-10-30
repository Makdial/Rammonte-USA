import logging

from django.contrib.auth import login
from django.contrib.auth.models import User
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import CreateView

from core.permissions import read_required, write_required

from .forms import CustomUserCreationForm
from .models import Profile

logger = logging.getLogger(__name__)


class RegisterView(CreateView):
    form_class = CustomUserCreationForm
    success_url = reverse_lazy('accounts:login')
    template_name = "accounts/register.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object)
        return redirect("/")


# --- User management (administrators) --------------------------------------

def _profile_for(user) -> Profile:
    profile, _created = Profile.objects.get_or_create(user=user)
    return profile


def _target_guard(request, target: User):
    """Return an error string when ``request.user`` may not touch ``target``."""
    if target.id == request.user.id:
        return _("You cannot change your own account here.")
    if target.is_superuser and not request.user.is_superuser:
        return _("Only a superuser can manage another superuser.")
    return ""


@read_required
def manage_users(request):
    users = (
        User.objects.exclude(profile__is_guest=True)
        .annotate(video_total=Count("videos", distinct=True))
        .order_by("-is_superuser", "username")
    )
    rows = []
    for u in users:
        profile = _profile_for(u)
        rows.append(
            {
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "is_superuser": u.is_superuser,
                "is_active": u.is_active,
                "video_total": u.video_total,
                "access_level": profile.access_level,
                "access_label": profile.get_access_level_display(),
                "is_self": u.id == request.user.id,
                "protected": u.is_superuser and not request.user.is_superuser,
            }
        )

    return render(
        request,
        "accounts/manage_users.html",
        {
            "rows": rows,
            "access_choices": Profile.Access.choices,
            "total_users": len(rows),
        },
    )


@write_required
@require_POST
def user_set_blocked(request, user_id):
    target = get_object_or_404(User, id=user_id)
    error = _target_guard(request, target)
    if error:
        return JsonResponse({"success": False, "error": error}, status=403)

    blocked = request.POST.get("blocked") == "1"
    target.is_active = not blocked
    target.save(update_fields=["is_active"])

    return JsonResponse(
        {
            "success": True,
            "blocked": blocked,
            "message": _("User blocked") if blocked else _("User unblocked"),
        }
    )


@write_required
@require_POST
def user_set_access(request, user_id):
    target = get_object_or_404(User, id=user_id)
    error = _target_guard(request, target)
    if error:
        return JsonResponse({"success": False, "error": error}, status=403)

    level = request.POST.get("access_level", "")
    if level not in Profile.Access.values:
        return JsonResponse(
            {"success": False, "error": _("Invalid access level.")}, status=400
        )

    profile = _profile_for(target)
    profile.access_level = level
    profile.save(update_fields=["access_level"])

    # ``is_staff`` unlocks the Django admin; only read & write users get it so
    # read-only users cannot change data through the stock admin.
    target.is_staff = bool(target.is_superuser) or level == Profile.Access.WRITE
    target.save(update_fields=["is_staff"])

    return JsonResponse(
        {
            "success": True,
            "message": _("Access updated"),
            "access_level": level,
            "label": profile.get_access_level_display(),
        }
    )


@write_required
@require_POST
def user_delete(request, user_id):
    target = get_object_or_404(User, id=user_id)
    error = _target_guard(request, target)
    if error:
        return JsonResponse({"success": False, "error": error}, status=403)

    # Remove the R2 objects of every video this user uploaded, then let the
    # database CASCADE clean up the videos, comments, likes and playlists.
    for video in target.videos.all():
        video.delete_stored_files()

    username = target.username
    logger.info("User %r (id=%s) deleted by %r", username, target.id, request.user.username)
    target.delete()

    return JsonResponse(
        {"success": True, "message": _("User deleted"), "username": username}
    )
