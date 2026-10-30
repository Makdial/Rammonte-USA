from django.urls import path
from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy
from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.RegisterView.as_view(), name="register"),
    path("login/", auth_views.LoginView.as_view(
        template_name="accounts/login.html",
        redirect_authenticated_user=True,
    ), name="login"),
    path("logout/", auth_views.LogoutView.as_view(
        template_name="accounts/logout.html",
    ), name="logout"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="accounts/password_reset_form.html",
            email_template_name="accounts/password_reset_email.txt",
            subject_template_name="accounts/password_reset_subject.txt",
            # Always land on the same page whether or not the address exists,
            # so the form cannot be used to discover who has an account.
            success_url=reverse_lazy("accounts:password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="accounts/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    # One pattern covers both steps in Django 6: the emailed token is validated,
    # parked in the session, and the URL is rewritten so the token no longer
    # sits in the address bar (and cannot leak through a Referer header). The
    # rewritten path simply repeats this route with <token> = "set-password".
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            success_url=reverse_lazy("accounts:password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="accounts/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    path("manage/users/", views.manage_users, name="manage_users"),
    path("manage/users/<int:user_id>/block/", views.user_set_blocked, name="user_block"),
    path("manage/users/<int:user_id>/access/", views.user_set_access, name="user_access"),
    path("manage/users/<int:user_id>/delete/", views.user_delete, name="user_delete"),
]
