from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Max, Sum
from django.utils import timezone

from apps.inventory.constants import (
    COMMERCIAL_IN_PROGRESS_ORDER_STATUSES,
    COMPLETED_ORDER_STATUS,
    MONTH_LABELS_PT,
    MONTH_SHORT_LABELS_PT,
    PRODUCTION_ENTRY_MOVEMENT_TYPES,
    ZERO,
)
from apps.inventory.models import StockMovement
from apps.inventory.utils import (
    aware_datetime as _aware_datetime,
    format_qty as _format_qty,
    safe_int as _safe_int,
    shift_month as _shift_month,
    to_decimal as _to_decimal,
)
from apps.orders.models import Order, OrderItem, OrderItemStatus


def _period_bounds(*, period="annual", year=None, month=None, now=None):
    now = now or timezone.now()
    current_year = now.year
    selected_year = _safe_int(year, current_year)
    if selected_year < 2000 or selected_year > current_year + 1:
        selected_year = current_year

    selected_month = _safe_int(month, now.month)
    if selected_month < 1 or selected_month > 12:
        selected_month = now.month

    selected_period = (period or "annual").strip().lower()
    if selected_period not in {"annual", "monthly"}:
        selected_period = "annual"

    if selected_period == "monthly":
        start = _aware_datetime(selected_year, selected_month, 1)
        end = _shift_month(start, 1)
        previous_start = _shift_month(start, -1)
        previous_end = start
        label = f"{MONTH_LABELS_PT[selected_month - 1]} {selected_year}"
    else:
        start = _aware_datetime(selected_year, 1, 1)
        end = _aware_datetime(selected_year + 1, 1, 1)
        previous_start = _aware_datetime(selected_year - 1, 1, 1)
        previous_end = start
        label = str(selected_year)

    return {
        "period": selected_period,
        "year": selected_year,
        "month": selected_month,
        "start": start,
        "end": end,
        "previous_start": previous_start,
        "previous_end": previous_end,
        "label": label,
    }


def _period_chart_segments(bounds):
    if bounds["period"] == "annual":
        segments = []
        for month in range(1, 13):
            start = _aware_datetime(bounds["year"], month, 1)
            end = _shift_month(start, 1)
            segments.append({
                "label": MONTH_SHORT_LABELS_PT[month - 1],
                "start": start,
                "end": end,
            })
        return segments

    segments = []
    start = bounds["start"]
    end = bounds["end"]
    cursor = start
    while cursor < end:
        segment_end = min(cursor + timedelta(days=7), end)
        segments.append({
            "label": f"{cursor.day}-{(segment_end - timedelta(days=1)).day}",
            "start": cursor,
            "end": segment_end,
        })
        cursor = segment_end
    return segments


def _trend(current, previous):
    current = _to_decimal(current)
    previous = _to_decimal(previous)
    if previous > ZERO:
        pct = ((current - previous) / previous * Decimal("100")).quantize(Decimal("0.1"))
    elif current > ZERO:
        pct = Decimal("100.0")
    else:
        pct = Decimal("0.0")

    if pct > ZERO:
        return {"pct": pct, "direction": "up", "label": "acima do período anterior"}
    if pct < ZERO:
        return {"pct": pct, "direction": "down", "label": "abaixo do período anterior"}
    return {"pct": pct, "direction": "flat", "label": "igual ao período anterior"}


def _producer_name(producer):
    if not producer:
        return "Produtor"
    if getattr(producer, "display_name", None):
        return producer.display_name
    if getattr(producer, "company_name", None):
        return producer.company_name
    user = getattr(producer, "user", None)
    if user:
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        return full_name or user.email or "Produtor"
    return "Produtor"


def _build_order_items_label(items, *, limit=3):
    labels = []
    for item in list(items)[:limit]:
        labels.append(f"{_format_qty(item.quantity)} {item.product.unit} {item.product.name}")
    extra_count = max(len(items) - limit, 0)
    if extra_count:
        labels.append(f"+{extra_count} produto{'' if extra_count == 1 else 's'}")
    return " · ".join(labels) if labels else "Sem itens ativos"


def _purchase_total(producer, start, end):
    return _to_decimal(
        Order.objects.filter(
            buyer_producer=producer,
            status=COMPLETED_ORDER_STATUS,
            completed_at__gte=start,
            completed_at__lt=end,
        ).aggregate(total=Sum("total_amount"))["total"]
    )


def _sales_total(producer, start, end):
    return _to_decimal(
        OrderItem.objects.filter(
            seller_producer=producer,
            order__status=COMPLETED_ORDER_STATUS,
            item_status=OrderItemStatus.COMPLETED,
            order__completed_at__gte=start,
            order__completed_at__lt=end,
        ).aggregate(total=Sum("subtotal"))["total"]
    )


def _production_total(producer, start, end):
    return _to_decimal(
        StockMovement.objects.filter(
            stock__producer=producer,
            movement_type__in=PRODUCTION_ENTRY_MOVEMENT_TYPES,
            quantity_delta__gt=ZERO,
            created_at__gte=start,
            created_at__lt=end,
        ).aggregate(total=Sum("quantity_delta"))["total"]
    )


def _build_commercial_chart(producer, segments):
    points = []
    for segment in segments:
        points.append({
            "label": segment["label"],
            "purchase_total": _purchase_total(producer, segment["start"], segment["end"]),
            "sales_total": _sales_total(producer, segment["start"], segment["end"]),
        })
    return points


def _build_production_chart(producer, segments):
    points = []
    for segment in segments:
        points.append({
            "label": segment["label"],
            "quantity": _production_total(producer, segment["start"], segment["end"]),
        })
    return points


def _build_purchase_history_rows(producer, start, end, limit=8):
    orders = (
        Order.objects
        .filter(buyer_producer=producer, created_at__gte=start, created_at__lt=end)
        .prefetch_related("items__product", "items__seller_producer__user")
        .order_by("-created_at")[:limit]
    )

    rows = []
    for order in orders:
        active_items = [item for item in order.items.all() if item.item_status != OrderItemStatus.CANCELLED]
        sellers = []
        seen_sellers = set()
        for item in active_items:
            seller_id = getattr(item, "seller_producer_id", None)
            if seller_id in seen_sellers:
                continue
            seen_sellers.add(seller_id)
            sellers.append(_producer_name(item.seller_producer))

        rows.append({
            "order": order,
            "detail_url": f"/encomendas/{order.id}/",
            "title": f"Encomenda #{order.order_number}",
            "meta": f"{order.created_at.strftime('%d/%m/%Y %H:%M')} · {order.get_status_display()}",
            "items_label": _build_order_items_label(active_items),
            "counterparty_label": ", ".join(sellers[:2]) if sellers else "Vendedor não identificado",
            "value": _to_decimal(order.total_amount),
        })
    return rows


def _build_sales_history_rows(producer, start, end, limit=8):
    orders = (
        Order.objects
        .filter(items__seller_producer=producer, created_at__gte=start, created_at__lt=end)
        .select_related("buyer_producer__user")
        .prefetch_related("items__product", "items__seller_producer")
        .distinct()
        .order_by("-created_at")[:limit]
    )

    rows = []
    for order in orders:
        seller_items = [
            item for item in order.items.all()
            if item.seller_producer_id == producer.id and item.item_status != OrderItemStatus.CANCELLED
        ]
        value = sum((Decimal(str(item.subtotal or 0)) for item in seller_items), ZERO)
        rows.append({
            "order": order,
            "detail_url": f"/encomendas/{order.id}/",
            "title": f"Encomenda #{order.order_number}",
            "meta": f"{order.created_at.strftime('%d/%m/%Y %H:%M')} · {order.get_status_display()}",
            "items_label": _build_order_items_label(seller_items),
            "counterparty_label": _producer_name(order.buyer_producer),
            "value": value,
        })
    return rows


def get_purchase_dashboard(producer, *, period="annual", year=None, month=None):
    bounds = _period_bounds(period=period, year=year, month=month)
    start = bounds["start"]
    end = bounds["end"]
    previous_start = bounds["previous_start"]
    previous_end = bounds["previous_end"]
    segments = _period_chart_segments(bounds)

    purchase_total = _purchase_total(producer, start, end)
    sales_total = _sales_total(producer, start, end)
    production_total = _production_total(producer, start, end)
    previous_purchase_total = _purchase_total(producer, previous_start, previous_end)
    previous_sales_total = _sales_total(producer, previous_start, previous_end)
    previous_production_total = _production_total(producer, previous_start, previous_end)

    purchase_completed_count = Order.objects.filter(
        buyer_producer=producer,
        status=COMPLETED_ORDER_STATUS,
        completed_at__gte=start,
        completed_at__lt=end,
    ).count()
    sales_completed_count = (
        OrderItem.objects
        .filter(
            seller_producer=producer,
            order__status=COMPLETED_ORDER_STATUS,
            item_status=OrderItemStatus.COMPLETED,
            order__completed_at__gte=start,
            order__completed_at__lt=end,
        )
        .values("order_id")
        .distinct()
        .count()
    )
    purchase_in_progress_count = Order.objects.filter(
        buyer_producer=producer,
        status__in=COMMERCIAL_IN_PROGRESS_ORDER_STATUSES,
    ).count()
    sales_in_progress_count = (
        OrderItem.objects
        .filter(
            seller_producer=producer,
            order__status__in=COMMERCIAL_IN_PROGRESS_ORDER_STATUSES,
        )
        .exclude(item_status__in=[OrderItemStatus.CANCELLED, OrderItemStatus.COMPLETED])
        .values("order_id")
        .distinct()
        .count()
    )

    production_qs = StockMovement.objects.filter(
        stock__producer=producer,
        movement_type__in=PRODUCTION_ENTRY_MOVEMENT_TYPES,
        quantity_delta__gt=ZERO,
        created_at__gte=start,
        created_at__lt=end,
    )
    production_product_count = production_qs.values("stock__product_id").distinct().count()
    top_production_product = (
        production_qs
        .values("stock__product__name", "stock__product__unit")
        .annotate(total_quantity=Sum("quantity_delta"))
        .order_by("-total_quantity")
        .first()
    )

    commercial_points = _build_commercial_chart(producer, segments)
    production_points = _build_production_chart(producer, segments)
    commercial_chart_data = {
        "labels": [point["label"] for point in commercial_points],
        "purchases": [float(point["purchase_total"]) for point in commercial_points],
        "sales": [float(point["sales_total"]) for point in commercial_points],
    }
    production_chart_data = {
        "labels": [point["label"] for point in production_points],
        "quantities": [float(point["quantity"]) for point in production_points],
    }

    top_purchased_products = (
        OrderItem.objects
        .filter(
            order__buyer_producer=producer,
            order__status=COMPLETED_ORDER_STATUS,
            order__completed_at__gte=start,
            order__completed_at__lt=end,
        )
        .exclude(item_status=OrderItemStatus.CANCELLED)
        .values("product__name", "product__unit")
        .annotate(total_quantity=Sum("quantity"), total_amount=Sum("subtotal"))
        .order_by("-total_quantity")[:6]
    )
    top_sold_products = (
        OrderItem.objects
        .filter(
            seller_producer=producer,
            order__status=COMPLETED_ORDER_STATUS,
            item_status=OrderItemStatus.COMPLETED,
            order__completed_at__gte=start,
            order__completed_at__lt=end,
        )
        .values("product__name", "product__unit")
        .annotate(total_quantity=Sum("quantity"), total_amount=Sum("subtotal"))
        .order_by("-total_quantity")[:6]
    )
    production_product_rows = (
        production_qs
        .values("stock__product__name", "stock__product__unit")
        .annotate(
            total_quantity=Sum("quantity_delta"),
            movement_count=Count("id"),
            last_movement_at=Max("created_at"),
        )
        .order_by("-total_quantity")[:8]
    )
    production_product_chart_data = {
        "labels": [row["stock__product__name"] for row in production_product_rows],
        "quantities": [float(row["total_quantity"] or 0) for row in production_product_rows],
    }

    return {
        "commercial_period": bounds["period"],
        "commercial_year": bounds["year"],
        "commercial_month": bounds["month"],
        "commercial_period_label": bounds["label"],
        "commercial_year_options": list(range(timezone.now().year + 1, timezone.now().year - 6, -1)),
        "commercial_month_options": [
            {"value": idx, "label": MONTH_LABELS_PT[idx - 1]}
            for idx in range(1, 13)
        ],
        "purchase_total_period": purchase_total,
        "purchase_completed_count_period": purchase_completed_count,
        "purchase_trend": _trend(purchase_total, previous_purchase_total),
        "sales_total_period": sales_total,
        "sales_completed_count_period": sales_completed_count,
        "sales_trend": _trend(sales_total, previous_sales_total),
        "commercial_balance": sales_total - purchase_total,
        "purchase_in_progress_count": purchase_in_progress_count,
        "sales_in_progress_count": sales_in_progress_count,
        "production_total_period": production_total,
        "production_product_count": production_product_count,
        "production_trend": _trend(production_total, previous_production_total),
        "top_production_product": top_production_product,
        "commercial_chart_data": commercial_chart_data,
        "production_chart_data": production_chart_data,
        "production_product_chart_data": production_product_chart_data,
        "purchase_chart_points": commercial_points,
        "recent_orders": _build_purchase_history_rows(producer, start, end),
        "recent_sales_rows": _build_sales_history_rows(producer, start, end),
        "top_products": top_purchased_products,
        "top_sold_products": top_sold_products,
        "production_product_rows": production_product_rows,
    }

def get_recent_orders_for_export(producer, limit=50):
    recent_orders = (
        Order.objects
        .filter(buyer_producer=producer)
        .order_by("-created_at")[:limit]
    )

    export_total = _to_decimal(
        Order.objects.filter(buyer_producer=producer).aggregate(
            total=Sum("total_amount")
        )["total"]
    )

    return {
        "recent_orders": recent_orders,
        "export_total": export_total,
    }
