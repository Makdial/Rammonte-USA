from django.contrib.auth.models import User
from django.db import models
from django.utils.translation import gettext_lazy as _

from . import r2_storage


class Short(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="shorts")
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)

    file_id = models.CharField(max_length=200, blank=True, default="")
    video_url = models.URLField(max_length=500, blank=True, default="")
    thumbnail_file_id = models.CharField(max_length=200, blank=True, default="")
    thumbnail_url = models.URLField(max_length=500, blank=True)

    views = models.PositiveIntegerField(default=0)
    likes = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def playback_url(self):
        return r2_storage.resolve_url(self.file_id, self.video_url)

    @property
    def display_thumbnail_url(self):
        return r2_storage.resolve_url(self.thumbnail_file_id, self.thumbnail_url)

    def delete_stored_files(self):
        for path in (self.file_id, self.thumbnail_file_id):
            if path:
                try:
                    r2_storage.delete_file(path)
                except r2_storage.StorageError:
                    pass