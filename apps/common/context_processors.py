from django.conf import settings

from apps.accounts.models import UserRole
from apps.alerts.services import get_client_alerts_badge_state
from apps.common.media import resolve_media_url
from apps.messaging.services import get_client_messages_badge_state
from apps.settings_app.models import UserPreference
from apps.support.services import get_admin_support_badge_state


EMPTY_BADGE = {"visible": False, "count": 0, "tone": "orange"}


def _empty_badge():
    return dict(EMPTY_BADGE)


def _request_cache(request):
    cache = getattr(request, "_common_context_cache", None)
    if cache is None:
        cache = {}
        setattr(request, "_common_context_cache", cache)
    return cache


def _cached(request, key, factory):
    cache = _request_cache(request)
    if key not in cache:
        cache[key] = factory()
    return cache[key]


def brand_assets(request):
    return {
        "brand_logo_color_url": getattr(settings, "BRAND_LOGO_COLOR_URL", ""),
        "brand_logo_white_url": getattr(settings, "BRAND_LOGO_WHITE_URL", ""),
        "brand_login_logo_white_url": getattr(settings, "BRAND_LOGIN_LOGO_WHITE_URL", ""),
        "brand_sidebar_compact_logo_url": getattr(settings, "BRAND_SIDEBAR_COMPACT_LOGO_URL", ""),
        "brand_favicon_url": getattr(settings, "BRAND_FAVICON_URL", ""),
    }


def _avatar_initials(user):
    first_name = (getattr(user, "first_name", "") or "").strip()
    last_name = (getattr(user, "last_name", "") or "").strip()
    if first_name and last_name:
        return f"{first_name[0]}{last_name[0]}".upper()

    full_name = (getattr(user, "full_name", "") or "").strip()
    parts = [part for part in full_name.split() if part]
    if len(parts) >= 2:
        return f"{parts[0][0]}{parts[-1][0]}".upper()
    if len(parts) == 1:
        return parts[0][:2].upper()

    email = (getattr(user, "email", "") or "").strip()
    return (email[:2] or "U").upper()


def topbar_user_profile(request):
    user = getattr(request, "current_user", None)
    if not user:
        return {
            "topbar_profile_photo_url": None,
            "topbar_avatar_initials": "U",
        }

    def _get_profile_photo_url():
        preference = (
            UserPreference.objects
            .filter(user=user)
            .only("profile_photo")
            .first()
        )

        if not preference:
            return None

        return resolve_media_url(preference.profile_photo)

    return {
        "topbar_profile_photo_url": _cached(
            request,
            "topbar_profile_photo_url",
            _get_profile_photo_url,
        ),
        "topbar_avatar_initials": _avatar_initials(user),
    }


def admin_support_sidebar_badge(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.ADMIN:
        return {"admin_support_badge": _empty_badge()}
    return {
        "admin_support_badge": _cached(
            request,
            "admin_support_badge",
            lambda: get_admin_support_badge_state(request),
        )
    }


def client_alerts_sidebar_badge(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.CLIENTE:
        return {"client_alerts_badge": _empty_badge()}
    return {
        "client_alerts_badge": _cached(
            request,
            "client_alerts_badge",
            lambda: get_client_alerts_badge_state(request),
        )
    }


def client_messages_sidebar_badge(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.CLIENTE:
        return {"client_messages_badge": _empty_badge()}
    return {
        "client_messages_badge": _cached(
            request,
            "client_messages_badge",
            lambda: get_client_messages_badge_state(user),
        )
    }
