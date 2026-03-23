from django.urls import path
from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("", views.login_view, name="login"),
    path("login/", views.login_view, name="login"),
    path("registo/", views.register_view, name="register"),
    path("registo/sucesso/", views.register_success_view, name="register_success"),
    path("verificar-email/<str:token>/", views.verify_email_view, name="verify_email"),
    path("logout/", views.logout_view, name="logout"),
    path("recuperar-password/", views.password_reset_request_view, name="password_reset_request"),
    path("recuperar-password/<str:token>/", views.password_reset_confirm_view, name="password_reset_confirm"),
]