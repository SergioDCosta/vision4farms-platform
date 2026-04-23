from django.urls import path

from apps.alerts.consumers import AlertsSidebarConsumer


websocket_urlpatterns = [
    path("ws/alertas/sidebar/", AlertsSidebarConsumer.as_asgi()),
]
