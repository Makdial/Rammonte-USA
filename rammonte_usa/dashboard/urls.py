from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("videos/", views.video_manage, name="videos"),
    path("videos/order/", views.video_order, name="video_order"),
    path("videos/order/save/", views.video_order_save, name="video_order_save"),
    path("videos/audio/generate/", views.audio_generate, name="audio_generate"),
    path("videos/audio/status/", views.audio_status, name="audio_status"),
    path("videos/subtitles/generate/", views.subtitle_generate, name="subtitle_generate"),
    path("videos/subtitles/status/", views.subtitle_status, name="subtitle_status"),
    path("comments/", views.comment_manage, name="comments"),
path("usage/", views.usage, name="usage"),
    path("storage/cleanup/", views.cleanup, name="cleanup"),
    path("storage/settings/", views.settings_update, name="settings"),
    path("storage/test-email/", views.test_email, name="test_email"),
]
