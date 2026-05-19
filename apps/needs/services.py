from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Product
from apps.inventory.models import Stock
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.needs.models import Need, NeedResponseStatus, NeedSourceSystem, NeedStatus
from apps.orders.models import OrderItem, OrderItemStatus, OrderStatus


ACTIVE_NEED_STATUSES = [NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED]
EDITABLE_NEED_STATUSES = [NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED, NeedStatus.COVERED]
PLANNED_NEED_ORDER_STATUSES = [
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.DELIVERING,
]
PUBLIC_OFFERED_ORDER_STATUSES = [
    OrderStatus.PENDING,
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.DELIVERING,
]
NEEDS_SEARCH_QUERY_MAX_LENGTH = 120
NEED_NOTES_MAX_LENGTH = 1200
NEED_RESPONSE_NOTES_MAX_LENGTH = 1200


class DuplicateActiveNeedError(ValidationError):
    def __init__(self, existing_need):
        self.existing_need = existing_need
        super().__init__("Já existe uma necessidade ativa para este produto. Edite essa necessidade em vez de criar outra.")


@dataclass(frozen=True)
class NeedResponse:
    listing: MarketplaceListing
    id: object
    need_id: object
    producer_label: str
    need_owner_label: str
    product_name: str
    product_unit: str
    offered_quantity: Decimal
    available_quantity: Decimal
    ordered_quantity: Decimal
    quantity_available: Decimal
    unit_price: Decimal
    source_key: str
    source_label: str
    status: str
    status_label: str
    response_status: str
    response_status_label: str
    response_badge_class: str
    response_message: str
    can_buy: bool
    can_reject: bool
    notes: str
    detail_url: str
    reject_url: str
    edit_url: str
    is_editable: bool
    cta_label: str = "Ver oferta e comprar"


@dataclass(frozen=True)
class NeedResponseSummary:
    listing_id: object
    status: str
    status_label: str
    badge_class: str
    message: str
    detail_url: str
    is_active: bool
    edit_url: str = ""
    can_edit: bool = False
    can_send_new_proposal: bool = False


def _quantize_need_quantity(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))


def normalize_needs_search_query(value):
    return (value or "").strip()[:NEEDS_SEARCH_QUERY_MAX_LENGTH]


def is_listing_effectively_expired(listing, *, now=None):
    expires_at = getattr(listing, "expires_at", None)
    if not expires_at or not isinstance(expires_at, datetime):
        return False
    now = now or timezone.now()
    return expires_at <= now


def _normalize_needed_by_date(value):
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
                parsed = datetime.combine(datetime.strptime(raw_value, "%Y-%m-%d").date(), time.max)
        except ValueError:
            raise ValidationError("Data limite inválida para a necessidade.")

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def get_need_minimum_edit_quantity(coverage):
    return _quantize_need_quantity(
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
        return f"Esta necessidade já tem encomendas associadas. A quantidade mínima permitida é {minimum_quantity} {unit}."
    return "Pode ajustar a quantidade, a data limite e as observações. O produto mantém-se fixo."


def _producer_marketplace_display_name(producer):
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


def calculate_need_coverage(need):
    required_quantity = _quantize_need_quantity(need.required_quantity)
    planned_qty = Decimal("0.000")
    completed_qty = Decimal("0.000")

    items = (
        OrderItem.objects
        .filter(need_id=need.id)
        .select_related("order")
    )

    for item in items:
        item_status = item.item_status
        if item_status == OrderItemStatus.CANCELLED:
            continue

        quantity = _quantize_need_quantity(item.quantity)

        if item_status == OrderItemStatus.COMPLETED:
            planned_qty += quantity
            completed_qty += quantity
            continue

        if item_status == OrderItemStatus.IN_DELIVERY:
            planned_qty += quantity
            continue

        if item_status == OrderItemStatus.CONFIRMED:
            order_status = getattr(getattr(item, "order", None), "status", None)
            if order_status in PLANNED_NEED_ORDER_STATUSES:
                planned_qty += quantity

    planned_qty = _quantize_need_quantity(planned_qty)
    completed_qty = _quantize_need_quantity(completed_qty)
    remaining_to_plan = _quantize_need_quantity(max(required_quantity - planned_qty, Decimal("0.000")))
    remaining_to_receive = _quantize_need_quantity(max(required_quantity - completed_qty, Decimal("0.000")))

    return {
        "required_quantity": required_quantity,
        "planned_qty": planned_qty,
        "completed_qty": completed_qty,
        "remaining_to_plan": remaining_to_plan,
        "remaining_to_receive": remaining_to_receive,
    }


def _resolve_need_status(need, coverage):
    if need.status == NeedStatus.IGNORED:
        return NeedStatus.IGNORED

    if coverage["completed_qty"] >= coverage["required_quantity"]:
        return NeedStatus.COVERED

    if coverage["planned_qty"] > 0:
        return NeedStatus.PARTIALLY_COVERED

    return NeedStatus.OPEN


@transaction.atomic
def update_need(
    *,
    need,
    producer,
    required_quantity,
    needed_by_date=None,
    notes=None,
):
    if not need:
        raise ValidationError("Necessidade inválida.")

    locked_need = (
        Need.objects
        .select_for_update()
        .select_related("product", "producer")
        .get(id=need.id)
    )
    if locked_need.producer_id != producer.id:
        raise ValidationError("Não pode editar esta necessidade.")
    if locked_need.status not in EDITABLE_NEED_STATUSES:
        raise ValidationError("Esta necessidade já não pode ser editada.")

    quantity = _quantize_need_quantity(required_quantity)
    if quantity <= Decimal("0.000"):
        raise ValidationError("A quantidade necessária deve ser superior a zero.")

    coverage = calculate_need_coverage(locked_need)
    minimum_quantity = get_need_minimum_edit_quantity(coverage)
    if quantity < minimum_quantity:
        raise ValidationError(
            f"A quantidade mínima permitida é {minimum_quantity} {locked_need.product.unit}, "
            "porque já existem encomendas associadas."
        )

    normalized_deadline = _normalize_needed_by_date(needed_by_date)
    normalized_notes = (notes or "").strip() or None

    changed = (
        locked_need.required_quantity != quantity
        or locked_need.needed_by_date != normalized_deadline
        or (locked_need.notes or None) != normalized_notes
    )

    if changed:
        locked_need.required_quantity = quantity
        locked_need.needed_by_date = normalized_deadline
        locked_need.notes = normalized_notes
        if hasattr(locked_need, "updated_at"):
            locked_need.updated_at = timezone.now()
            locked_need.save(update_fields=["required_quantity", "needed_by_date", "notes", "updated_at"])
        else:
            locked_need.save(update_fields=["required_quantity", "needed_by_date", "notes"])

    updated_need, updated_coverage, status_changed = recalculate_need_status(locked_need)
    return updated_need, updated_coverage, bool(changed or status_changed)


@transaction.atomic
def recalculate_need_status(need, *, acting_user=None):
    need = Need.objects.select_for_update().get(id=need.id)
    if need.status == NeedStatus.IGNORED:
        return need, calculate_need_coverage(need), False

    coverage = calculate_need_coverage(need)
    next_status = _resolve_need_status(need, coverage)
    status_changed = False

    if need.status != next_status:
        need.status = next_status
        if hasattr(need, "updated_at"):
            need.updated_at = timezone.now()
            need.save(update_fields=["status", "updated_at"])
        else:
            need.save(update_fields=["status"])
        status_changed = True

    return need, coverage, status_changed


@transaction.atomic
def recalculate_needs_for_order(order, *, acting_user=None):
    need_ids = list(
        OrderItem.objects
        .filter(order_id=order.id, need_id__isnull=False)
        .values_list("need_id", flat=True)
        .distinct()
    )
    if not need_ids:
        return []

    needs = list(
        Need.objects
        .select_for_update()
        .filter(id__in=need_ids)
    )

    results = []
    for need in needs:
        _, coverage, changed = recalculate_need_status(
            need,
            acting_user=acting_user,
        )
        results.append({
            "need": need,
            "coverage": coverage,
            "changed": changed,
        })

    return results


def get_need_for_producer(*, producer, need_id):
    return Need.objects.filter(id=need_id, producer=producer).select_related(
        "product",
        "product__category",
        "producer",
        "producer__user",
    ).first()


@transaction.atomic
def create_need(
    *,
    producer,
    product,
    required_quantity,
    needed_by_date=None,
    source_system=NeedSourceSystem.MANUAL,
    external_id=None,
    notes=None,
):
    quantity = _quantize_need_quantity(required_quantity)
    if quantity <= Decimal("0.000"):
        raise ValidationError("A quantidade necessária deve ser superior a zero.")

    existing_need = (
        Need.objects
        .select_for_update()
        .filter(
            producer=producer,
            product=product,
            status__in=ACTIVE_NEED_STATUSES,
        )
        .order_by("-updated_at", "-created_at")
        .first()
    )
    if existing_need:
        raise DuplicateActiveNeedError(existing_need)

    need = Need.objects.create(
        producer=producer,
        product=product,
        required_quantity=quantity,
        needed_by_date=needed_by_date,
        source_system=source_system,
        external_id=external_id,
        notes=(notes or "").strip() or None,
        status=NeedStatus.OPEN,
    )

    need, coverage, _ = recalculate_need_status(need)
    return need, coverage


@transaction.atomic
def create_or_update_need(**kwargs):
    need, coverage = create_need(**kwargs)
    return need, coverage, True


@transaction.atomic
def ignore_need(*, need, producer):
    if not need or need.producer_id != producer.id:
        raise ValidationError("Necessidade inválida para este produtor.")

    if need.status == NeedStatus.IGNORED:
        return False

    need.status = NeedStatus.IGNORED
    if hasattr(need, "updated_at"):
        need.updated_at = timezone.now()
        need.save(update_fields=["status", "updated_at"])
    else:
        need.save(update_fields=["status"])
    return True


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
        if row["remaining_to_receive"] > 0:
            row["public_status_label"] = "Aberta"
            row["public_status"] = NeedStatus.OPEN
            row["public_quantity"] = row["remaining_to_receive"]
            row["public_offered_quantity"] = Decimal("0.000")
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
    for stock in qs.only("product_id", "current_quantity", "reserved_quantity", "safety_stock"):
        current_quantity = Decimal(str(stock.current_quantity or 0))
        reserved_quantity = Decimal(str(stock.reserved_quantity or 0))
        safety_stock = Decimal(str(stock.safety_stock or 0))
        if current_quantity - reserved_quantity < safety_stock:
            critical_product_ids.add(str(stock.product_id))
    return critical_product_ids


def _get_need_response_listings_for_owner(*, owner_producer, q="", category_id="", need_id=""):
    qs = (
        MarketplaceListing.objects
        .select_related(
            "producer",
            "producer__user",
            "product",
            "stock",
            "forecast",
            "need",
            "need__producer",
            "need__producer__user",
        )
        .filter(
            need_id__isnull=False,
            need__producer=owner_producer,
        )
        .order_by("-published_at", "-created_at")
    )

    if need_id:
        qs = qs.filter(need_id=need_id)

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(producer__display_name__icontains=q)
            | Q(producer__company_name__icontains=q)
            | Q(producer__user__first_name__icontains=q)
            | Q(producer__user__last_name__icontains=q)
            | Q(notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    return qs


def _get_need_response_listing_queryset():
    return (
        MarketplaceListing.objects
        .select_related(
            "producer",
            "producer__user",
            "product",
            "stock",
            "forecast",
            "need",
            "need__producer",
            "need__producer__user",
            "need__product",
        )
        .filter(need_id__isnull=False)
    )


def get_need_response_listing_for_viewer(*, viewer_producer, listing_id):
    if not viewer_producer:
        return None

    return (
        _get_need_response_listing_queryset()
        .filter(id=listing_id)
        .filter(Q(need__producer=viewer_producer) | Q(producer=viewer_producer))
        .first()
    )


def get_active_need_response_for_responder(*, responder_producer, need):
    if not responder_producer or not need:
        return None

    listings = list(
        _get_need_response_listing_queryset()
        .filter(producer=responder_producer, need=need)
        .order_by("-published_at", "-created_at")
    )
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    for listing in listings:
        state = _derive_need_response_state(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
        )
        if state["is_active"]:
            return listing
    return None


def get_editable_need_response_for_responder(*, responder_producer, listing_id):
    if not responder_producer or not listing_id:
        return None

    listing = (
        _get_need_response_listing_queryset()
        .filter(id=listing_id, producer=responder_producer)
        .exclude(need__producer=responder_producer)
        .first()
    )
    if not listing:
        return None

    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids([listing.id])
    state = _derive_need_response_state(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
    )
    if (
        state["is_active"]
        and state["status"] == NeedResponseStatus.PENDING
        and listing.status == ListingStatus.ACTIVE
    ):
        return listing
    return None


def _get_need_response_listing_for_update(listing_id):
    return (
        MarketplaceListing.objects
        .select_for_update()
        .select_related(
            "producer",
            "product",
            "stock",
            "forecast",
            "need",
            "need__producer",
        )
        .filter(need_id__isnull=False)
        .get(id=listing_id)
    )


def _listing_source_label(listing):
    has_stock_source = bool(getattr(listing, "stock_id", None))
    has_forecast_source = bool(getattr(listing, "forecast_id", None))
    if has_forecast_source and not has_stock_source:
        return "forecast", "Pré-venda"
    return "stock", "Disponível agora"


def get_need_response_order_snapshot(listing_ids):
    if not listing_ids:
        return {}

    rows = (
        OrderItem.objects
        .filter(
            listing_id__in=listing_ids,
            need_id__isnull=False,
        )
        .values_list("listing_id", "item_status", "quantity")
    )
    snapshots = {}
    for listing_id, item_status, quantity in rows:
        snapshot = snapshots.setdefault(
            listing_id,
            {
                "item_statuses": [],
                "active_or_completed_quantity": Decimal("0.000"),
                "cancelled_quantity": Decimal("0.000"),
                "completed_quantity": Decimal("0.000"),
                "ordered_quantity": Decimal("0.000"),
                "status": None,
            },
        )
        snapshot["item_statuses"].append(item_status)
        quantity = _quantize_need_quantity(quantity)
        if item_status == OrderItemStatus.CANCELLED:
            snapshot["cancelled_quantity"] = _quantize_need_quantity(snapshot["cancelled_quantity"] + quantity)
        else:
            snapshot["active_or_completed_quantity"] = _quantize_need_quantity(
                snapshot["active_or_completed_quantity"] + quantity
            )
        if item_status == OrderItemStatus.COMPLETED:
            snapshot["completed_quantity"] = _quantize_need_quantity(snapshot["completed_quantity"] + quantity)

    for snapshot in snapshots.values():
        statuses = snapshot["item_statuses"]
        if any(status == OrderItemStatus.COMPLETED for status in statuses):
            snapshot["status"] = NeedResponseStatus.COMPLETED
            snapshot["ordered_quantity"] = snapshot["completed_quantity"]
        elif any(status != OrderItemStatus.CANCELLED for status in statuses):
            snapshot["status"] = NeedResponseStatus.ACCEPTED
            snapshot["ordered_quantity"] = snapshot["active_or_completed_quantity"]
        elif statuses:
            snapshot["status"] = NeedResponseStatus.CANCELLED
            snapshot["ordered_quantity"] = snapshot["cancelled_quantity"]

    return snapshots


def _get_need_response_order_state_listing_ids(listing_ids):
    snapshots = get_need_response_order_snapshot(listing_ids)
    accepted_listing_ids = set()
    cancelled_listing_ids = set()
    completed_listing_ids = set()
    for listing_id, snapshot in snapshots.items():
        if snapshot["status"] == NeedResponseStatus.CANCELLED:
            cancelled_listing_ids.add(listing_id)
        elif snapshot["status"] == NeedResponseStatus.COMPLETED:
            completed_listing_ids.add(listing_id)
        elif snapshot["status"] == NeedResponseStatus.ACCEPTED:
            accepted_listing_ids.add(listing_id)
    return accepted_listing_ids, cancelled_listing_ids, completed_listing_ids


def _get_accepted_need_response_listing_ids(listing_ids):
    accepted_listing_ids, _, completed_listing_ids = _get_need_response_order_state_listing_ids(listing_ids)
    return accepted_listing_ids | completed_listing_ids


def _derive_need_response_state(
    listing,
    *,
    accepted_listing_ids=None,
    cancelled_listing_ids=None,
    completed_listing_ids=None,
    order_snapshots=None,
):
    accepted_listing_ids = accepted_listing_ids or set()
    cancelled_listing_ids = cancelled_listing_ids or set()
    completed_listing_ids = completed_listing_ids or set()
    order_snapshots = order_snapshots or {}
    order_snapshot = order_snapshots.get(listing.id) or {}
    response_status = getattr(listing, "need_response_status", NeedResponseStatus.PENDING)

    if response_status != NeedResponseStatus.REJECTED and order_snapshot.get("status"):
        response_status = order_snapshot["status"]

    if response_status == NeedResponseStatus.COMPLETED or listing.id in completed_listing_ids:
        return {
            "status": NeedResponseStatus.COMPLETED,
            "label": NeedResponseStatus.COMPLETED.label,
            "badge_class": "ok",
            "message": "A encomenda criada a partir desta oferta foi concluída.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.REJECTED:
        return {
            "status": "REJECTED",
            "label": NeedResponseStatus.REJECTED.label,
            "badge_class": "danger",
            "message": "Esta oferta foi rejeitada pelo produtor da necessidade.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.ACCEPTED or listing.id in accepted_listing_ids:
        return {
            "status": NeedResponseStatus.ACCEPTED,
            "label": NeedResponseStatus.ACCEPTED.label,
            "badge_class": "ok",
            "message": "Esta oferta já foi aceite e originou uma encomenda.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.CANCELLED or listing.id in cancelled_listing_ids:
        return {
            "status": NeedResponseStatus.CANCELLED,
            "label": NeedResponseStatus.CANCELLED.label,
            "badge_class": "danger",
            "message": "A encomenda criada a partir desta oferta foi cancelada.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if (
        response_status == NeedResponseStatus.EXPIRED
        or listing.status == ListingStatus.EXPIRED
        or is_listing_effectively_expired(listing)
    ):
        return {
            "status": NeedResponseStatus.EXPIRED,
            "label": NeedResponseStatus.EXPIRED.label,
            "badge_class": "muted",
            "message": "Esta oferta expirou.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.WITHDRAWN or listing.status == ListingStatus.CANCELLED:
        return {
            "status": NeedResponseStatus.WITHDRAWN,
            "label": NeedResponseStatus.WITHDRAWN.label,
            "badge_class": "muted",
            "message": "Esta oferta foi retirada.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if listing.status == ListingStatus.ACTIVE:
        return {
            "status": NeedResponseStatus.PENDING,
            "label": NeedResponseStatus.PENDING.label,
            "badge_class": "warn",
            "message": "Esta oferta aguarda decisão do produtor da necessidade.",
            "is_active": True,
            "can_buy": Decimal(str(listing.quantity_available or 0)) > Decimal("0"),
            "can_reject": True,
        }

    return {
        "status": listing.status,
        "label": listing.get_status_display(),
        "badge_class": "info",
        "message": "Esta oferta já não está no estado inicial.",
        "is_active": False,
        "can_buy": False,
        "can_reject": False,
    }


def _build_need_response(
    listing,
    *,
    accepted_listing_ids=None,
    cancelled_listing_ids=None,
    completed_listing_ids=None,
    order_snapshots=None,
):
    order_snapshots = order_snapshots or {}
    order_snapshot = order_snapshots.get(listing.id) or {}
    source_key, source_label = _listing_source_label(listing)
    state = _derive_need_response_state(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
        order_snapshots=order_snapshots,
    )
    is_editable = bool(
        state["is_active"]
        and state["status"] == NeedResponseStatus.PENDING
        and listing.status == ListingStatus.ACTIVE
    )
    return NeedResponse(
        listing=listing,
        id=listing.id,
        need_id=listing.need_id,
        producer_label=_producer_marketplace_display_name(listing.producer),
        need_owner_label=_producer_marketplace_display_name(getattr(getattr(listing, "need", None), "producer", None)),
        product_name=listing.product.name,
        product_unit=listing.product.unit,
        offered_quantity=_quantize_need_quantity(
            getattr(listing, "quantity_total", getattr(listing, "quantity_available", Decimal("0.000")))
        ),
        available_quantity=_quantize_need_quantity(listing.quantity_available),
        ordered_quantity=_quantize_need_quantity(order_snapshot.get("ordered_quantity") or Decimal("0.000")),
        quantity_available=listing.quantity_available,
        unit_price=listing.unit_price,
        source_key=source_key,
        source_label=source_label,
        status=listing.status,
        status_label=listing.get_status_display(),
        response_status=state["status"],
        response_status_label=state["label"],
        response_badge_class=state["badge_class"],
        response_message=state["message"],
        can_buy=state["can_buy"],
        can_reject=state["can_reject"],
        notes=listing.notes or "",
        detail_url=reverse("needs:response_detail", args=[listing.id]),
        reject_url=reverse("needs:response_reject", args=[listing.id]),
        edit_url=reverse("needs:response_edit", args=[listing.id]) if is_editable else "",
        is_editable=is_editable,
    )


def list_need_responses_for_owner(*, owner_producer, q="", category_id="", need_id=""):
    listings = list(
        _get_need_response_listings_for_owner(
            owner_producer=owner_producer,
            q=q,
            category_id=category_id,
            need_id=need_id,
        )
    )
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    order_snapshots = get_need_response_order_snapshot([listing.id for listing in listings])
    return [
        _build_need_response(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
            order_snapshots=order_snapshots,
        )
        for listing in listings
    ]


def build_need_response_for_listing(listing):
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids([listing.id])
    order_snapshots = get_need_response_order_snapshot([listing.id])
    return _build_need_response(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
        order_snapshots=order_snapshots,
    )


def list_need_responses_for_responder(*, responder_producer, q="", category_id=""):
    if not responder_producer:
        return []

    qs = (
        _get_need_response_listing_queryset()
        .filter(producer=responder_producer)
        .exclude(need__producer=responder_producer)
        .order_by("-published_at", "-created_at")
    )

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(need__producer__display_name__icontains=q)
            | Q(need__producer__company_name__icontains=q)
            | Q(need__producer__user__first_name__icontains=q)
            | Q(need__producer__user__last_name__icontains=q)
            | Q(notes__icontains=q)
            | Q(need__notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    listings = list(qs)
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    order_snapshots = get_need_response_order_snapshot([listing.id for listing in listings])
    return [
        _build_need_response(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
            order_snapshots=order_snapshots,
        )
        for listing in listings
    ]


def get_need_response_summaries_for_responder(*, responder_producer, need_ids):
    if not responder_producer or not need_ids:
        return {}

    listings = list(
        _get_need_response_listing_queryset()
        .filter(producer=responder_producer, need_id__in=need_ids)
        .order_by("need_id", "-published_at", "-created_at")
    )
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    order_snapshots = get_need_response_order_snapshot([listing.id for listing in listings])

    summaries = {}
    for listing in listings:
        need_key = str(listing.need_id)
        if need_key in summaries:
            continue
        state = _derive_need_response_state(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
            order_snapshots=order_snapshots,
        )
        can_edit = bool(
            state["is_active"]
            and state["status"] == NeedResponseStatus.PENDING
            and listing.status == ListingStatus.ACTIVE
        )
        summaries[need_key] = NeedResponseSummary(
            listing_id=listing.id,
            status=state["status"],
            status_label=state["label"],
            badge_class=state["badge_class"],
            message=state["message"],
            detail_url=reverse("needs:response_detail", args=[listing.id]),
            is_active=state["is_active"],
            edit_url=reverse("needs:response_edit", args=[listing.id]) if can_edit else "",
            can_edit=can_edit,
            can_send_new_proposal=state["status"] in {"REJECTED", "CANCELLED", "EXPIRED", "WITHDRAWN", "COMPLETED"},
        )

    return summaries


def _resolve_persisted_need_response_status(listing, order_snapshot):
    current_status = getattr(listing, "need_response_status", NeedResponseStatus.PENDING)
    if current_status == NeedResponseStatus.REJECTED:
        return NeedResponseStatus.REJECTED

    if order_snapshot and order_snapshot.get("status"):
        return order_snapshot["status"]

    if listing.status == ListingStatus.EXPIRED:
        return NeedResponseStatus.EXPIRED
    if listing.status == ListingStatus.CANCELLED:
        return NeedResponseStatus.WITHDRAWN
    return NeedResponseStatus.PENDING


def sync_need_response_status_for_listing(listing):
    if not listing or not getattr(listing, "need_id", None):
        return listing

    order_snapshot = get_need_response_order_snapshot([listing.id]).get(listing.id)
    next_status = _resolve_persisted_need_response_status(listing, order_snapshot)
    if listing.need_response_status != next_status:
        listing.need_response_status = next_status
        if hasattr(listing, "updated_at"):
            listing.updated_at = timezone.now()
            listing.save(update_fields=["need_response_status", "updated_at"])
        else:
            listing.save(update_fields=["need_response_status"])
    return listing


@transaction.atomic
def update_need_response(
    *,
    listing,
    responder_producer,
    quantity,
    unit_price,
    delivery_mode,
    delivery_radius_km=None,
    delivery_fee=None,
    notes=None,
):
    from apps.marketplace.services import MarketplaceServiceError, update_listing

    listing = _get_need_response_listing_for_update(listing.id)
    if not responder_producer or listing.producer_id != responder_producer.id:
        raise ValidationError("Não pode editar esta proposta.")
    if not listing.need or listing.need.producer_id == responder_producer.id:
        raise ValidationError("Proposta inválida para edição.")

    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids([listing.id])
    state = _derive_need_response_state(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
    )
    if not (
        state["is_active"]
        and state["status"] == NeedResponseStatus.PENDING
        and listing.status == ListingStatus.ACTIVE
    ):
        raise ValidationError("Esta proposta já não pode ser editada.")

    try:
        updated_listing = update_listing(
            listing=listing,
            quantity_total=quantity,
            unit_price=unit_price,
            delivery_mode=delivery_mode,
            delivery_radius_km=delivery_radius_km,
            delivery_fee=delivery_fee,
            show_location_on_map=True,
            notes=notes,
            status=ListingStatus.ACTIVE,
            expires_at=None,
            photo_path=listing.photo_path,
        )
    except MarketplaceServiceError as exc:
        raise ValidationError(str(exc)) from exc

    if updated_listing.need_response_status != NeedResponseStatus.PENDING:
        updated_listing.need_response_status = NeedResponseStatus.PENDING
        updated_listing.save(update_fields=["need_response_status"])
    return updated_listing


@transaction.atomic
def reject_need_response(*, listing, owner_producer):
    listing = _get_need_response_listing_for_update(listing.id)

    if not owner_producer or not listing.need or listing.need.producer_id != owner_producer.id:
        raise ValidationError("Não tem permissão para rejeitar esta oferta.")

    if _get_accepted_need_response_listing_ids([listing.id]):
        raise ValidationError("Esta oferta já foi aceite e não pode ser rejeitada.")

    if listing.need_response_status == NeedResponseStatus.REJECTED and listing.status == ListingStatus.CANCELLED:
        return False

    listing.need_response_status = NeedResponseStatus.REJECTED
    listing.status = ListingStatus.CANCELLED
    listing.updated_at = timezone.now()
    listing.save(update_fields=["need_response_status", "status", "updated_at"])
    try:
        from apps.alerts.models import AlertSeverity, AlertType
        from apps.alerts.services import create_need_response_event_alert

        create_need_response_event_alert(
            target_producer=listing.producer,
            listing=listing,
            alert_type=AlertType.OFFER_REJECTED,
            title=f"Oferta rejeitada: {listing.product.name}",
            description="O produtor da necessidade rejeitou a sua proposta. Pode enviar uma nova proposta se fizer sentido.",
            action_url=f"/necessidades/?need={listing.need_id}",
            action_label="Ver necessidade",
            acting_user=getattr(owner_producer, "user", None),
            severity=AlertSeverity.INFO,
            requires_action=False,
        )
    except Exception:
        pass
    return True
