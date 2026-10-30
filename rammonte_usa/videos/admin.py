from django.contrib import admin

from .models import Comment, Playlist, PlaylistItem, SubtitleTrack, Video, VideoLike


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "user",
        "audio_status",
        "views",
        "likes",
        "dislikes",
        "created_at",
    )
    list_filter = ("created_at", "user", "audio_status")
    search_fields = ("title", "description", "user__username")
    readonly_fields = ("views", "likes", "dislikes", "created_at", "updated_at")
    date_hierarchy = "created_at"

    @admin.display(description="MP3")
    def audio_status(self, obj):
        return obj.get_audio_status_display() if obj.audio_status else "-"


@admin.register(VideoLike)
class VideoLikeAdmin(admin.ModelAdmin):
    list_display = ("user", "video", "value", "created_at")
    list_filter = ("value", "created_at")
    search_fields = ("user__username", "video__title")


@admin.register(SubtitleTrack)
class SubtitleTrackAdmin(admin.ModelAdmin):
    list_display = ("video", "language_code", "label", "is_lyrics", "updated_at")
    list_filter = ("language_code", "is_lyrics")
    search_fields = ("video__title", "label")


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("user", "video", "short_body", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username", "video__title", "body")
    date_hierarchy = "created_at"

    @admin.display(description="Comment")
    def short_body(self, obj):
        return obj.body[:60]


class PlaylistItemInline(admin.TabularInline):
    model = PlaylistItem
    extra = 0
    autocomplete_fields = ("video",)


@admin.register(Playlist)
class PlaylistAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "is_public", "video_count", "updated_at")
    list_filter = ("is_public", "created_at")
    search_fields = ("title", "user__username")
    inlines = (PlaylistItemInline,)

    @admin.display(description="Videos")
    def video_count(self, obj):
        return obj.items.count()


@admin.register(PlaylistItem)
class PlaylistItemAdmin(admin.ModelAdmin):
    list_display = ("playlist", "video", "position", "added_at")
    search_fields = ("playlist__title", "video__title")
