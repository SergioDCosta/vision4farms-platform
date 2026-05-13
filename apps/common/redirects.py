from django.utils.http import url_has_allowed_host_and_scheme


def get_safe_next_url(request, value):
    next_url = str(value or "").strip()
    if not next_url:
        return ""

    allowed_hosts = {request.get_host()} if request else set()
    if url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts=allowed_hosts,
        require_https=request.is_secure() if request else False,
    ):
        return next_url

    return ""
