from django.urls import path
from apps.messaging.consumers import ConversationConsumer

websocket_urlpatterns = [
    path("ws/mensagens/<uuid:conversation_id>/", ConversationConsumer.as_asgi()),
]