from django.urls import path

from apps.alerts import views

app_name = "alerts"

urlpatterns = [
    path("alertas/", views.alerts_index_view, name="index"),
    path("alertas/<uuid:alert_id>/ignorar/", views.alert_ignore_view, name="ignore"),
    path("alertas/<uuid:alert_id>/resolver/", views.alert_resolve_view, name="resolve"),
]
