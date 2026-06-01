from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.needs.constants import (
    EXTERNAL_DEMAND_SEARCH_QUERY_MAX_LENGTH,
    NEEDS_SEARCH_QUERY_MAX_LENGTH,
)


def quantize_need_quantity(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))


def normalize_needs_search_query(value):
    return (value or "").strip()[:NEEDS_SEARCH_QUERY_MAX_LENGTH]


def normalize_external_demands_search_query(value):
    return (value or "").strip()[:EXTERNAL_DEMAND_SEARCH_QUERY_MAX_LENGTH]


def clean_optional_text(value):
    value = (value or "").strip()
    return value or None


def is_uuid_like(value):
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def as_local_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.date()
    if isinstance(value, date):
        return value
    return None


def is_listing_effectively_expired(listing, *, now=None):
    expires_at = getattr(listing, "expires_at", None)
    if not expires_at or not isinstance(expires_at, datetime):
        return False
    now = now or timezone.now()
    return expires_at <= now


def normalize_needed_by_date(value):
    if not value:
        return None

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max)
    else:
        raw_value = str(value).strip()
        if not raw_value:
            return None
        try:
            if "T" in raw_value:
                parsed = datetime.strptime(raw_value, "%Y-%m-%dT%H:%M")
            else:
                parsed = datetime.combine(
                    datetime.strptime(raw_value, "%Y-%m-%d").date(),
                    time.max,
                )
        except ValueError:
            raise ValidationError("Data limite inválida para a necessidade.")

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def get_need_minimum_edit_quantity(coverage):
    return quantize_need_quantity(
        max(
            coverage.get("planned_qty") or Decimal("0.000"),
            coverage.get("completed_qty") or Decimal("0.000"),
        )
    )


def get_need_edit_help_text(need, coverage):
    minimum_quantity = get_need_minimum_edit_quantity(coverage)
    product = getattr(need, "product", None)
    unit = getattr(product, "unit", "kg")
    if minimum_quantity > 0:
        return (
            "Esta necessidade já tem encomendas associadas. "
            f"A quantidade mínima permitida é {minimum_quantity} {unit}."
        )
    return "Pode ajustar a quantidade, a data limite e as observações. O produto mantém-se fixo."


def producer_marketplace_display_name(producer):
    if not producer:
        return "Produtor"
    if getattr(producer, "display_name", None):
        return producer.display_name
    if getattr(producer, "company_name", None):
        return producer.company_name
    user = getattr(producer, "user", None)
    if user:
        full_name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
        if full_name:
            return full_name
        if getattr(user, "email", None):
            return user.email
    return "Produtor"
