from django.urls import path

from apps.settings_app.views import settings_view

app_name = "settings_app"

urlpatterns = [
    path("definicoes/", settings_view, name="settings_index"),
]
