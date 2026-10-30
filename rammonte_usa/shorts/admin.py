from django.contrib import admin

from .models import Short


@admin.register(Short)
class ShortAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "views", "likes", "created_at")
    search_fields = ("title", "user__username")