import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from apps.accounts.models import User
from apps.messaging.models import (
    Conversation,
    ConversationParticipant,
)
from apps.messaging.services import create_text_message, serialize_message_payload


class ConversationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.conversation_id = str(self.scope["url_route"]["kwargs"]["conversation_id"])
        self.group_name = f"conversation_{self.conversation_id}"
        self.current_user = await self._resolve_current_user()

        if not self.current_user:
            await self.close(code=4401)
            return

        is_participant = await self._is_conversation_participant()
        if not is_participant:
            await self.close(code=4403)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self._mark_conversation_as_read()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return

        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            return

        if payload.get("type") != "message.send":
            return

        content = str(payload.get("content") or "").strip()
        if not content:
            return

        try:
            message_payload = await self._create_text_message(content)
        except Exception:
            await self._send_json(
                {
                    "type": "message.error",
                    "error": "Não foi possível enviar a mensagem.",
                }
            )
            return

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "message_created",
                "message": message_payload,
            },
        )

    async def message_created(self, event):
        message_payload = event.get("message", {})
        await self._send_json(
            {
                "type": "message.created",
                "message": message_payload,
            }
        )

        if str(message_payload.get("sender_id")) != str(self.current_user.id):
            await self._mark_conversation_as_read()

    async def _send_json(self, payload):
        await self.send(text_data=json.dumps(payload))

    @database_sync_to_async
    def _resolve_current_user(self):
        scope_user = self.scope.get("user")
        if scope_user is not None and getattr(scope_user, "is_authenticated", False):
            return scope_user

        session = self.scope.get("session")
        if not session:
            return None

        user_id = session.get("user_id")
        if not user_id:
            return None

        return User.objects.filter(id=user_id).first()

    @database_sync_to_async
    def _is_conversation_participant(self):
        return ConversationParticipant.objects.filter(
            conversation_id=self.conversation_id,
            conversation__is_active=True,
            user_id=self.current_user.id,
            is_archived=False,
        ).exists()

    @database_sync_to_async
    def _mark_conversation_as_read(self):
        ConversationParticipant.objects.filter(
            conversation_id=self.conversation_id,
            user_id=self.current_user.id,
            is_archived=False,
        ).update(last_read_at=timezone.now())

    @database_sync_to_async
    def _create_text_message(self, content):
        conversation = Conversation.objects.get(id=self.conversation_id, is_active=True)

        message = create_text_message(
            conversation=conversation,
            sender_user=self.current_user,
            content=content,
        )
        return serialize_message_payload(message=message)
