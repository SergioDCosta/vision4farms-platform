import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.accounts.models import UserRole
from apps.alerts.services import get_alerts_badge_group_name
from apps.common.session import resolve_active_session_user


class AlertsSidebarConsumer(AsyncWebsocketConsumer):
    @database_sync_to_async
    def _resolve_current_user(self):
        return resolve_active_session_user(self.scope.get("session"))

    async def connect(self):
        self.current_user = await self._resolve_current_user()
        if not self.current_user:
            await self.close(code=4401)
            return

        if getattr(self.current_user, "role", None) != UserRole.CLIENTE:
            await self.close(code=4403)
            return

        self.group_name = get_alerts_badge_group_name(self.current_user.id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name") and self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        return

    async def alerts_badge_changed(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "alerts.badge.changed",
                }
            )
        )
