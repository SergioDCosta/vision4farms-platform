from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.common.audit import log_audit_event
from apps.needs.audit import audit_quantity, need_marketplace_audit_values
from apps.needs.constants import PLANNED_NEED_ORDER_STATUSES
from apps.needs.models import Need, NeedSourceSystem, NeedStatus
from apps.needs.utils import quantize_need_quantity
from apps.orders.models import OrderItem, OrderItemStatus


def calculate_need_coverage(need):
    required_quantity = quantize_need_quantity(need.required_quantity)
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

        quantity = quantize_need_quantity(item.quantity)

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

    planned_qty = quantize_need_quantity(planned_qty)
    completed_qty = quantize_need_quantity(completed_qty)
    remaining_to_plan = quantize_need_quantity(
        max(required_quantity - planned_qty, Decimal("0.000"))
    )
    remaining_to_receive = quantize_need_quantity(
        max(required_quantity - completed_qty, Decimal("0.000"))
    )

    return {
        "required_quantity": required_quantity,
        "planned_qty": planned_qty,
        "completed_qty": completed_qty,
        "remaining_to_plan": remaining_to_plan,
        "remaining_to_receive": remaining_to_receive,
    }


def resolve_need_status(need, coverage):
    if need.status == NeedStatus.IGNORED:
        return NeedStatus.IGNORED

    if getattr(need, "source_system", None) == NeedSourceSystem.CUSTOMER_DEMAND:
        from apps.needs.services import calculate_external_demand_plan

        plan = calculate_external_demand_plan(producer=need.producer, product=need.product)
        if quantize_need_quantity(plan.get("max_deficit")) <= Decimal("0.000"):
            return NeedStatus.COVERED

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
    next_status = resolve_need_status(need, coverage)
    status_changed = False

    publication_values_before = None
    update_fields = []
    if (
        getattr(need, "source_system", None) == NeedSourceSystem.CUSTOMER_DEMAND
        and next_status == NeedStatus.COVERED
        and getattr(need, "is_marketplace_published", False)
    ):
        publication_values_before = need_marketplace_audit_values(need)
        need.is_marketplace_published = False
        update_fields.append("is_marketplace_published")

    if need.status != next_status:
        need.status = next_status
        update_fields.append("status")
        status_changed = True

    if update_fields:
        if hasattr(need, "updated_at"):
            need.updated_at = timezone.now()
            update_fields.append("updated_at")
        need.save(update_fields=list(dict.fromkeys(update_fields)))

    if publication_values_before:
        log_audit_event(
            actor=acting_user,
            action="NEED_MARKETPLACE_UNPUBLISHED_AFTER_RECALCULATION",
            entity_type="needs",
            entity_id=need.id,
            notes="Procura retirada do marketplace porque ficou coberta.",
            old_values=publication_values_before,
            new_values=need_marketplace_audit_values(need),
        )

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
        log_audit_event(
            actor=acting_user,
            action="NEED_COVERAGE_CHANGED",
            entity_type="needs",
            entity_id=need.id,
            notes=f"Cobertura recalculada após alteração da encomenda #{getattr(order, 'order_number', order.id)}.",
            new_values={
                "order_id": str(order.id),
                "status": need.status,
                "required_quantity": audit_quantity(coverage["required_quantity"]),
                "planned_quantity": audit_quantity(coverage["planned_qty"]),
                "completed_quantity": audit_quantity(coverage["completed_qty"]),
            },
        )

    return results
