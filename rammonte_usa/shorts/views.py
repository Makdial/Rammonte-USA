from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST, require_GET

from core.permissions import write_required

from . import r2_storage
from .models import Short


def short_list(request):
    shorts = Short.objects.select_related("user").all()
    return render(request, "shorts/list.html", {"shorts": shorts})


@require_GET
def short_detail(request, short_id):
    short = get_object_or_404(Short, id=short_id)
    short.views += 1
    short.save(update_fields=["views"])
    return render(request, "shorts/detail.html", {"short": short})


@write_required
def short_upload_page(request):
    return render(request, "shorts/upload.html", {"storage_ready": r2_storage.is_configured()})


@write_required
@require_POST
def short_upload(request):
    upload = request.FILES.get("video_file")
    if not upload:
        return JsonResponse({"success": False, "error": _("Please choose a video file.")}, status=400)
    title = (request.POST.get("title") or "").strip() or Path(upload.name).stem
    try:
        result = r2_storage.upload_video(file_data=upload, file_name=upload.name)
    except r2_storage.StorageError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
    short = Short.objects.create(
        user=request.user,
        title=title[:120],
        description=(request.POST.get("description") or "").strip(),
        file_id=result["path"],
        video_url=result["url"],
    )
    return JsonResponse({"success": True, "short_id": short.id, "message": _("Short uploaded")})


@write_required
@require_POST
def short_delete(request, short_id):
    short = get_object_or_404(Short, id=short_id)
    short.delete_stored_files()
    short.delete()
    return JsonResponse({"success": True, "message": _("Short deleted")})