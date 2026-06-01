from apps.accounts.models import UserRole
from apps.alerts.services import get_client_alerts_badge_state
from apps.common.context import cached, empty_badge


def client_alerts_sidebar_badge(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.CLIENTE:
        return {"client_alerts_badge": empty_badge()}
    return {
        "client_alerts_badge": cached(
            request,
            "client_alerts_badge",
            lambda: get_client_alerts_badge_state(request),
        )
    }

