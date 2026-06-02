"""Alert domain services: events."""

from apps.alerts.models import Alert, AlertCategory, AlertEvent, AlertEventType, AlertSeverity, AlertSourceSystem, AlertStatus, AlertType
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from apps.alerts.badges import _queue_alerts_badge_changed_for_user
from apps.alerts.delivery import _queue_alert_delivery_for_producer


def record_alert_event(alert, event_type, performed_by=None, notes=None):
    return AlertEvent.objects.create(
        alert=alert,
        event_type=event_type,
        performed_by=performed_by,
        notes=notes or None,
    )


@transaction.atomic
def create_order_interaction_alert(
    *,
    target_producer,
    order,
    alert_type,
    title,
    description,
    counterpart_name,
    summary_label,
    action_url,
    action_label="Ir para encomenda",
    acting_user=None,
):
    first_item = (
        order.items
        .select_related("product", "listing")
        .order_by("created_at")
        .first()
    )
    payload = {
        "order_id": str(order.id),
        "order_number": order.order_number,
        "order_status": order.status,
        "order_status_label": order.get_status_display(),
        "counterpart_name": counterpart_name or "Contraparte",
        "summary": summary_label or "",
        "action_url": action_url,
        "action_label": action_label,
        "secondary_action_url": f"/mensagens/encomenda/{order.id}/iniciar/",
        "secondary_action_label": "Ir para conversa",
    }
    if first_item and first_item.product_id:
        payload["product_name"] = first_item.product.name

    category = AlertCategory.ORDERS
    context_key = f"{alert_type}:order:{order.id}"
    requires_action = alert_type in {
        AlertType.ORDER_PURCHASE_CREATED,
        AlertType.ORDER_REQUIRES_CONFIRMATION,
        AlertType.ORDER_DELIVERY_OVERDUE,
    }
    priority = 25 if requires_action else 70
    severity = (
        AlertSeverity.WARNING
        if alert_type in {AlertType.ORDER_CANCELLED, AlertType.ORDER_DELIVERY_OVERDUE}
        else AlertSeverity.INFO
    )
    alert, created = Alert.objects.select_for_update().get_or_create(
        producer=target_producer,
        type=alert_type,
        context_key=context_key,
        cleared_at__isnull=True,
        defaults={
            "product": getattr(first_item, "product", None),
            "listing": getattr(first_item, "listing", None),
            "need": None,
            "forecast": None,
            "severity": severity,
            "category": category,
            "title": title,
            "description": description,
            "source_system": AlertSourceSystem.INTERNAL,
            "status": AlertStatus.ACTIVE,
            "payload": payload,
            "assumed_loss": False,
            "requires_action": requires_action,
            "priority": priority,
        },
    )
    if not created:
        alert.product = getattr(first_item, "product", None)
        alert.listing = getattr(first_item, "listing", None)
        alert.need = None
        alert.forecast = None
        alert.severity = severity
        alert.category = category
        alert.title = title
        alert.description = description
        alert.source_system = AlertSourceSystem.INTERNAL
        alert.status = AlertStatus.ACTIVE
        alert.payload = payload
        alert.requires_action = requires_action
        alert.priority = priority
        alert.updated_at = timezone.now()
        alert.save(
            update_fields=[
                "product",
                "listing",
                "need",
                "forecast",
                "severity",
                "category",
                "title",
                "description",
                "source_system",
                "status",
                "payload",
                "requires_action",
                "priority",
                "updated_at",
            ]
        )
    if created:
        record_alert_event(
            alert,
            AlertEventType.CREATED,
            performed_by=acting_user,
            notes="Alerta de encomenda criado automaticamente.",
        )
        _queue_alert_delivery_for_producer(alert=alert, producer=target_producer)
    _queue_alerts_badge_changed_for_user(user_id=target_producer.user_id)
    return alert


@transaction.atomic
def create_need_response_event_alert(
    *,
    target_producer,
    listing,
    alert_type,
    title,
    description,
    action_url,
    action_label,
    acting_user=None,
    severity=AlertSeverity.INFO,
    requires_action=False,
):
    if not target_producer or not listing:
        return None

    product = getattr(listing, "product", None)
    need = getattr(listing, "need", None)
    producer_name = getattr(getattr(listing, "producer", None), "display_name", None)
    payload = {
        "listing_id": str(listing.id),
        "need_id": str(getattr(listing, "need_id", "") or ""),
        "product_name": getattr(product, "name", "") or "",
        "counterpart_name": producer_name or "Produtor",
        "quantity_available": str(getattr(listing, "quantity_available", "") or ""),
        "unit_price": str(getattr(listing, "unit_price", "") or ""),
        "action_url": action_url,
        "action_label": action_label,
    }

    alert, created = Alert.objects.select_for_update().get_or_create(
        producer=target_producer,
        type=alert_type,
        context_key=f"{alert_type}:listing:{listing.id}",
        cleared_at__isnull=True,
        defaults={
            "product": product,
            "listing": listing,
            "need": need,
            "forecast": getattr(listing, "forecast", None),
            "severity": severity,
            "category": AlertCategory.NEEDS,
            "title": title,
            "description": description,
            "source_system": AlertSourceSystem.INTERNAL,
            "status": AlertStatus.ACTIVE,
            "payload": payload,
            "assumed_loss": False,
            "requires_action": requires_action,
            "priority": 20 if requires_action else 65,
        },
    )
    if not created:
        alert.title = title
        alert.description = description
        alert.payload = payload
        alert.status = AlertStatus.ACTIVE
        alert.updated_at = timezone.now()
        alert.save(update_fields=["title", "description", "payload", "status", "updated_at"])
    if created:
        record_alert_event(
            alert,
            AlertEventType.CREATED,
            performed_by=acting_user,
            notes="Alerta de resposta a necessidade criado automaticamente.",
        )
        _queue_alert_delivery_for_producer(alert=alert, producer=target_producer)
    _queue_alerts_badge_changed_for_user(user_id=target_producer.user_id)
    return alert


@transaction.atomic
def upsert_message_unread_alert(
    *,
    target_producer,
    conversation_id,
    conversation_type,
    sender_name,
    preview_text,
    action_url,
    acting_user=None,
):
    if not target_producer or not conversation_id:
        return None

    now = timezone.now()
    sender_label = (sender_name or "Utilizador").strip() or "Utilizador"
    preview_label = (preview_text or "").strip()
    title = f"Nova mensagem de {sender_label}"
    description = preview_label or "Tens uma nova mensagem por ler."

    payload = {
        "conversation_id": str(conversation_id),
        "conversation_type": str(conversation_type or "").strip() or "DIRECT",
        "sender_name": sender_label,
        "preview": preview_label,
        "action_url": action_url,
        "action_label": "Ir para conversa",
    }
    context_key = f"{AlertType.MESSAGE_UNREAD}:conversation:{conversation_id}"

    alert = (
        Alert.objects
        .select_for_update()
        .filter(
            producer=target_producer,
            type=AlertType.MESSAGE_UNREAD,
            status=AlertStatus.ACTIVE,
        )
        .filter(Q(context_key=context_key) | Q(payload__conversation_id=str(conversation_id)))
        .order_by("-updated_at", "-created_at")
        .first()
    )

    if alert:
        changed = False
        if alert.title != title:
            alert.title = title
            changed = True
        if alert.description != description:
            alert.description = description
            changed = True
        if alert.payload != payload:
            alert.payload = payload
            changed = True
        if alert.severity != AlertSeverity.INFO:
            alert.severity = AlertSeverity.INFO
            changed = True
        if alert.source_system != AlertSourceSystem.INTERNAL:
            alert.source_system = AlertSourceSystem.INTERNAL
            changed = True
        if alert.category != AlertCategory.MESSAGES:
            alert.category = AlertCategory.MESSAGES
            changed = True
        if alert.context_key != context_key:
            alert.context_key = context_key
            changed = True
        if alert.requires_action:
            alert.requires_action = False
            changed = True
        if alert.priority != 80:
            alert.priority = 80
            changed = True
        if alert.updated_at != now:
            alert.updated_at = now
            changed = True
        if changed:
            alert.save(
                update_fields=[
                    "title",
                    "description",
                    "payload",
                    "severity",
                    "source_system",
                    "category",
                    "context_key",
                    "requires_action",
                    "priority",
                    "updated_at",
                ]
            )
        _queue_alerts_badge_changed_for_user(user_id=target_producer.user_id)
        return alert

    alert = Alert.objects.create(
        producer=target_producer,
        type=AlertType.MESSAGE_UNREAD,
        severity=AlertSeverity.INFO,
        category=AlertCategory.MESSAGES,
        context_key=context_key,
        title=title,
        description=description,
        source_system=AlertSourceSystem.INTERNAL,
        status=AlertStatus.ACTIVE,
        payload=payload,
        assumed_loss=False,
        requires_action=False,
        priority=80,
    )
    record_alert_event(
        alert,
        AlertEventType.CREATED,
        performed_by=acting_user,
        notes="Alerta de nova mensagem criado automaticamente.",
    )
    _queue_alerts_badge_changed_for_user(user_id=target_producer.user_id)
    return alert


@transaction.atomic
def resolve_message_unread_alert(
    *,
    target_producer,
    conversation_id,
    acting_user=None,
):
    if not target_producer or not conversation_id:
        return False

    now = timezone.now()
    alert = (
        Alert.objects
        .select_for_update()
        .filter(
            producer=target_producer,
            type=AlertType.MESSAGE_UNREAD,
            status=AlertStatus.ACTIVE,
        )
        .filter(Q(context_key=f"{AlertType.MESSAGE_UNREAD}:conversation:{conversation_id}") | Q(payload__conversation_id=str(conversation_id)))
        .order_by("-updated_at", "-created_at")
        .first()
    )
    if not alert:
        return False

    alert.status = AlertStatus.RESOLVED
    alert.cleared_at = now
    alert.updated_at = now
    alert.save(update_fields=["status", "cleared_at", "updated_at"])
    record_alert_event(
        alert,
        AlertEventType.RESOLVED,
        performed_by=acting_user,
        notes="Alerta de mensagem resolvido ao ler conversa.",
    )
    _queue_alerts_badge_changed_for_user(user_id=target_producer.user_id)
    return True
