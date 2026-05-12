from django.conf import settings
from django.core.files.storage import default_storage

from apps.accounts.models import UserRole
from apps.alerts.services import get_client_alerts_badge_state
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


def _resolve_media_url(photo_path):
    if not photo_path:
        return None

    raw_path = str(photo_path).strip()
    if not raw_path:
        return None

    if raw_path.startswith(("http://", "https://")):
        return raw_path

    if raw_path.startswith(settings.MEDIA_URL):
        raw_path = raw_path[len(settings.MEDIA_URL):]

    normalized_path = raw_path.lstrip("/").strip()
    if not normalized_path:
        return None

    try:
        return default_storage.url(normalized_path)
    except Exception:
        return f"{settings.MEDIA_URL}{normalized_path}"


def topbar_user_profile(request):
    user = getattr(request, "current_user", None)
    if not user:
        return {"topbar_profile_photo_url": None}

    def _get_profile_photo_url():
        preference = (
            UserPreference.objects
            .filter(user=user)
            .only("profile_photo")
            .first()
        )

        if not preference:
            return None

        return _resolve_media_url(preference.profile_photo)

    return {
        "topbar_profile_photo_url": _cached(
            request,
            "topbar_profile_photo_url",
            _get_profile_photo_url,
        )
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
