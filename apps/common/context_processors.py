from django.conf import settings
from django.core.files.storage import default_storage

from apps.accounts.models import UserRole
from apps.settings_app.models import UserPreference
from apps.support.services import get_admin_support_badge_state


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

    preference = (
        UserPreference.objects
        .filter(user=user)
        .only("profile_photo")
        .first()
    )

    if not preference:
        return {"topbar_profile_photo_url": None}

    return {
        "topbar_profile_photo_url": _resolve_media_url(preference.profile_photo),
    }


def admin_support_sidebar_badge(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.ADMIN:
        return {"admin_support_badge": {"visible": False, "count": 0, "tone": "orange"}}
    return {"admin_support_badge": get_admin_support_badge_state(request)}
