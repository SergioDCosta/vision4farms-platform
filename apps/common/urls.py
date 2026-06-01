from urllib.parse import urljoin

from django.conf import settings


def build_public_absolute_url(request, relative_path):
    path = str(relative_path or "")
    app_base_url = (getattr(settings, "APP_BASE_URL", "") or "").strip().rstrip("/")
    if app_base_url and not app_base_url.startswith(("http://", "https://")):
        app_base_url = f"https://{app_base_url.lstrip('/')}"
    if app_base_url:
        return urljoin(f"{app_base_url}/", path.lstrip("/"))
    return request.build_absolute_uri(path)

