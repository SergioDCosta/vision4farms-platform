from django.conf import settings
from django.core.files.storage import default_storage


def resolve_media_url(path):
    if not path:
        return None

    raw_path = str(path).strip()
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
