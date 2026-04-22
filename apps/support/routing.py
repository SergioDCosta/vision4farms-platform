from django.urls import path

from apps.support.consumers import SupportSidebarConsumer


websocket_urlpatterns = [
    path("ws/suporte/sidebar/", SupportSidebarConsumer.as_asgi()),
]
