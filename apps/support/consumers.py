import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.accounts.models import User, UserRole


SUPPORT_ADMIN_BADGE_GROUP = "support_admin_badge"


class SupportSidebarConsumer(AsyncWebsocketConsumer):
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

    async def connect(self):
        self.current_user = await self._resolve_current_user()
        if not self.current_user:
            await self.close(code=4401)
            return

        if getattr(self.current_user, "role", None) != UserRole.ADMIN:
            await self.close(code=4403)
            return

        await self.channel_layer.group_add(SUPPORT_ADMIN_BADGE_GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "current_user") and self.current_user:
            await self.channel_layer.group_discard(SUPPORT_ADMIN_BADGE_GROUP, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        return

    async def support_badge_changed(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "support.badge.changed",
                }
            )
        )
