from apps.accounts.models import UserRole
from apps.common.context import cached, empty_badge
from apps.support.services import get_admin_support_badge_state


def admin_support_sidebar_badge(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.ADMIN:
        return {"admin_support_badge": empty_badge()}
    return {
        "admin_support_badge": cached(
            request,
            "admin_support_badge",
            lambda: get_admin_support_badge_state(request),
        )
    }

