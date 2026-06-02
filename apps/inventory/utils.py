from datetime import datetime
from decimal import Decimal

from django.utils import timezone

from apps.inventory.constants import ZERO


def month_floor(dt):
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def shift_month(dt, delta_months):
    month_index = dt.month - 1 + delta_months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    return dt.replace(year=year, month=month, day=1)


def aware_datetime(year, month, day):
    return timezone.make_aware(
        datetime(year, month, day),
        timezone.get_current_timezone(),
    )


def safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_decimal(value):
    return value if value is not None else ZERO


def format_qty(value):
    decimal_value = Decimal(str(value or 0)).quantize(Decimal("0.001"))
    formatted = format(decimal_value, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


def progress_percent(value, target):
    value = Decimal(str(value or 0))
    target = Decimal(str(target or 0))
    if target <= ZERO:
        return 0
    percent = (value / target) * Decimal("100")
    percent = max(Decimal("0"), min(percent, Decimal("100")))
    return int(percent.quantize(Decimal("1")))


def quantize_stock_quantity(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))
