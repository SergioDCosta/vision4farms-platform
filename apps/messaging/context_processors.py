from apps.accounts.models import UserRole
from apps.common.context import cached, empty_badge
from apps.messaging.services import get_client_messages_badge_state


def client_messages_sidebar_badge(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.CLIENTE:
        return {"client_messages_badge": empty_badge()}
    return {
        "client_messages_badge": cached(
            request,
            "client_messages_badge",
            lambda: get_client_messages_badge_state(user),
        )
    }

