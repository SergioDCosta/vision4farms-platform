from django.utils import timezone
from django.utils.dateparse import parse_datetime


def parse_session_datetime(value):
    raw = (value or "").strip()
    if not raw:
        return None

    parsed = parse_datetime(raw)
    if not parsed:
        return None

    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed

