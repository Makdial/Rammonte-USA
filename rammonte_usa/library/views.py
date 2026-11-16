import json
import mimetypes
import os

def _extract_text(file_obj, content_type):
    text = ""
    name = getattr(file_obj, "name", "") or ""
    if (content_type or "").lower().find("pdf") != -1 or name.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_obj)
            parts = []
            for page in reader.pages:
                parts.append(page.extract_text() or "")
            text = "\n\n".join(parts)
        except Exception:
            text = ""
    else:
        try:
            file_obj.seek(0)
            data = file_obj.read()
            if isinstance(data, bytes):
                text = data.decode("utf-8", errors="ignore")
            else:
                text = str(data)
        except Exception:
            text = ""
    return text.strip()
import urllib.parse
import urllib.request

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core.permissions import write_required

from .forms import BookForm
from .models import Book
from videos.r2_storage import (
    is_configured,
    upload_object,
    resolve_url,
    download_url,
    delete_object,
    _object_path,
)


def _translate_text(text, source, target):
    text = (text or "").strip()
    if not text:
        return ""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        try:
            import openai
            client = openai.OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": f"Translate the following text from {source} to {target}. Return only the translation."},
                    {"role": "user", "content": text},
                ],
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            pass
    gkey = os.environ.get("GOOGLE_API_KEY", "").strip()
    if gkey:
        try:
            url = "https://translation.googleapis.com/language/translate/v2?key=" + gkey
            data = urllib.parse.urlencode({"q": text, "source": source, "target": target, "format": "text"}).encode()
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.loads(r.read().decode())
                return j["data"]["translations"][0]["translatedText"]
        except Exception:
            pass
    try:
        params = urllib.parse.urlencode({"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text})
        url = "https://translate.googleapis.com/translate_a/single?" + params
        with urllib.request.urlopen(url, timeout=30) as r:
            j = json.loads(r.read().decode())
            return "".join(part[0] for part in j[0] if part and part[0])
    except Exception as exc:
        return f"Translation failed: {exc}"


def library_list(request):
    query = (request.GET.get("q") or "").strip()
    sort = (request.GET.get("sort") or "newest").strip()
    if request.user.is_staff or request.user.is_superuser:
        books = Book.objects.all()
    else:
        books = Book.objects.filter(is_public=True)
    if query:
        from django.db.models import Q
        books = books.filter(
            Q(title__icontains=query)
            | Q(author__icontains=query)
            | Q(description__icontains=query)
        )
    if sort == "title":
        books = books.order_by("title")
    elif sort == "author":
        books = books.order_by("author", "title")
    elif sort == "oldest":
        books = books.order_by("created_at")
    else:
        books = books.order_by("-created_at")
    return render(request, "library/list.html", {
        "books": books,
        "query": query,
        "sort": sort,
    })


def library_detail(request, book_id):
    book = get_object_or_404(Book, id=book_id)
    if not book.is_public and not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied
    return render(request, "library/detail.html", {
        "book": book,
        "file_url": book.file_url,
        "cover_url": book.cover_url,
        "download_url": book.download_link,
    })


def library_read(request, book_id):
    book = get_object_or_404(Book, id=book_id)
    if not book.is_public and not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied
    return render(request, "library/read.html", {
        "book": book,
        "file_url": book.file_url,
        "download_url": book.download_link,
    })


@write_required
def library_upload(request):
    if request.method == "POST":
        form = BookForm(request.POST, request.FILES)
        if form.is_valid():
            book = form.save(commit=False)
            book.uploaded_by = request.user
            uploaded = request.FILES.get("file")
            if uploaded:
                try:
                    uploaded.seek(0)
                    book.content = _extract_text(uploaded, uploaded.content_type or "")
                except Exception:
                    book.content = ""
                uploaded.seek(0)
                path = _object_path("books", uploaded.name)
                try:
                    upload_object(path, uploaded, content_type=uploaded.content_type or "application/pdf")
                except Exception as exc:
                    messages.error(request, _("Upload failed: %(err)s") % {"err": exc})
                    return render(request, "library/upload.html", {"form": form})
                book.file_path = path
                book.file_name = uploaded.name
                book.file_size = uploaded.size
                book.content_type = uploaded.content_type or ""
            cover = request.FILES.get("cover")
            if cover:
                cpath = _object_path("book-covers", cover.name)
                try:
                    upload_object(cpath, cover, content_type=cover.content_type or "image/jpeg")
                except Exception as exc:
                    messages.error(request, _("Cover upload failed: %(err)s") % {"err": exc})
                else:
                    book.cover_path = cpath
            book.save()
            messages.success(request, _("Book added to the library."))
            return redirect("library:detail", book_id=book.id)
    else:
        form = BookForm()
    return render(request, "library/upload.html", {"form": form})


@write_required
def library_edit(request, book_id):
    book = get_object_or_404(Book, id=book_id)
    if request.method == "POST":
        form = BookForm(request.POST, request.FILES, instance=book)
        if form.is_valid():
            book = form.save(commit=False)
            uploaded = request.FILES.get("file")
            if uploaded:
                if book.file_path:
                    try:
                        delete_object(book.file_path)
                    except Exception:
                        pass
                try:
                    uploaded.seek(0)
                    book.content = _extract_text(uploaded, uploaded.content_type or "")
                except Exception:
                    pass
                uploaded.seek(0)
                path = _object_path("books", uploaded.name)
                try:
                    upload_object(path, uploaded, content_type=uploaded.content_type or "application/pdf")
                except Exception as exc:
                    messages.error(request, _("Upload failed: %(err)s") % {"err": exc})
                    return render(request, "library/edit.html", {"form": form, "book": book})
                book.file_path = path
                book.file_name = uploaded.name
                book.file_size = uploaded.size
                book.content_type = uploaded.content_type or ""
            cover = request.FILES.get("cover")
            if cover:
                if book.cover_path:
                    try:
                        delete_object(book.cover_path)
                    except Exception:
                        pass
                cpath = _object_path("book-covers", cover.name)
                try:
                    upload_object(cpath, cover, content_type=cover.content_type or "image/jpeg")
                except Exception as exc:
                    messages.error(request, _("Cover upload failed: %(err)s") % {"err": exc})
                else:
                    book.cover_path = cpath
            book.save()
            messages.success(request, _("Book updated."))
            return redirect("library:detail", book_id=book.id)
    else:
        form = BookForm(instance=book)
    return render(request, "library/edit.html", {"form": form, "book": book})


@write_required
@require_POST
def library_delete(request, book_id):
    book = get_object_or_404(Book, id=book_id)
    if book.file_path:
        try:
            delete_object(book.file_path)
        except Exception:
            pass
    if book.cover_path:
        try:
            delete_object(book.cover_path)
        except Exception:
            pass
    book.delete()
    messages.success(request, _("Book removed from the library."))
    return redirect("library:list")


@require_POST
def library_translate(request):
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        data = request.POST
    text = (data.get("text") or "").strip()
    source = (data.get("source") or "en").strip()
    target = (data.get("target") or "fr").strip()
    if not text:
        return JsonResponse({"error": "No text provided."}, status=400)
    translated = _translate_text(text, source, target)
    return JsonResponse({"translated": translated})
