"""Order domain services: statuses."""

from apps.common.audit import log_audit_event
from apps.orders.models import OrderItem, OrderItemStatus, OrderStatus, OrderStatusHistory
from django.db import transaction
from django.utils import timezone
from apps.orders.utils import _order_audit_values


def _create_status_history(order, status, changed_by=None, notes=None):
    return OrderStatusHistory.objects.create(
        order=order,
        status=status,
        changed_by=changed_by,
        notes=notes or None,
    )


def _set_order_status(order, status):
    update_fields = ["status", "updated_at"]

    order.status = status
    order.updated_at = timezone.now()

    if status == OrderStatus.CONFIRMED and not order.confirmed_at:
        order.confirmed_at = timezone.now()
        update_fields.append("confirmed_at")

    if status == OrderStatus.COMPLETED and not order.completed_at:
        order.completed_at = timezone.now()
        update_fields.append("completed_at")

    if status == OrderStatus.CANCELLED and not order.cancelled_at:
        order.cancelled_at = timezone.now()
        update_fields.append("cancelled_at")

    order.save(update_fields=update_fields)


def compute_order_status_from_db(order_id, *, preferred_status=None, current_status=None):
    item_statuses = list(
        OrderItem.objects.filter(order_id=order_id).values_list("item_status", flat=True)
    )
    active_statuses = [
        item_status for item_status in item_statuses
        if item_status != OrderItemStatus.CANCELLED
    ]

    if not active_statuses:
        return OrderStatus.CANCELLED

    if all(item_status == OrderItemStatus.COMPLETED for item_status in active_statuses):
        return OrderStatus.COMPLETED

    has_in_delivery = any(item_status == OrderItemStatus.IN_DELIVERY for item_status in active_statuses)
    if has_in_delivery:
        return OrderStatus.DELIVERING

    has_confirmed = any(item_status == OrderItemStatus.CONFIRMED for item_status in active_statuses)

    should_keep_in_progress = (
        (preferred_status == OrderStatus.IN_PROGRESS or current_status == OrderStatus.IN_PROGRESS)
        and has_confirmed
        and not has_in_delivery
    )
    if should_keep_in_progress:
        return OrderStatus.IN_PROGRESS

    if has_confirmed:
        return OrderStatus.CONFIRMED

    return OrderStatus.PENDING


@transaction.atomic
def reconcile_order_status(order, *, expected_status=None, changed_by=None, notes=None):
    expected_status = expected_status or compute_order_status_from_db(
        order.id,
        current_status=order.status,
    )
    if order.status == expected_status:
        return False

    previous_status = order.status
    _set_order_status(order, expected_status)
    _create_status_history(
        order=order,
        status=expected_status,
        changed_by=changed_by,
        notes=notes or (
            "Reconciliação técnica automática: "
            f"{previous_status} -> {expected_status}."
        ),
    )
    return True


def _recalculate_order_status(order, preferred_status=None):
    resolved_status = compute_order_status_from_db(
        order_id=order.id,
        preferred_status=preferred_status,
        current_status=order.status,
    )
    _set_order_status(order, resolved_status)
    return order


def _log_order_status_change(order, *, previous_status, acting_user, notes, cancelled=False):
    if previous_status == order.status:
        return
    values_before = {"status": previous_status}
    values_after = _order_audit_values(order)
    log_audit_event(
        actor=acting_user,
        action="ORDER_STATUS_CHANGED",
        entity_type="orders",
        entity_id=order.id,
        notes=notes,
        old_values=values_before,
        new_values=values_after,
    )
    if cancelled:
        log_audit_event(
            actor=acting_user,
            action="ORDER_CANCELLED",
            entity_type="orders",
            entity_id=order.id,
            notes=notes,
            old_values=values_before,
            new_values=values_after,
        )
