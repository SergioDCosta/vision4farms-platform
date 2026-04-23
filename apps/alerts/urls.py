from django.urls import path

from apps.alerts import views

app_name = "alerts"

urlpatterns = [
    path("alertas/", views.alerts_index_view, name="index"),
    path("alertas/sidebar-state/", views.alerts_sidebar_state_view, name="sidebar_state"),
    path("alertas/ignorar-todos/", views.alert_ignore_all_view, name="ignore_all"),
    path("alertas/<uuid:alert_id>/ignorar/", views.alert_ignore_view, name="ignore"),
    path("alertas/<uuid:alert_id>/reativar/", views.alert_reactivate_view, name="reactivate"),
    path("alertas/<uuid:alert_id>/resolver/", views.alert_resolve_view, name="resolve"),
]
