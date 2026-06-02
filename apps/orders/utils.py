"""Order domain services: utils."""

from apps.common.formatting import format_quantity
from decimal import Decimal, ROUND_HALF_UP
from apps.orders.constants import MONEY_DECIMAL, QTY_DECIMAL


def quantize_qty(value):
    return Decimal(str(value)).quantize(QTY_DECIMAL)


def quantize_money(value):
    return Decimal(str(value)).quantize(MONEY_DECIMAL, rounding=ROUND_HALF_UP)


def _audit_qty(value):
    return str(quantize_qty(value or 0))


def _order_audit_values(order):
    return {
        "order_id": str(order.id),
        "buyer_producer_id": str(order.buyer_producer_id),
        "status": order.status,
        "total_amount": str(quantize_money(order.total_amount or 0)),
        "source_type": order.source_type,
        "order_number": order.order_number,
    }


def _quantity_label(value, unit=""):
    unit_label = (unit or "").strip()
    quantity = format_quantity(value)
    return f"{quantity} {unit_label}".strip()


def _producer_display_name(producer):
    if not producer:
        return "Vendedor"
    display_name = (getattr(producer, "display_name", "") or "").strip()
    if display_name:
        return display_name
    company_name = (getattr(producer, "company_name", "") or "").strip()
    if company_name:
        return company_name
    user = getattr(producer, "user", None)
    if user:
        full_name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
        if full_name:
            return full_name
        email = (getattr(user, "email", "") or "").strip()
        if email:
            return email
    return "Vendedor"
