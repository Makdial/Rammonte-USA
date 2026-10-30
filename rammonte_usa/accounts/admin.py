from django.contrib import admin

from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "access_level", "is_guest", "created_at")
    list_filter = ("access_level", "is_guest")
    search_fields = ("user__username", "user__email", "user__first_name")
