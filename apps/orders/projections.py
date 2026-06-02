"""Order domain services: projections."""

from apps.orders.models import OrderItem, OrderItemStatus, OrderStatus
from decimal import Decimal
from django.utils import timezone
from apps.orders.constants import INCOMING_FORECAST_ORDER_STATUSES, PRESALE_TIMELINE_STEPS
from apps.orders.utils import _producer_display_name, quantize_money, quantize_qty


def _coerce_history_events(order):
    status_history = getattr(order, "status_history", None)
    if status_history is None:
        return []

    if hasattr(status_history, "all"):
        return list(status_history.all())

    try:
        return list(status_history)
    except TypeError:
        return []


def build_presale_timeline_context(order):
    current_status = str(getattr(order, "status", "") or "")
    history_events = _coerce_history_events(order)
    reached_from_history = {str(getattr(event, "status", "") or "") for event in history_events}
    reached_from_history.add(current_status)

    current_step_for_status = {
        OrderStatus.PENDING: "created",
        OrderStatus.CONFIRMED: "confirmed",
        OrderStatus.IN_PROGRESS: "in_progress",
        OrderStatus.DELIVERING: "delivered",
    }
    is_cancelled = current_status == OrderStatus.CANCELLED
    is_completed = current_status == OrderStatus.COMPLETED

    timeline_steps = []
    for key, label, reached_by_statuses in PRESALE_TIMELINE_STEPS:
        reached = bool(reached_from_history.intersection(reached_by_statuses))
        if reached:
            if not is_cancelled and not is_completed and current_step_for_status.get(current_status) == key:
                state = "current"
            else:
                state = "done"
        else:
            state = "interrupted" if is_cancelled else "pending"

        timeline_steps.append(
            {
                "key": key,
                "label": label,
                "state": state,
            }
        )

    return {
        "steps": timeline_steps,
        "state": "interrupted" if is_cancelled else "normal",
        "cancelled": is_cancelled,
    }


def get_buyer_incoming_forecast_projection(*, buyer_producer):
    """
    Calcula stock previsto do comprador sem persistência:
    - apenas encomendas já comprometidas (CONFIRMED/IN_PROGRESS/DELIVERING)
    - apenas itens ainda ativos (exclui COMPLETED/CANCELLED)
    - apenas itens com prova de origem forecast (listing + forecast_id)
    """
    incoming_items = (
        OrderItem.objects
        .filter(
            order__buyer_producer=buyer_producer,
            order__status__in=INCOMING_FORECAST_ORDER_STATUSES,
            listing__isnull=False,
            listing__forecast_id__isnull=False,
        )
        .exclude(item_status__in=[OrderItemStatus.COMPLETED, OrderItemStatus.CANCELLED])
        .select_related(
            "order",
            "listing",
            "listing__forecast",
            "seller_producer",
            "seller_producer__user",
            "product",
        )
        .order_by("-order__created_at", "-created_at")
    )

    total_incoming = Decimal("0.000")
    by_product = {}
    for item in incoming_items:
        product_id = str(item.product_id)
        listing = item.listing
        forecast = getattr(listing, "forecast", None) if listing else None
        order = item.order

        bucket = by_product.get(product_id)
        if not bucket:
            bucket = {
                "product_id": product_id,
                "product_name": getattr(item.product, "name", None) or "Produto",
                "product_unit": getattr(item.product, "unit", None) or "",
                "incoming_qty": Decimal("0.000"),
                "period_start_min": None,
                "period_end_max": None,
                "items": [],
            }
            by_product[product_id] = bucket

        quantity = quantize_qty(item.quantity or 0)
        bucket["incoming_qty"] = quantize_qty(bucket["incoming_qty"] + quantity)

        period_start = getattr(forecast, "period_start", None)
        period_end = getattr(forecast, "period_end", None)
        if period_start and (not bucket["period_start_min"] or period_start < bucket["period_start_min"]):
            bucket["period_start_min"] = period_start
        if period_end and (not bucket["period_end_max"] or period_end > bucket["period_end_max"]):
            bucket["period_end_max"] = period_end

        bucket["items"].append(
            {
                "order_id": order.id,
                "order_number": order.order_number,
                "order_status": order.status,
                "order_status_label": order.get_status_display(),
                "listing_id": getattr(listing, "id", None),
                "listing_unit_price": quantize_money(item.unit_price or 0),
                "seller_name": _producer_display_name(item.seller_producer),
                "quantity": quantity,
                "subtotal": quantize_money(item.subtotal or 0),
                "delivery_mode": getattr(listing, "delivery_mode", None),
                "delivery_mode_label": (
                    listing.get_delivery_mode_display()
                    if listing and hasattr(listing, "get_delivery_mode_display")
                    else "—"
                ),
                "period_start": period_start,
                "period_end": period_end,
                "committed_at": order.created_at,
            }
        )

        total_incoming = quantize_qty(total_incoming + quantity)

    products = list(by_product.values())

    for entry in products:
        entry["incoming_qty"] = quantize_qty(entry["incoming_qty"])
        fallback_now = timezone.now()
        entry["items"].sort(
            key=lambda row: (
                row.get("committed_at") is not None,
                row.get("committed_at") or fallback_now,
            ),
            reverse=True,
        )

    products.sort(key=lambda item: (-item["incoming_qty"], item["product_name"].lower()))

    return {
        "total_incoming_qty": quantize_qty(total_incoming),
        "product_count": len(products),
        "products": products,
        "by_product": by_product,
    }
