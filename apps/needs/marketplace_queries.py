from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Product
from apps.inventory.models import Stock
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.needs.constants import EDITABLE_NEED_STATUSES, PUBLIC_OFFERED_ORDER_STATUSES
from apps.needs.coverage import calculate_need_coverage
from apps.needs.models import Need, NeedResponseStatus, NeedStatus
from apps.needs.responses import get_need_response_summaries_for_responder
from apps.needs.utils import (
    get_need_edit_help_text,
    get_need_minimum_edit_quantity,
    normalize_needs_search_query,
    producer_marketplace_display_name as _producer_marketplace_display_name,
    quantize_need_quantity as _quantize_need_quantity,
)
from apps.orders.models import OrderItem, OrderItemStatus


def _build_need_row(need):
    coverage = calculate_need_coverage(need)
    required_quantity = coverage["required_quantity"]
    completed_qty = coverage["completed_qty"]
    minimum_edit_quantity = get_need_minimum_edit_quantity(coverage)
    progress_percent = Decimal("0")
    if required_quantity > 0:
        progress_percent = (completed_qty / required_quantity) * Decimal("100")

    return {
        "need": need,
        "status": need.status,
        "status_label": need.get_status_display(),
        "producer_label": _producer_marketplace_display_name(need.producer),
        "required_quantity": required_quantity,
        "planned_qty": coverage["planned_qty"],
        "completed_qty": completed_qty,
        "remaining_to_plan": coverage["remaining_to_plan"],
        "remaining_to_receive": coverage["remaining_to_receive"],
        "planned_excess_qty": _quantize_need_quantity(
            max(coverage["planned_qty"] - required_quantity, Decimal("0.000"))
        ),
        "progress_percent": max(Decimal("0"), min(progress_percent, Decimal("100"))),
        "can_edit": need.status in EDITABLE_NEED_STATUSES,
        "minimum_edit_quantity": minimum_edit_quantity,
        "edit_help_text": get_need_edit_help_text(need, coverage),
    }


def _add_offered_quantity(offered_quantities, need_id, quantity):
    need_key = str(need_id)
    offered_quantities[need_key] = _quantize_need_quantity(
        offered_quantities.get(need_key, Decimal("0.000"))
        + _quantize_need_quantity(quantity)
    )


def get_public_offered_quantities_by_need(*, need_ids, viewer_producer=None):
    if not need_ids:
        return {}

    now = timezone.now()
    offered_quantities = {}
    pending_listings = (
        MarketplaceListing.objects
        .filter(
            need_id__in=need_ids,
            status=ListingStatus.ACTIVE,
            need_response_status=NeedResponseStatus.PENDING,
            quantity_available__gt=0,
            order_items__isnull=True,
        )
        .exclude(expires_at__lte=now)
        .only("need_id", "producer_id", "quantity_available")
    )
    if viewer_producer:
        pending_listings = pending_listings.exclude(producer=viewer_producer)

    for listing in pending_listings:
        _add_offered_quantity(
            offered_quantities,
            listing.need_id,
            listing.quantity_available,
        )

    active_order_items = (
        OrderItem.objects
        .filter(
            need_id__in=need_ids,
            order__status__in=PUBLIC_OFFERED_ORDER_STATUSES,
        )
        .exclude(item_status__in=[OrderItemStatus.CANCELLED, OrderItemStatus.COMPLETED])
        .only("need_id", "seller_producer_id", "quantity")
    )
    if viewer_producer:
        active_order_items = active_order_items.exclude(seller_producer=viewer_producer)

    for item in active_order_items:
        _add_offered_quantity(
            offered_quantities,
            item.need_id,
            item.quantity,
        )

    return offered_quantities


def list_marketplace_public_needs(*, viewer_producer=None, q="", category_id=""):
    qs = (
        Need.objects
        .select_related("producer", "producer__user", "product", "product__category")
        .filter(
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            is_marketplace_published=True,
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    if viewer_producer:
        qs = qs.exclude(producer=viewer_producer)

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(producer__display_name__icontains=q)
            | Q(producer__company_name__icontains=q)
            | Q(producer__user__first_name__icontains=q)
            | Q(producer__user__last_name__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    rows = []
    for need in qs:
        row = _build_need_row(need)
        if row["remaining_to_plan"] > 0:
            row["public_status_label"] = "Aberta"
            row["public_status"] = NeedStatus.OPEN
            row["public_quantity"] = row["remaining_to_plan"]
            row["public_offered_quantity"] = Decimal("0.000")
            need_product_id = getattr(need, "product_id", None) or getattr(getattr(need, "product", None), "id", "")
            response_query = urlencode({"product": str(need_product_id)})
            row["response_url"] = (
                f"{reverse('marketplace:need_respond', args=[need.id])}?{response_query}"
            )
            rows.append(row)

    if viewer_producer and rows:
        offered_quantities = get_public_offered_quantities_by_need(
            need_ids=[row["need"].id for row in rows],
            viewer_producer=viewer_producer,
        )
        summaries = get_need_response_summaries_for_responder(
            responder_producer=viewer_producer,
            need_ids=[row["need"].id for row in rows],
        )
        for row in rows:
            row["public_offered_quantity"] = offered_quantities.get(
                str(row["need"].id),
                Decimal("0.000"),
            )
            row["viewer_response_summary"] = summaries.get(str(row["need"].id))

    return rows


def list_marketplace_my_needs(*, producer, q="", category_id=""):
    qs = (
        Need.objects
        .select_related("producer", "producer__user", "product", "product__category")
        .filter(
            producer=producer,
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED, NeedStatus.COVERED],
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    return [_build_need_row(need) for need in qs]


def list_marketplace_my_published_needs(*, producer, q="", category_id=""):
    if not producer:
        return []
    qs = (
        Need.objects
        .select_related("producer", "producer__user", "product", "product__category")
        .filter(
            producer=producer,
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            is_marketplace_published=True,
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(Q(product__name__icontains=q) | Q(notes__icontains=q))

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    rows = []
    for need in qs:
        row = _build_need_row(need)
        row["public_quantity"] = row["remaining_to_plan"]
        rows.append(row)

    if rows:
        response_counts = get_need_response_counts_for_owner(
            owner_producer=producer,
            need_ids=[row["need"].id for row in rows],
        )
        for row in rows:
            row["response_count"] = response_counts.get(str(row["need"].id), 0)

    return rows


def get_need_response_counts_for_owner(*, owner_producer, need_ids):
    if not owner_producer or not need_ids:
        return {}

    rows = (
        MarketplaceListing.objects
        .filter(
            need_id__in=need_ids,
            need__producer=owner_producer,
        )
        .values("need_id")
        .annotate(total=Count("id"))
    )
    return {str(row["need_id"]): row["total"] for row in rows}


def get_need_candidate_products(producer):
    return (
        Product.objects
        .filter(
            producer_links__producer=producer,
            producer_links__is_active=True,
            is_active=True,
        )
        .distinct()
        .order_by("name")
    )


def get_critical_stock_product_ids(producer, *, product_ids=None):
    if not producer:
        return set()

    qs = Stock.objects.filter(producer=producer)
    if product_ids:
        qs = qs.filter(product_id__in=product_ids)

    critical_product_ids = set()
    from apps.inventory.services import calculate_inventory_commitment_state

    for stock in qs.select_related("product").only(
        "product_id",
        "product__id",
        "product__name",
        "current_quantity",
        "reserved_quantity",
        "safety_stock",
    ):
        commitment_state = calculate_inventory_commitment_state(
            producer,
            stock.product,
            stock=stock,
        )
        if commitment_state.get("state_key") == "critical":
            critical_product_ids.add(str(stock.product_id))
    return critical_product_ids
