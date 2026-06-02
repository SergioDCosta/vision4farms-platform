"""Order domain services: notifications."""

import logging

from apps.orders.models import OrderStatus
from apps.orders.queries import is_order_from_need_response
from apps.orders.utils import _producer_display_name, _quantity_label, quantize_qty


logger = logging.getLogger(__name__)


def _sync_alerts_for_producers(*producers, acting_user=None):
    try:
        from apps.alerts.services import sync_alerts_for_producer
    except Exception:
        logger.exception("Falha ao carregar sincronização secundária de alertas.")
        return

    seen_ids = set()
    for producer in producers:
        producer_id = getattr(producer, "id", None)
        if not producer or producer_id in seen_ids:
            continue
        seen_ids.add(producer_id)
        try:
            sync_alerts_for_producer(producer, acting_user=acting_user)
        except Exception:
            logger.exception(
                "Falha ao sincronizar alertas após alteração de encomenda producer_id=%s.",
                producer_id,
            )
            continue


def _safe_emit_order_interaction_alert(
    *,
    target_producer,
    order,
    alert_type,
    title,
    description,
    counterpart_name,
    summary_label,
    action_url,
    acting_user,
):
    try:
        from apps.alerts.services import create_order_interaction_alert
    except Exception:
        logger.exception("Falha ao carregar criação secundária de alerta de encomenda.")
        return

    try:
        create_order_interaction_alert(
            target_producer=target_producer,
            order=order,
            alert_type=alert_type,
            title=title,
            description=description,
            counterpart_name=counterpart_name,
            summary_label=summary_label,
            action_url=action_url,
            acting_user=acting_user,
        )
    except Exception:
        logger.exception(
            "Falha ao criar alerta secundário de encomenda order_id=%s producer_id=%s.",
            getattr(order, "id", None),
            getattr(target_producer, "id", None),
        )
        return


def _safe_create_order_update_notification(*, target_producer, order, title, body, action_url):
    user = getattr(target_producer, "user", None)
    if not user:
        return

    try:
        from apps.notifications_app.services import create_order_update_notification
    except Exception:
        logger.exception("Falha ao carregar criação secundária de notificação de encomenda.")
        return

    try:
        create_order_update_notification(
            user=user,
            order=order,
            title=title,
            body=body,
            action_url=action_url,
        )
    except Exception:
        logger.exception(
            "Falha ao criar notificação secundária de encomenda order_id=%s producer_id=%s.",
            getattr(order, "id", None),
            getattr(target_producer, "id", None),
        )
        return


def _order_detail_url_for_alert(order, *, viewer_role):
    if viewer_role == "buyer":
        return f"/encomendas/{order.id}/?force_single=1"
    return f"/encomendas/{order.id}/"


def _build_order_alert_summary(order, *, seller_producer=None):
    all_items = list(
        order.items.select_related("product", "seller_producer", "seller_producer__user")
    )
    items = all_items
    if seller_producer:
        items = [item for item in all_items if item.seller_producer_id == seller_producer.id]

    if not items:
        return "sem itens"

    if len(items) == 1:
        item = items[0]
        quantity = quantize_qty(item.quantity or 0)
        unit = getattr(getattr(item, "product", None), "unit", "") or ""
        product_name = getattr(getattr(item, "product", None), "name", "") or "Produto"
        quantity_label = _quantity_label(quantity, unit)
        return f"{quantity_label} de {product_name}"

    return f"{len(items)} itens"


def _notify_order_purchase_created(*, order, buyer_producer, seller_producer, acting_user):
    try:
        from apps.alerts.models import AlertType
    except Exception:
        return

    counterpart_name = _producer_display_name(buyer_producer)
    summary_label = _build_order_alert_summary(order, seller_producer=seller_producer)
    is_need_response_order = is_order_from_need_response(order)
    _safe_emit_order_interaction_alert(
        target_producer=seller_producer,
        order=order,
        alert_type=AlertType.ORDER_REQUIRES_CONFIRMATION,
        title=(
            f"A sua oferta foi aceite na encomenda #{order.order_number}"
            if is_need_response_order
            else f"Nova encomenda #{order.order_number}"
        ),
        description=(
            f"{counterpart_name} aceitou a sua oferta privada para uma necessidade ({summary_label})."
            if is_need_response_order
            else f"{counterpart_name} criou uma nova encomenda ({summary_label})."
        ),
        counterpart_name=counterpart_name,
        summary_label=summary_label,
        action_url=_order_detail_url_for_alert(order, viewer_role="seller"),
        acting_user=acting_user,
    )


def _notify_order_status_changed_to_buyer(
    *,
    order,
    buyer_producer,
    seller_producer,
    status,
    acting_user,
):
    status_label = dict(OrderStatus.choices).get(status, str(status))
    counterpart_name = _producer_display_name(seller_producer)
    summary_label = _build_order_alert_summary(order, seller_producer=seller_producer)
    title = f"Encomenda #{order.order_number}: {status_label}"
    description = (
        f"{counterpart_name} atualizou a encomenda para "
        f"\"{status_label}\" ({summary_label})."
    )
    if status != OrderStatus.CANCELLED:
        _safe_create_order_update_notification(
            target_producer=buyer_producer,
            order=order,
            title=title,
            body=description,
            action_url=_order_detail_url_for_alert(order, viewer_role="buyer"),
        )
        return

    try:
        from apps.alerts.models import AlertType
    except Exception:
        return

    _safe_emit_order_interaction_alert(
        target_producer=buyer_producer,
        order=order,
        alert_type=AlertType.ORDER_CANCELLED,
        title=title,
        description=description,
        counterpart_name=counterpart_name,
        summary_label=summary_label,
        action_url=_order_detail_url_for_alert(order, viewer_role="buyer"),
        acting_user=acting_user,
    )


def _notify_order_completed_to_seller(*, order, buyer_producer, seller_producer, acting_user):
    counterpart_name = _producer_display_name(buyer_producer)
    summary_label = _build_order_alert_summary(order, seller_producer=seller_producer)
    _safe_create_order_update_notification(
        target_producer=seller_producer,
        order=order,
        title=f"Receção confirmada na encomenda #{order.order_number}",
        body=f"{counterpart_name} confirmou a receção ({summary_label}).",
        action_url=_order_detail_url_for_alert(order, viewer_role="seller"),
    )
