from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.catalog.models import Product
from apps.marketplace.models import MarketplaceListing
from apps.needs.models import Need, NeedSourceSystem, NeedStatus
from apps.orders.models import OrderItem, OrderItemStatus, OrderStatus


ACTIVE_NEED_STATUSES = [NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED]
PLANNED_NEED_ORDER_STATUSES = [
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.DELIVERING,
]


def _quantize_need_quantity(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))


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
def create_or_update_need(
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

    active_needs = list(
        Need.objects
        .select_for_update()
        .filter(
            producer=producer,
            product=product,
            status__in=ACTIVE_NEED_STATUSES,
        )
        .order_by("-updated_at", "-created_at")
    )

    if active_needs:
        need = active_needs[0]
        need.required_quantity = quantity
        need.needed_by_date = needed_by_date
        need.source_system = source_system
        need.external_id = external_id
        need.notes = notes or None
        if hasattr(need, "updated_at"):
            need.updated_at = timezone.now()
            need.save(
                update_fields=[
                    "required_quantity",
                    "needed_by_date",
                    "source_system",
                    "external_id",
                    "notes",
                    "updated_at",
                ]
            )
        else:
            need.save(
                update_fields=[
                    "required_quantity",
                    "needed_by_date",
                    "source_system",
                    "external_id",
                    "notes",
                ]
            )
        created = False
    else:
        need = Need.objects.create(
            producer=producer,
            product=product,
            required_quantity=quantity,
            needed_by_date=needed_by_date,
            source_system=source_system,
            external_id=external_id,
            notes=notes or None,
            status=NeedStatus.OPEN,
        )
        created = True

    need, coverage, _ = recalculate_need_status(need)
    return need, coverage, created


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
        "progress_percent": max(Decimal("0"), min(progress_percent, Decimal("100"))),
    }


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
        q = q.strip()
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
            rows.append(row)
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
        q = q.strip()
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    return [_build_need_row(need) for need in qs]


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


def get_need_response_listings_for_owner(*, owner_producer, q="", category_id="", need_id=""):
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
        q = q.strip()
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
