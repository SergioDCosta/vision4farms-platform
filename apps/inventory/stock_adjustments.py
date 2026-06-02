from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.common.audit import log_audit_event
from apps.inventory.audit import _log_stock_movement, _stock_audit_values
from apps.inventory.constants import ZERO
from apps.inventory.models import StockMovement
from apps.inventory.utils import format_qty as _format_qty
from apps.orders.models import Order, OrderStatus, OrderStatusHistory


def get_stock_movements(stock, limit=20):
    return (
        StockMovement.objects
        .filter(stock=stock)
        .select_related("performed_by")
        .order_by("-created_at")[:limit]
    )

def _user_display_name(user):
    if not user:
        return "Sistema"

    full_name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
    return full_name or getattr(user, "email", "Sistema")


def _split_note_order_token(notes, order_number):
    note_text = notes or "—"
    if not order_number:
        return {
            "notes": note_text,
            "note_prefix": note_text,
            "note_token": None,
            "note_suffix": "",
        }

    token = f"#{order_number}"
    if token not in note_text:
        return {
            "notes": note_text,
            "note_prefix": note_text,
            "note_token": None,
            "note_suffix": "",
        }

    prefix, suffix = note_text.split(token, 1)
    return {
        "notes": note_text,
        "note_prefix": prefix,
        "note_token": token,
        "note_suffix": suffix,
    }


def get_stock_activity_feed(stock, limit=20):
    feed = []

    movements = (
        StockMovement.objects
        .filter(stock=stock)
        .select_related("performed_by")
        .order_by("-created_at")[:limit]
    )
    movement_order_ids = {
        str(mv.reference_id)
        for mv in movements
        if mv.reference_type == "ORDER" and mv.reference_id
    }
    movement_orders_by_id = {
        str(order.id): order
        for order in Order.objects.filter(id__in=movement_order_ids).only("id", "order_number")
    }

    for mv in movements:
        linked_order = None
        if mv.reference_type == "ORDER" and mv.reference_id:
            linked_order = movement_orders_by_id.get(str(mv.reference_id))
        linked_order_id = str(linked_order.id) if linked_order else None
        linked_order_number = linked_order.order_number if linked_order else None
        note_parts = _split_note_order_token(mv.notes, linked_order_number)

        delta = Decimal(str(mv.quantity_delta or 0))
        if delta > 0:
            impact_label = f"+{_format_qty(delta)} {stock.product.unit}"
            impact_class = "is-positive"
        elif delta < 0:
            impact_label = f"{_format_qty(delta)} {stock.product.unit}"
            impact_class = "is-negative"
        else:
            impact_label = "Sem impacto direto"
            impact_class = "is-neutral"

        type_label = (
            "Entrega a cliente externo"
            if mv.reference_type == "EXTERNAL_DEMAND"
            else mv.get_movement_type_display()
        )
        feed.append({
            "created_at": mv.created_at,
            "type_label": type_label,
            "impact_label": impact_label,
            "impact_class": impact_class,
            "notes": note_parts["notes"],
            "note_prefix": note_parts["note_prefix"],
            "note_token": note_parts["note_token"],
            "note_suffix": note_parts["note_suffix"],
            "order_id": linked_order_id,
            "order_number": linked_order_number,
            "actor_name": _user_display_name(mv.performed_by),
            "source": "movement",
        })

    history_qs = (
        OrderStatusHistory.objects
        .filter(
            order__items__seller_producer=stock.producer,
            order__items__product=stock.product,
        )
        .select_related("changed_by", "order")
        .prefetch_related("order__items__listing")
        .order_by("-created_at")
    )

    seen_ids = set()

    for event in history_qs:
        if event.id in seen_ids:
            continue
        seen_ids.add(event.id)

        related_items = [
            item for item in event.order.items.all()
            if (
                item.seller_producer_id == stock.producer_id
                and item.product_id == stock.product_id
                and getattr(getattr(item, "listing", None), "stock_id", None) == stock.id
            )
        ]
        if not related_items:
            continue

        qty = sum(Decimal(str(item.quantity or 0)) for item in related_items)
        qty = qty.quantize(Decimal("0.001"))

        if event.status == OrderStatus.PENDING:
            impact_label = f"{_format_qty(qty)} {stock.product.unit} solicitados"
            impact_class = "is-neutral"
        elif event.status == OrderStatus.CONFIRMED:
            impact_label = f"+{_format_qty(qty)} {stock.product.unit} reservados"
            impact_class = "is-warning"
        elif event.status == OrderStatus.IN_PROGRESS:
            impact_label = "Pedido em preparação"
            impact_class = "is-info"
        elif event.status == OrderStatus.DELIVERING:
            impact_label = "Pedido em entrega"
            impact_class = "is-info"
        elif event.status == OrderStatus.COMPLETED:
            impact_label = f"-{_format_qty(qty)} {stock.product.unit} debitados"
            impact_class = "is-negative"
        elif event.status == OrderStatus.CANCELLED:
            had_reservation_before = event.order.status_history.filter(
                created_at__lt=event.created_at,
                status__in=[OrderStatus.CONFIRMED, OrderStatus.IN_PROGRESS, OrderStatus.DELIVERING],
            ).exists()

            if had_reservation_before:
                impact_label = f"-{_format_qty(qty)} {stock.product.unit} reserva libertada"
            else:
                impact_label = "Pedido cancelado sem reserva"
            impact_class = "is-neutral"
        else:
            impact_label = "Sem impacto direto"
            impact_class = "is-neutral"

        note_parts = _split_note_order_token(event.notes, event.order.order_number)

        feed.append({
            "created_at": event.created_at,
            "type_label": f"Encomenda #{event.order.order_number} — {event.get_status_display()}",
            "impact_label": impact_label,
            "impact_class": impact_class,
            "notes": note_parts["notes"],
            "note_prefix": note_parts["note_prefix"],
            "note_token": note_parts["note_token"],
            "note_suffix": note_parts["note_suffix"],
            "order_id": str(event.order.id),
            "order_number": event.order.order_number,
            "actor_name": _user_display_name(event.changed_by),
            "source": "order",
        })

    feed.sort(key=lambda item: item["created_at"], reverse=True)
    return feed[:limit]

class ListingsBlockStockReductionError(ValidationError):
    """Raised when a stock reduction would leave active listings without coverage."""

    def __init__(self, blocking):
        self.blocking = blocking
        super().__init__(
            "A nova quantidade não chega para cobrir os anúncios ativos deste produto."
        )


def get_listings_blocking_stock_decrease(stock, new_quantity):
    from apps.marketplace.reconciliation import reconcile_listings_for_stock_reduction

    return reconcile_listings_for_stock_reduction(stock, new_quantity, mode="inspect")


def reduce_listings_to_fit_stock(
    stock,
    new_quantity,
    *,
    mode,
    listing_ids_to_cancel=None,
    acting_user=None,
):
    from apps.marketplace.reconciliation import reconcile_listings_for_stock_reduction

    return reconcile_listings_for_stock_reduction(
        stock,
        new_quantity,
        mode=mode,
        listing_ids_to_cancel=listing_ids_to_cancel,
        acting_user=acting_user,
    )


@transaction.atomic
def update_stock(
    stock,
    new_quantity,
    safety_stock,
    movement_type,
    user,
    notes="",
    *,
    allow_listing_reconciliation=False,
):
    new_quantity = new_quantity or ZERO
    safety_stock = safety_stock or ZERO

    if new_quantity < ZERO:
        raise ValidationError("A quantidade não pode ser negativa.")

    if new_quantity < stock.reserved_quantity:
        raise ValidationError(
            (
                "A nova quantidade não pode ser inferior à quantidade reservada. "
                f"Atualmente tens {stock.reserved_quantity} reservada."
            )
        )

    if (
        not allow_listing_reconciliation
        and new_quantity < Decimal(str(stock.current_quantity or 0))
    ):
        blocking = get_listings_blocking_stock_decrease(stock, new_quantity)
        if blocking["deficit"] > ZERO:
            raise ListingsBlockStockReductionError(blocking)

    quantity_delta = new_quantity - stock.current_quantity

    threshold_changed = safety_stock != stock.safety_stock

    if quantity_delta == ZERO and not threshold_changed:
        raise ValidationError("Não foi detetada nenhuma alteração no stock.")

    previous_values = _stock_audit_values(stock)
    stock.current_quantity = new_quantity
    stock.safety_stock = safety_stock
    stock.updated_by = user
    stock.last_updated_at = timezone.now()
    update_fields = [
        "current_quantity",
        "safety_stock",
        "updated_by",
        "last_updated_at",
        "updated_at",
    ]
    stock.save(update_fields=update_fields)
    log_audit_event(
        actor=user,
        action="STOCK_UPDATED",
        entity_type="stocks",
        entity_id=stock.id,
        notes="Quantidade de stock atualizada manualmente.",
        old_values=previous_values,
        new_values=_stock_audit_values(stock),
    )

    movement = None
    if quantity_delta != ZERO:
        movement = StockMovement.objects.create(
            stock=stock,
            movement_type=movement_type,
            quantity_delta=quantity_delta,
            notes=notes or None,
            performed_by=user,
        )
        _log_stock_movement(movement, actor=user)

    return movement
