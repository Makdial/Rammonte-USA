from django.urls import path
from . import views

app_name = "videos"

urlpatterns = [
    path("", views.video_list, name="list"),
    path("search/", views.video_search, name="search"),
    path("offline/", views.offline_library, name="offline"),
    path("upload/", views.video_upload_page, name="upload"),
    path("upload/submit/", views.video_upload, name="upload_submit"),
    path("playlists/", views.playlist_list, name="playlists"),
    path("playlists/new/", views.playlist_create, name="playlist_create"),
    path("playlists/<int:playlist_id>/", views.playlist_detail, name="playlist_detail"),
    path("playlists/<int:playlist_id>/delete/", views.playlist_delete, name="playlist_delete"),
    path("playlists/<int:playlist_id>/update/", views.playlist_update, name="playlist_update"),
    path("playlists/<int:playlist_id>/toggle/", views.playlist_toggle_video, name="playlist_toggle"),
    path(
        "playlists/<int:playlist_id>/remove/<int:video_id>/",
        views.playlist_remove_video,
        name="playlist_remove",
    ),
    path("comments/<int:comment_id>/delete/", views.comment_delete, name="comment_delete"),
    path("<int:video_id>", views.video_detail, name="detail"),
    path("<int:video_id>/audio-stream/", views.audio_stream, name="audio_stream"),
    path("<int:video_id>/audio-status/", views.audio_status, name="audio_status"),
    path("<int:video_id>/audio-generate/", views.audio_generate, name="audio_generate"),
    path("channel/<str:username>/", views.channel_videos, name="channel"),
    path("<int:video_id>/delete/", views.delete_video, name="delete"),
    path("<int:video_id>/edit/", views.video_edit, name="edit"),
    path("<int:video_id>/vote/", views.video_vote, name="vote"),
    path("<int:video_id>/comments/add/", views.comment_add, name="comment_add"),
    path(
        "<int:video_id>/subtitles/<str:language_code>.vtt",
        views.subtitle_file,
        name="subtitle_file",
    ),
    path(
        "<int:video_id>/subtitles/upload/",
        views.subtitle_upload,
        name="subtitle_upload",
    ),
    path(
        "<int:video_id>/subtitles/<str:language_code>/delete/",
        views.subtitle_delete,
        name="subtitle_delete",
    ),
    path(
        "<int:video_id>/subtitles/generate/",
        views.subtitles_generate,
        name="subtitles_generate",
    ),
    path(
        "<int:video_id>/subtitles/status/",
        views.subtitles_status,
        name="subtitles_status",
    ),
]
