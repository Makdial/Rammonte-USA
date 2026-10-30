from django.urls import path

from . import views

app_name = "shorts"

urlpatterns = [
    path("", views.short_list, name="list"),
    path("<int:short_id>/", views.short_detail, name="detail"),
    path("upload/", views.short_upload_page, name="upload"),
    path("upload/submit/", views.short_upload, name="upload_submit"),
    path("<int:short_id>/delete/", views.short_delete, name="delete"),
]