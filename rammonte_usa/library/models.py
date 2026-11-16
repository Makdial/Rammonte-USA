from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Book(models.Model):
    title = models.CharField(_("title"), max_length=200)
    author = models.CharField(_("author"), max_length=200, blank=True)
    description = models.TextField(_("description"), blank=True)

    file_path = models.CharField(_("file path"), max_length=500, blank=True)
    file_name = models.CharField(_("file name"), max_length=255, blank=True)
    file_size = models.PositiveBigIntegerField(_("file size"), default=0)
    content_type = models.CharField(_("content type"), max_length=100, blank=True)
    content = models.TextField(_("extracted text"), blank=True)

    cover_path = models.CharField(_("cover path"), max_length=500, blank=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="books",
    )
    is_public = models.BooleanField(_("public"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("book")
        verbose_name_plural = _("books")

    def __str__(self):
        return self.title

    @property
    def cover_url(self):
        if not self.cover_path:
            return ""
        from videos.r2_storage import resolve_url
        return resolve_url(self.cover_path)

    @property
    def file_url(self):
        if not self.file_path:
            return ""
        from videos.r2_storage import resolve_url
        return resolve_url(self.file_path)

    @property
    def download_link(self):
        if not self.file_path:
            return ""
        from videos.r2_storage import download_url
        return download_url(self.file_path, file_name=self.file_name)