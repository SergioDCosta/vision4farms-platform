"""Order domain services: queries."""

from apps.orders.models import Order, OrderGroup, OrderStatus
from decimal import Decimal
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from apps.orders.constants import ORDER_STATUS_LABELS
from apps.orders.utils import _producer_display_name, _quantity_label, quantize_money, quantize_qty


def _collect_order_source_flags(order):
    items = list(getattr(order, "_prefetched_objects_cache", {}).get("items", []) or order.items.all())
    has_stock_source = False
    has_forecast_source = False
    has_unknown_source = False

    for item in items:
        listing = getattr(item, "listing", None)
        if not listing:
            has_unknown_source = True
            continue

        has_stock = bool(getattr(listing, "stock_id", None))
        has_forecast = bool(getattr(listing, "forecast_id", None))
        if has_stock == has_forecast:
            has_unknown_source = True
            continue

        if has_stock:
            has_stock_source = True
        if has_forecast:
            has_forecast_source = True

    return has_stock_source, has_forecast_source, has_unknown_source


def is_order_from_need_response(order):
    items = list(getattr(order, "_prefetched_objects_cache", {}).get("items", []) or order.items.all())
    if not items:
        return False

    return any(
        bool(getattr(item, "need_id", None))
        or bool(getattr(getattr(item, "listing", None), "need_id", None))
        for item in items
    )


def is_order_forecast_only(order):
    if is_order_from_need_response(order):
        return False

    has_stock_source, has_forecast_source, has_unknown_source = _collect_order_source_flags(order)
    return bool(has_forecast_source and not has_stock_source and not has_unknown_source)


def get_order_source_label(order):
    if is_order_from_need_response(order):
        return "Resposta a necessidade"

    has_stock_source, has_forecast_source, _ = _collect_order_source_flags(order)

    if has_forecast_source and not has_stock_source:
        return "Pré-venda"
    if has_stock_source and not has_forecast_source:
        return "Stock atual"
    return "Origem mista"


def compute_order_group_status(order_statuses):
    statuses = [str(status) for status in order_statuses if status]
    if not statuses:
        return OrderStatus.PENDING

    if all(status == OrderStatus.COMPLETED for status in statuses):
        return OrderStatus.COMPLETED

    if all(status == OrderStatus.CANCELLED for status in statuses):
        return OrderStatus.CANCELLED

    if any(status == OrderStatus.DELIVERING for status in statuses):
        return OrderStatus.DELIVERING

    if any(status == OrderStatus.IN_PROGRESS for status in statuses):
        return OrderStatus.IN_PROGRESS

    if any(status == OrderStatus.CONFIRMED for status in statuses):
        return OrderStatus.CONFIRMED

    if any(status == OrderStatus.PENDING for status in statuses):
        return OrderStatus.PENDING

    if any(status == OrderStatus.COMPLETED for status in statuses):
        return OrderStatus.COMPLETED

    if any(status == OrderStatus.CANCELLED for status in statuses):
        return OrderStatus.CANCELLED

    return OrderStatus.PENDING


def get_order_group_status_label(status):
    return ORDER_STATUS_LABELS.get(str(status), str(status))


def _sum_order_items_count(orders):
    total = 0
    for order in orders:
        prefetched_items = getattr(order, "_prefetched_objects_cache", {}).get("items", None)
        if prefetched_items is not None:
            total += len(prefetched_items)
        else:
            total += order.items.count()
    return total


def _sum_total_amount(orders):
    total = Decimal("0.00")
    for order in orders:
        total += Decimal(str(order.total_amount or 0))
    return quantize_money(total)


def _build_group_purchase_entry(group):
    group_orders = list(group.orders.all())
    statuses = [order.status for order in group_orders]
    aggregated_status = compute_order_group_status(statuses)
    return {
        "kind": "group",
        "group": group,
        "orders": group_orders,
        "status": aggregated_status,
        "status_label": get_order_group_status_label(aggregated_status),
        "total_amount": _sum_total_amount(group_orders),
        "item_count": _sum_order_items_count(group_orders),
        "order_count": len(group_orders),
        "created_at": group.created_at,
    }


def _build_legacy_order_purchase_entry(order):
    prefetched_items = getattr(order, "_prefetched_objects_cache", {}).get("items", None)
    item_count = len(prefetched_items) if prefetched_items is not None else order.items.count()
    return {
        "kind": "legacy_order",
        "group": None,
        "orders": [order],
        "order": order,
        "status": order.status,
        "status_label": order.get_status_display(),
        "total_amount": order.total_amount,
        "item_count": item_count,
        "order_count": 1,
        "created_at": order.created_at,
    }


def _format_forecast_period_from_order(order):
    items = list(getattr(order, "_prefetched_objects_cache", {}).get("items", []) or order.items.all())
    period_start_min = None
    period_end_max = None

    for item in items:
        listing = getattr(item, "listing", None)
        forecast = getattr(listing, "forecast", None) if listing else None
        if not forecast:
            continue

        period_start = getattr(forecast, "period_start", None)
        period_end = getattr(forecast, "period_end", None)
        if period_start and (not period_start_min or period_start < period_start_min):
            period_start_min = period_start
        if period_end and (not period_end_max or period_end > period_end_max):
            period_end_max = period_end

    if period_start_min and timezone.is_aware(period_start_min):
        period_start_min = timezone.localtime(period_start_min)
    if period_end_max and timezone.is_aware(period_end_max):
        period_end_max = timezone.localtime(period_end_max)

    if period_start_min and period_end_max:
        return f"{period_start_min.strftime('%d/%m/%Y')} - {period_end_max.strftime('%d/%m/%Y')}"
    if period_start_min:
        return f"A partir de {period_start_min.strftime('%d/%m/%Y')}"
    return "Sem período definido"


def _build_presale_order_entry(*, order, viewer_role):
    prefetched_items = list(getattr(order, "_prefetched_objects_cache", {}).get("items", []) or order.items.all())
    first_item = prefetched_items[0] if prefetched_items else None
    item_count = len(prefetched_items)

    if item_count == 1 and first_item:
        product_label = getattr(getattr(first_item, "product", None), "name", "") or "Produto"
        quantity_label = _quantity_label(
            quantize_qty(first_item.quantity or 0),
            getattr(getattr(first_item, "product", None), "unit", ""),
        )
    else:
        product_label = f"Múltiplos produtos ({item_count})" if item_count > 1 else "Produto"
        quantity_label = "Vários itens"

    if viewer_role == "buyer":
        counterpart = first_item.seller_producer if first_item else None
    else:
        counterpart = order.buyer_producer

    return {
        "order": order,
        "viewer_role": viewer_role,
        "status": order.status,
        "status_label": order.get_status_display(),
        "total_amount": order.total_amount,
        "created_at": order.created_at,
        "product_label": product_label,
        "quantity_label": quantity_label,
        "counterpart_label": _producer_display_name(counterpart),
        "forecast_period_text": _format_forecast_period_from_order(order),
        "is_presale": True,
    }


def get_presale_order_entries_for_producer(*, producer, status=""):
    common_prefetch = [
        "items__product",
        "items__seller_producer__user",
        "items__listing",
        "items__listing__forecast",
    ]

    buyer_qs = (
        Order.objects
        .select_related("buyer_producer__user")
        .prefetch_related(*common_prefetch)
        .filter(buyer_producer=producer)
        .order_by("-created_at")
    )
    seller_qs = (
        Order.objects
        .select_related("buyer_producer__user")
        .prefetch_related(*common_prefetch)
        .filter(items__seller_producer=producer)
        .distinct()
        .order_by("-created_at")
    )

    if status:
        buyer_qs = buyer_qs.filter(status=status)
        seller_qs = seller_qs.filter(status=status)

    buyer_entries = []
    for order in buyer_qs:
        if not is_order_forecast_only(order):
            continue
        buyer_entries.append(_build_presale_order_entry(order=order, viewer_role="buyer"))

    seller_entries = []
    for order in seller_qs:
        if not is_order_forecast_only(order):
            continue
        seller_entries.append(_build_presale_order_entry(order=order, viewer_role="seller"))

    return {
        "buyer_entries": buyer_entries,
        "seller_entries": seller_entries,
    }


def get_buyer_purchase_entries(*, buyer_producer, status=""):
    group_orders_queryset = (
        Order.objects
        .select_related("recommendation", "buyer_producer__user")
        .prefetch_related("items__product", "items__seller_producer__user", "items__listing")
        .order_by("-created_at")
    )
    groups = (
        OrderGroup.objects
        .filter(buyer_producer=buyer_producer)
        .prefetch_related(Prefetch("orders", queryset=group_orders_queryset))
        .order_by("-created_at")
    )

    entries = []

    for group in groups:
        entry = _build_group_purchase_entry(group)
        if entry["order_count"] <= 0:
            continue
        if status and entry["status"] != status:
            continue
        entries.append(entry)

    legacy_orders_queryset = (
        Order.objects
        .select_related("recommendation", "buyer_producer__user")
        .prefetch_related("items__product", "items__seller_producer__user", "items__listing")
        .filter(buyer_producer=buyer_producer, group_id__isnull=True)
        .order_by("-created_at")
    )
    if status:
        legacy_orders_queryset = legacy_orders_queryset.filter(status=status)

    for order in legacy_orders_queryset:
        entries.append(_build_legacy_order_purchase_entry(order))

    entries.sort(key=lambda item: item["created_at"], reverse=True)
    return entries


def get_orders_for_seller(*, seller_producer, status=""):
    qs = (
        Order.objects
        .select_related("recommendation", "buyer_producer__user")
        .prefetch_related("items__product", "items__seller_producer__user")
        .filter(items__seller_producer=seller_producer)
        .distinct()
        .order_by("-created_at")
    )

    if status:
        qs = qs.filter(status=status)

    return qs


def get_order_group_detail_for_buyer(*, buyer_producer, group_id):
    group_queryset = (
        OrderGroup.objects
        .select_related("buyer_producer__user")
        .prefetch_related(
            Prefetch(
                "orders",
                queryset=(
                    Order.objects
                    .select_related("recommendation", "buyer_producer__user")
                    .prefetch_related(
                        "items__product",
                        "items__seller_producer__user",
                        "items__listing",
                        "status_history__changed_by",
                    )
                    .order_by("-created_at")
                ),
            )
        )
        .filter(id=group_id, buyer_producer=buyer_producer)
    )
    return get_object_or_404(group_queryset)


def get_order_detail_for_buyer(*, buyer_producer, order_id):
    return get_object_or_404(
        Order.objects
        .select_related("recommendation", "buyer_producer__user")
        .prefetch_related(
            "items__product",
            "items__seller_producer__user",
            "items__listing",
            "status_history__changed_by",
        ),
        id=order_id,
        buyer_producer=buyer_producer,
    )


def get_order_detail_for_seller(*, seller_producer, order_id):
    queryset = (
        Order.objects
        .select_related("recommendation", "buyer_producer__user")
        .prefetch_related(
            "items__product",
            "items__seller_producer__user",
            "items__listing",
            "status_history__changed_by",
        )
        .filter(
            id=order_id,
            items__seller_producer=seller_producer,
        )
        .distinct()
    )
    return get_object_or_404(queryset)
