from django.urls import path
from apps.messaging.consumers import ConversationConsumer, UnreadCounterConsumer

websocket_urlpatterns = [
    path("ws/mensagens/<uuid:conversation_id>/", ConversationConsumer.as_asgi()),
    path("ws/mensagens/unread/", UnreadCounterConsumer.as_asgi()),
]
