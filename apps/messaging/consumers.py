import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.common.session import resolve_active_session_user
from apps.messaging.models import (
    Conversation,
    ConversationParticipant,
)
from apps.messaging.services import (
    create_text_message,
    get_unread_totals_for_user,
    mark_conversation_as_read,
    serialize_message_payload,
)


class _BaseMessagingConsumer(AsyncWebsocketConsumer):
    @database_sync_to_async
    def _resolve_current_user(self):
        return resolve_active_session_user(self.scope.get("session"))

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
        was_unread = await self._mark_conversation_as_read()
        if was_unread:
            await self._broadcast_read_update()

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

        msg_type = payload.get("type")

        if msg_type == "message.send":
            content = str(payload.get("content") or "").strip()
            if not content:
                return
            try:
                message_result = await self._create_text_message(content)
            except Exception:
                await self._send_json({
                    "type": "message.error",
                    "error": "Não foi possível enviar a mensagem.",
                })
                return
            if not message_result.get("realtime_dispatched"):
                await self._send_json({
                    "type": "message.created",
                    "message": message_result.get("message", {}),
                })

        elif msg_type == "typing.start":
            await self._broadcast_typing(True)

        elif msg_type == "typing.stop":
            await self._broadcast_typing(False)

        elif msg_type == "conversation.sync":
            last_message_id = str(payload.get("last_message_id") or "").strip()
            if last_message_id:
                missed = await self._get_missed_messages(last_message_id)
                if missed:
                    await self._send_json({
                        "type": "messages.catchup",
                        "messages": missed,
                    })

    async def message_created(self, event):
        message_payload = event.get("message", {})
        await self._send_json({
            "type": "message.created",
            "message": message_payload,
        })

        if str(message_payload.get("sender_id")) != str(self.current_user.id):
            was_unread = await self._mark_conversation_as_read()
            await self._broadcast_current_user_unread_totals()
            if was_unread:
                await self._broadcast_read_update()

    async def typing_update(self, event):
        if str(event.get("user_id")) == str(self.current_user.id):
            return
        await self._send_json({
            "type": "typing.update",
            "user_id": event["user_id"],
            "user_name": event["user_name"],
            "is_typing": event["is_typing"],
        })

    async def read_update(self, event):
        if str(event.get("reader_id")) == str(self.current_user.id):
            return
        await self._send_json({
            "type": "read.update",
            "reader_id": event["reader_id"],
        })

    async def _broadcast_typing(self, is_typing):
        user_name = (
            getattr(self.current_user, "full_name", None)
            or getattr(self.current_user, "email", "")
            or "Utilizador"
        )
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "typing_update",
                "user_id": str(self.current_user.id),
                "user_name": str(user_name),
                "is_typing": is_typing,
            },
        )

    async def _broadcast_read_update(self):
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "read_update",
                "reader_id": str(self.current_user.id),
            },
        )

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
        from apps.messaging.services import broadcast_unread_totals_for_user_ids
        return bool(broadcast_unread_totals_for_user_ids([self.current_user.id]))

    @database_sync_to_async
    def _create_text_message(self, content):
        conversation = Conversation.objects.get(id=self.conversation_id, is_active=True)
        message = create_text_message(
            conversation=conversation,
            sender_user=self.current_user,
            content=content,
            broadcast_realtime=True,
        )
        return {
            "message": serialize_message_payload(message=message),
            "realtime_dispatched": bool(getattr(message, "realtime_dispatched", False)),
        }

    @database_sync_to_async
    def _get_missed_messages(self, last_message_id):
        from apps.messaging.models import Message
        try:
            last_msg = Message.objects.filter(
                conversation_id=self.conversation_id,
                id=last_message_id,
            ).first()
            if not last_msg:
                return []
            missed = (
                Message.objects
                .filter(
                    conversation_id=self.conversation_id,
                    created_at__gt=last_msg.created_at,
                )
                .select_related("sender_user")
                .order_by("created_at")[:50]
            )
            return [serialize_message_payload(message=m) for m in missed]
        except Exception:
            return []


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
