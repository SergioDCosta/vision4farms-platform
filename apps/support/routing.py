from django.urls import path

from apps.support.consumers import SupportSidebarConsumer, SupportTicketConsumer


websocket_urlpatterns = [
    path("ws/suporte/sidebar/", SupportSidebarConsumer.as_asgi()),
    path("ws/suporte/ticket/<uuid:ticket_id>/", SupportTicketConsumer.as_asgi()),
]
