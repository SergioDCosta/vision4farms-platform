import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.accounts.models import User
from apps.messaging.models import (
    Conversation,
    ConversationParticipant,
)
from apps.messaging.services import (
    broadcast_unread_totals_for_user_ids,
    create_text_message,
    get_unread_totals_for_conversation_participants,
    get_unread_totals_for_user,
    mark_conversation_as_read,
    serialize_message_payload,
)


class _BaseMessagingConsumer(AsyncWebsocketConsumer):
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

    async def _send_json(self, payload):
        await self.send(text_data=json.dumps(payload))


class ConversationConsumer(_BaseMessagingConsumer):
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
        await self._broadcast_unread_totals()

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
            await self._broadcast_current_user_unread_totals()

    @database_sync_to_async
    def _is_conversation_participant(self):
        return ConversationParticipant.objects.filter(
            conversation_id=self.conversation_id,
            conversation__is_active=True,
            user_id=self.current_user.id,
        ).exists()

    @database_sync_to_async
    def _mark_conversation_as_read(self):
        conversation = (
            Conversation.objects
            .filter(id=self.conversation_id, is_active=True)
            .first()
        )
        if not conversation:
            return False
        return bool(mark_conversation_as_read(user=self.current_user, conversation=conversation))

    @database_sync_to_async
    def _broadcast_current_user_unread_totals(self):
        return bool(broadcast_unread_totals_for_user_ids([self.current_user.id]))

    @database_sync_to_async
    def _create_text_message(self, content):
        conversation = Conversation.objects.get(id=self.conversation_id, is_active=True)

        message = create_text_message(
            conversation=conversation,
            sender_user=self.current_user,
            content=content,
        )
        return serialize_message_payload(message=message)

    @database_sync_to_async
    def _get_unread_targets(self):
        conversation = (
            Conversation.objects
            .prefetch_related("participants__user")
            .filter(id=self.conversation_id, is_active=True)
            .first()
        )
        if not conversation:
            return []
        return get_unread_totals_for_conversation_participants(conversation=conversation)

    async def _broadcast_unread_totals(self):
        unread_targets = await self._get_unread_targets()
        for target in unread_targets:
            await self.channel_layer.group_send(
                f"messaging_user_{target['user_id']}",
                {
                    "type": "unread_totals",
                    "active_unread_total": target["active_unread_total"],
                    "archived_unread_total": target["archived_unread_total"],
                },
            )


class UnreadCounterConsumer(_BaseMessagingConsumer):
    async def connect(self):
        self.current_user = await self._resolve_current_user()
        if not self.current_user:
            await self.close(code=4401)
            return

        self.group_name = f"messaging_user_{self.current_user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self._send_initial_unread_totals()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        return

    async def unread_totals(self, event):
        await self._send_json(
            {
                "type": "unread.totals",
                "active_unread_total": int(event.get("active_unread_total") or 0),
                "archived_unread_total": int(event.get("archived_unread_total") or 0),
            }
        )

    @database_sync_to_async
    def _get_initial_totals(self):
        return get_unread_totals_for_user(self.current_user)

    async def _send_initial_unread_totals(self):
        totals = await self._get_initial_totals()
        await self._send_json(
            {
                "type": "unread.totals",
                "active_unread_total": int(totals.get("active_unread_total") or 0),
                "archived_unread_total": int(totals.get("archived_unread_total") or 0),
            }
        )
