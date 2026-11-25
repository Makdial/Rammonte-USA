from django.urls import path

from . import views

app_name = "library"

urlpatterns = [
    path("", views.library_list, name="list"),
    path("<int:book_id>/", views.library_detail, name="detail"),
    path("<int:book_id>/edit/", views.library_edit, name="edit"),
    path("<int:book_id>/read/", views.library_read, name="read"),
    path("<int:book_id>/pdf/", views.library_pdf, name="pdf"),
    path("upload/", views.library_upload, name="upload"),
    path("<int:book_id>/delete/", views.library_delete, name="delete"),
    path("translate/", views.library_translate, name="translate"),
]