from django.conf import settings

from apps.common.context import cached
from apps.common.media import resolve_media_url
from apps.settings_app.models import UserPreference


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
        "topbar_profile_photo_url": cached(
            request,
            "topbar_profile_photo_url",
            _get_profile_photo_url,
        ),
        "topbar_avatar_initials": _avatar_initials(user),
    }
