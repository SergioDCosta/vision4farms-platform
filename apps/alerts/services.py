import logging
import threading
from datetime import timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import Count, Max
from django.db.models import Q
from django.db import transaction
from django.template.loader import render_to_string
from django.utils.dateparse import parse_datetime
from django.utils import formats, timezone

from apps.accounts.models import UserRole
from apps.alerts.models import (
    Alert,
    AlertCategory,
    AlertDelivery,
    AlertDeliveryChannel,
    AlertDeliveryStatus,
    AlertEvent,
    AlertEventType,
    AlertSeverity,
    AlertSourceSystem,
    AlertStatus,
    AlertType,
)
from apps.inventory.models import ProducerProfile, ProductionForecast, Stock
from apps.inventory.services import calculate_inventory_commitment_state
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.needs.models import (
    Need,
    NeedResponseStatus,
    NeedSourceSystem,
    NeedStatus,
)
from apps.needs.services import calculate_need_coverage
from apps.orders.models import Order, OrderItem, OrderItemStatus, OrderStatus
from apps.marketplace.services import get_forecast_available_quantity
from apps.common.formatting import format_quantity


ACTIVE_LIKE_ALERT_STATUSES = [AlertStatus.ACTIVE, AlertStatus.READ]
UI_TAB_STATUS_MAP = {
    "active": AlertStatus.ACTIVE,
    "ignored": AlertStatus.IGNORED,
    "resolved": AlertStatus.RESOLVED,
}
MANAGED_ALERT_TYPES = {
    AlertType.CRITICAL_STOCK,
    AlertType.SURPLUS_AVAILABLE,
    AlertType.EXTERNAL_DEFICIT,
    AlertType.NEED_UNDERCOVERED,
    AlertType.NEED_RESPONSE_RECEIVED,
    AlertType.NEED_DEADLINE_APPROACHING,
    AlertType.BUY_OPPORTUNITY,
    AlertType.SELL_SUGGESTION,
    AlertType.ORDER_REQUIRES_CONFIRMATION,
    AlertType.ORDER_DELIVERY_OVERDUE,
    AlertType.LISTING_EXPIRING_SOON,
}
ORDER_ALERT_TYPES = {
    AlertType.ORDER_PURCHASE_CREATED,
    AlertType.ORDER_REQUIRES_CONFIRMATION,
    AlertType.ORDER_CONFIRMED,
    AlertType.ORDER_IN_PROGRESS,
    AlertType.ORDER_DELIVERING,
    AlertType.ORDER_DELIVERY_OVERDUE,
    AlertType.ORDER_CANCELLED,
    AlertType.ORDER_COMPLETED,
}
AUTO_RESOLVED_NOTE = "Resolução automática por fim da condição"
ALERTS_LAST_SEEN_SESSION_KEY = "alerts_last_seen_at"
ALERTS_BADGE_GROUP_PREFIX = "alerts_badge_user_"
IGNORED_ALERT_TTL = timedelta(minutes=30)
INTERACTIVE_ALERT_STATUSES = {AlertStatus.ACTIVE, AlertStatus.READ}
SNOOZE_OPTIONS = {
    "1h": ("1 hora", timedelta(hours=1)),
    "tomorrow": ("amanhã", timedelta(days=1)),
    "1w": ("1 semana", timedelta(days=7)),
}
DEFAULT_SNOOZE_KEY = "1h"
NEED_DEADLINE_WINDOW = timedelta(days=7)
LISTING_EXPIRING_WINDOW = timedelta(days=3)
ORDER_CONFIRMATION_GRACE = timedelta(hours=24)
ORDER_DELIVERY_GRACE = timedelta(days=3)


logger = logging.getLogger(__name__)


EMAIL_ALERT_TYPES = {
    AlertType.CRITICAL_STOCK,
    AlertType.NEED_RESPONSE_RECEIVED,
    AlertType.NEED_DEADLINE_APPROACHING,
    AlertType.NEED_UNDERCOVERED,
    AlertType.ORDER_REQUIRES_CONFIRMATION,
    AlertType.ORDER_DELIVERY_OVERDUE,
}


def _parse_session_datetime(value):
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if not parsed:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def get_alerts_badge_group_name(user_id):
    return f"{ALERTS_BADGE_GROUP_PREFIX}{user_id}"


def broadcast_alerts_badge_changed_for_user(*, user_id):
    if not user_id:
        return
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            get_alerts_badge_group_name(user_id),
            {"type": "alerts_badge_changed"},
        )
    except Exception:
        logger.exception("Falha ao emitir atualização realtime do badge de alertas.")


def _queue_alerts_badge_changed_for_user(*, user_id):
    transaction.on_commit(
        lambda: broadcast_alerts_badge_changed_for_user(user_id=user_id)
    )


def get_client_alerts_badge_state(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.CLIENTE:
        return {"visible": False, "count": 0, "tone": "orange"}

    producer = ProducerProfile.objects.filter(user=user).only("id").first()
    if not producer:
        return {"visible": False, "count": 0, "tone": "orange"}

    aggregate = (
        Alert.objects
        .filter(producer=producer, status=AlertStatus.ACTIVE)
        .aggregate(
            open_count=Count("id"),
            latest_active_created_at=Max("created_at"),
        )
    )
    open_count = int(aggregate.get("open_count") or 0)
    if open_count <= 0:
        return {"visible": False, "count": 0, "tone": "orange"}

    latest_active_created_at = aggregate.get("latest_active_created_at")
    last_seen_at = _parse_session_datetime(
        request.session.get(ALERTS_LAST_SEEN_SESSION_KEY)
    )
    has_unseen_new = bool(
        latest_active_created_at and (
            not last_seen_at or latest_active_created_at > last_seen_at
        )
    )

    return {
        "visible": True,
        "count": open_count,
        "tone": "red" if has_unseen_new else "orange",
    }


def mark_client_alerts_seen(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.CLIENTE:
        return
    request.session[ALERTS_LAST_SEEN_SESSION_KEY] = timezone.now().isoformat()
    request.session.modified = True
    _queue_alerts_badge_changed_for_user(user_id=user.id)


def _as_decimal(value, default="0.000"):
    return Decimal(str(value if value is not None else default))


def _format_alert_quantity(value):
    return format_quantity(value)


def _quantity_label(value, unit):
    unit_label = (unit or "").strip()
    quantity = _format_alert_quantity(value)
    return f"{quantity} {unit_label}".strip()


def _money_label(value):
    decimal_value = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    amount = formats.number_format(
        decimal_value,
        decimal_pos=2,
        use_l10n=True,
        force_grouping=False,
    )
    return f"{amount} €"


def get_alert_type_label(alert_type):
    labels = {
        AlertType.CRITICAL_STOCK: "Stock crítico",
        AlertType.SURPLUS_AVAILABLE: "Excedente / oportunidade de venda",
        AlertType.BUY_OPPORTUNITY: "Oportunidade de compra",
        AlertType.EXTERNAL_DEFICIT: "Necessidade sem cobertura suficiente",
        AlertType.NEED_UNDERCOVERED: "Necessidade por cobrir",
        AlertType.NEED_RESPONSE_RECEIVED: "Oferta recebida",
        AlertType.NEED_DEADLINE_APPROACHING: "Prazo de necessidade próximo",
        AlertType.OFFER_REJECTED: "Oferta rejeitada",
        AlertType.SELL_SUGGESTION: "Pré-venda disponível para publicar",
        AlertType.ORDER_REQUIRES_CONFIRMATION: "Encomenda por confirmar",
        AlertType.ORDER_PURCHASE_CREATED: "Nova compra recebida",
        AlertType.ORDER_CONFIRMED: "Encomenda confirmada",
        AlertType.ORDER_IN_PROGRESS: "Encomenda em preparação",
        AlertType.ORDER_DELIVERING: "Encomenda em entrega",
        AlertType.ORDER_DELIVERY_OVERDUE: "Entrega atrasada",
        AlertType.ORDER_CANCELLED: "Encomenda cancelada",
        AlertType.ORDER_COMPLETED: "Receção confirmada",
        AlertType.LISTING_EXPIRING_SOON: "Anúncio a expirar",
        AlertType.MESSAGE_UNREAD: "Nova mensagem",
    }
    return labels.get(str(alert_type), str(alert_type))


def get_alert_category_label(category):
    labels = dict(AlertCategory.choices)
    return labels.get(str(category), str(category))


def normalize_alert_type(raw_type):
    alert_type = (raw_type or "").strip()
    valid_types = {value for value, _label in AlertType.choices}
    return alert_type if alert_type in valid_types else ""


def normalize_alert_category(raw_category):
    category = (raw_category or "").strip()
    valid_categories = {value for value, _label in AlertCategory.choices}
    return category if category in valid_categories else ""


def _build_context_key(alert_type, *, product_id=None, need_id=None, forecast_id=None, listing_id=None):
    if need_id:
        return f"{alert_type}:need:{need_id}"
    if forecast_id:
        return f"{alert_type}:forecast:{forecast_id}"
    if listing_id:
        return f"{alert_type}:listing:{listing_id}"
    if product_id:
        return f"{alert_type}:product:{product_id}"
    return f"{alert_type}:global"


def _alert_context_key(alert):
    if getattr(alert, "context_key", None):
        return alert.context_key
    return _build_context_key(
        alert.type,
        product_id=getattr(alert, "product_id", None),
        need_id=getattr(alert, "need_id", None),
        forecast_id=getattr(alert, "forecast_id", None),
        listing_id=getattr(alert, "listing_id", None),
    )


def _candidate(
    *,
    alert_type,
    severity,
    category,
    title,
    description,
    payload,
    product=None,
    need=None,
    forecast=None,
    listing=None,
    context_key=None,
    requires_action=False,
    due_at=None,
    expires_at=None,
    priority=50,
):
    return {
        "key": context_key or _build_context_key(
            alert_type,
            product_id=getattr(product, "id", None),
            need_id=getattr(need, "id", None),
            forecast_id=getattr(forecast, "id", None),
            listing_id=getattr(listing, "id", None),
        ),
        "type": alert_type,
        "severity": severity,
        "category": category,
        "product": product,
        "need": need,
        "forecast": forecast,
        "listing": listing,
        "title": title,
        "description": description,
        "payload": payload,
        "requires_action": requires_action,
        "due_at": due_at,
        "expires_at": expires_at,
        "priority": priority,
    }


def _get_snooze_option(raw_key):
    key = (raw_key or DEFAULT_SNOOZE_KEY).strip()
    if key not in SNOOZE_OPTIONS:
        key = DEFAULT_SNOOZE_KEY
    label, delta = SNOOZE_OPTIONS[key]
    return key, label, delta


def record_alert_event(alert, event_type, performed_by=None, notes=None):
    return AlertEvent.objects.create(
        alert=alert,
        event_type=event_type,
        performed_by=performed_by,
        notes=notes or None,
    )


def _get_user_alert_preferences(user):
    preferences = getattr(user, "preferences", None)
    if preferences is None:
        try:
            preferences = user.preferences
        except Exception:
            preferences = None
    return preferences


def _user_wants_in_app_alerts(user):
    preferences = _get_user_alert_preferences(user)
    return True if preferences is None else bool(getattr(preferences, "alerts_in_app", True))


def _user_wants_email_alerts(user):
    preferences = _get_user_alert_preferences(user)
    return bool(preferences and getattr(preferences, "alerts_email", False))


def _user_wants_sms_alerts(user):
    preferences = _get_user_alert_preferences(user)
    return bool(preferences and getattr(preferences, "alerts_sms", False))


def _should_email_alert(alert):
    return (
        getattr(alert, "severity", None) == AlertSeverity.CRITICAL
        or bool(getattr(alert, "requires_action", False))
        or getattr(alert, "type", None) in EMAIL_ALERT_TYPES
    )


def _record_alert_delivery(*, alert, user, channel, status, error=None, sent_at=None):
    try:
        return AlertDelivery.objects.create(
            alert=alert,
            user=user,
            channel=channel,
            status=status,
            error=(error or "")[:1000] or None,
            sent_at=sent_at,
        )
    except Exception:
        logger.exception("Falha ao registar entrega de alerta alert_id=%s channel=%s", getattr(alert, "id", None), channel)
        return None


def _send_alert_email_now(*, alert, user):
    payload = alert.payload or {}
    context = {
        "alert": alert,
        "user": user,
        "reason": payload.get("reason"),
        "action_url": payload.get("action_url"),
    }
    subject = render_to_string("emails/alert_delivery_subject.txt", context).strip()
    text_body = render_to_string("emails/alert_delivery.txt", context)
    html_body = render_to_string("emails/alert_delivery.html", context)
    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)


def _send_alert_email_safely(*, alert, user):
    try:
        _send_alert_email_now(alert=alert, user=user)
    except Exception as exc:
        _record_alert_delivery(
            alert=alert,
            user=user,
            channel=AlertDeliveryChannel.EMAIL,
            status=AlertDeliveryStatus.FAILED,
            error=str(exc),
        )
        logger.exception("Falha no envio de alerta por email alert_id=%s user_id=%s", alert.id, user.id)
        return

    _record_alert_delivery(
        alert=alert,
        user=user,
        channel=AlertDeliveryChannel.EMAIL,
        status=AlertDeliveryStatus.SENT,
        sent_at=timezone.now(),
    )


def _queue_alert_email_delivery(*, alert, user):
    transaction.on_commit(
        lambda: threading.Thread(
            target=_send_alert_email_safely,
            kwargs={"alert": alert, "user": user},
        ).start()
    )


def deliver_alert_to_user(*, alert, user):
    if not alert or not user:
        return

    if _user_wants_in_app_alerts(user):
        try:
            from apps.notifications_app.services import create_alert_notification

            create_alert_notification(user=user, alert=alert)
            _record_alert_delivery(
                alert=alert,
                user=user,
                channel=AlertDeliveryChannel.IN_APP,
                status=AlertDeliveryStatus.SENT,
                sent_at=timezone.now(),
            )
        except Exception:
            _record_alert_delivery(
                alert=alert,
                user=user,
                channel=AlertDeliveryChannel.IN_APP,
                status=AlertDeliveryStatus.FAILED,
                error="Falha ao criar notificação in-app.",
            )
    else:
        _record_alert_delivery(
            alert=alert,
            user=user,
            channel=AlertDeliveryChannel.IN_APP,
            status=AlertDeliveryStatus.SKIPPED,
            error="Utilizador desativou alertas na app.",
        )

    if _should_email_alert(alert):
        if _user_wants_email_alerts(user) and getattr(user, "email", None):
            _queue_alert_email_delivery(alert=alert, user=user)
        else:
            _record_alert_delivery(
                alert=alert,
                user=user,
                channel=AlertDeliveryChannel.EMAIL,
                status=AlertDeliveryStatus.SKIPPED,
                error="Email desativado ou indisponível.",
            )

    if _user_wants_sms_alerts(user):
        _record_alert_delivery(
            alert=alert,
            user=user,
            channel=AlertDeliveryChannel.SMS,
            status=AlertDeliveryStatus.SKIPPED,
            error="Fornecedor SMS não configurado.",
        )


def _queue_alert_delivery_for_producer(*, alert, producer):
    user = getattr(producer, "user", None)
    if not user and getattr(producer, "user_id", None):
        try:
            user = producer.user
        except Exception:
            user = None
    if not user:
        return
    transaction.on_commit(lambda: deliver_alert_to_user(alert=alert, user=user))


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


def _critical_stock_candidates(producer):
    rows = []
    stocks = (
        Stock.objects
        .select_related("product")
        .filter(
            producer=producer,
            product__is_active=True,
            product__producer_links__producer=producer,
            product__producer_links__is_active=True,
        )
        .distinct()
    )

    for stock in stocks:
        commitment_state = calculate_inventory_commitment_state(
            producer,
            stock.product,
            stock=stock,
        )
        if commitment_state.get("max_deficit", Decimal("0.000")) <= Decimal("0.000"):
            continue

        unit = getattr(stock.product, "unit", "") or ""
        available_label = _quantity_label(commitment_state.get("available_stock_now"), unit)
        forecast_label = _quantity_label(commitment_state.get("useful_forecast_total"), unit)
        safety_label = _quantity_label(commitment_state.get("total_external_demand"), unit)
        deficit_label = _quantity_label(commitment_state.get("max_deficit"), unit)
        first_external_deadline = commitment_state.get("first_deficit_date")
        deadline_label = (
            formats.date_format(first_external_deadline, "SHORT_DATE_FORMAT")
            if first_external_deadline
            else None
        )
        rows.append(
            _candidate(
                alert_type=AlertType.CRITICAL_STOCK,
                severity=AlertSeverity.CRITICAL,
                category=AlertCategory.STOCK,
                product=stock.product,
                title=f"Compromissos externos em risco: {stock.product.name}",
                description=(
                    f"Faltam {deficit_label} para cumprir pedidos externos"
                    + (f" até {deadline_label}." if deadline_label else ".")
                    + f" Disponível: {available_label} · Previsão útil: {forecast_label} · Necessário: {safety_label}."
                ),
                payload={
                    "available_quantity": str(commitment_state.get("available_stock_now")),
                    "useful_forecast_total": str(commitment_state.get("useful_forecast_total")),
                    "safety_stock": str(commitment_state.get("total_external_demand")),
                    "max_deficit": str(commitment_state.get("max_deficit")),
                    "first_deficit_date": str(first_external_deadline or ""),
                    "action_url": f"/inventario/stock/{stock.product_id}/",
                    "action_label": "Ver detalhe do stock",
                    "secondary_action_url": f"/recomendacoes/?product={stock.product_id}",
                    "secondary_action_label": "Abrir recomendações",
                    "impact_label": f"Falta stock para cumprir pedidos externos de {stock.product.name}",
                    "reason": "O stock atual e a produção prevista útil não chegam a tempo dos pedidos externos.",
                },
                requires_action=True,
                priority=10,
            )
        )
    return rows


def _surplus_candidates(producer):
    rows = []
    stocks = (
        Stock.objects
        .select_related("product")
        .filter(
            producer=producer,
            product__is_active=True,
            product__producer_links__producer=producer,
            product__producer_links__is_active=True,
        )
        .distinct()
    )

    for stock in stocks:
        commitment_state = calculate_inventory_commitment_state(
            producer,
            stock.product,
            stock=stock,
        )
        real_surplus = _as_decimal(commitment_state.get("temporal_sellable_quantity"))
        if real_surplus <= Decimal("0.000"):
            continue

        total_external_demand = _as_decimal(commitment_state.get("total_external_demand"))
        if total_external_demand > 0 and real_surplus <= (total_external_demand * Decimal("0.10")):
            continue

        unit = getattr(stock.product, "unit", "") or ""
        surplus_label = _quantity_label(real_surplus, unit)
        rows.append(
            _candidate(
                alert_type=AlertType.SURPLUS_AVAILABLE,
                severity=AlertSeverity.INFO,
                category=AlertCategory.MARKETPLACE,
                product=stock.product,
                title=f"Excedente disponível: {stock.product.name}",
                description=(
                    f"Margem vendável depois de cumprir pedidos externos por data: {surplus_label}."
                ),
                payload={
                    "real_surplus": str(real_surplus),
                    "useful_forecast_total": str(commitment_state.get("useful_forecast_total")),
                    "total_external_demand": str(total_external_demand),
                    "action_url": (
                        f"/marketplace/publicar/?source=stock&product={stock.product_id}&from=inventory"
                    ),
                    "action_label": "Publicar no marketplace",
                    "reason": "Existe margem temporal acima dos compromissos externos.",
                },
                requires_action=False,
                priority=55,
            )
        )
    return rows


def _need_candidates(producer):
    rows = []
    needs = (
        Need.objects
        .select_related("product")
        .filter(
            producer=producer,
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    for need in needs:
        coverage = calculate_need_coverage(need)
        remaining_to_plan = _as_decimal(coverage.get("remaining_to_plan"))

        if need.status == NeedStatus.PARTIALLY_COVERED and remaining_to_plan <= Decimal("0.000"):
            continue

        unit = getattr(need.product, "unit", "") or ""
        remaining_label = _quantity_label(remaining_to_plan, unit)
        is_customer_demand = getattr(need, "source_system", None) == NeedSourceSystem.CUSTOMER_DEMAND
        rows.append(
            _candidate(
                alert_type=AlertType.NEED_UNDERCOVERED,
                severity=AlertSeverity.WARNING,
                category=AlertCategory.NEEDS,
                product=need.product,
                need=need,
                title=(
                    f"Procura de clientes por cobrir: {need.product.name}"
                    if is_customer_demand
                    else f"Necessidade por cobrir: {need.product.name}"
                ),
                description=(
                    f"Faltam {remaining_label} para cumprir pedidos externos de clientes."
                    if is_customer_demand
                    else f"Em falta para planear: {remaining_label}."
                ),
                payload={
                    "required_quantity": str(coverage.get("required_quantity")),
                    "planned_qty": str(coverage.get("planned_qty")),
                    "completed_qty": str(coverage.get("completed_qty")),
                    "remaining_to_plan": str(remaining_to_plan),
                    "action_url": f"/necessidades/?need={need.id}",
                    "action_label": "Ver necessidade",
                    "secondary_action_url": f"/recomendacoes/?product={need.product_id}",
                    "secondary_action_label": "Abrir recomendações",
                    "reason": (
                        "A procura gerada por pedidos externos ainda não tem quantidade suficiente planeada."
                        if is_customer_demand
                        else "A necessidade ainda não tem quantidade suficiente planeada."
                    ),
                },
                requires_action=True,
                priority=30,
            )
        )
    return rows


def _sell_suggestion_candidates(producer):
    rows = []
    forecasts = (
        ProductionForecast.objects
        .select_related("product")
        .filter(
            producer=producer,
            is_marketplace_enabled=True,
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    for forecast in forecasts:
        saleable = _as_decimal(get_forecast_available_quantity(forecast))
        if saleable <= Decimal("0.000"):
            continue

        unit = getattr(forecast.product, "unit", "") or ""
        saleable_label = _quantity_label(saleable, unit)
        rows.append(
            _candidate(
                alert_type=AlertType.SELL_SUGGESTION,
                severity=AlertSeverity.INFO,
                category=AlertCategory.MARKETPLACE,
                product=forecast.product,
                forecast=forecast,
                title="Pré-venda disponível para publicar",
                description=(
                    f"{forecast.product.name}: {saleable_label} disponíveis para pré-venda."
                ),
                payload={
                    "saleable_quantity": str(saleable),
                    "action_url": (
                        f"/marketplace/publicar/?source=forecast&product={forecast.product_id}&forecast={forecast.id}"
                    ),
                    "action_label": "Publicar pré-venda",
                    "reason": "Existe produção futura marcada como disponível para marketplace.",
                },
                requires_action=False,
                priority=60,
            )
        )
    return rows


def _need_response_candidates(producer):
    rows = []
    listings = (
        MarketplaceListing.objects
        .select_related("producer", "producer__user", "product", "need", "forecast")
        .filter(
            need__producer=producer,
            need_response_status=NeedResponseStatus.PENDING,
            status=ListingStatus.ACTIVE,
            quantity_available__gt=0,
        )
        .filter(order_items__isnull=True)
        .order_by("-published_at", "-created_at")
        .distinct()
    )

    for listing in listings:
        unit = getattr(listing.product, "unit", "") or ""
        quantity_label = _quantity_label(listing.quantity_available, unit)
        price_label = _money_label(listing.unit_price)
        producer_label = (
            getattr(listing.producer, "display_name", None)
            or getattr(listing.producer, "company_name", None)
            or "Outro produtor"
        )
        rows.append(
            _candidate(
                alert_type=AlertType.NEED_RESPONSE_RECEIVED,
                severity=AlertSeverity.WARNING,
                category=AlertCategory.NEEDS,
                product=listing.product,
                need=listing.need,
                forecast=getattr(listing, "forecast", None),
                listing=listing,
                title=f"Nova oferta para {listing.product.name}",
                description=(
                    f"{producer_label} ofereceu {quantity_label} "
                    f"a {price_label}/{unit}".rstrip("/")
                ),
                payload={
                    "listing_id": str(listing.id),
                    "need_id": str(listing.need_id),
                    "quantity_available": str(listing.quantity_available),
                    "unit_price": str(listing.unit_price),
                    "action_url": f"/necessidades/respostas/{listing.id}/",
                    "action_label": "Ver oferta",
                    "secondary_action_url": f"/necessidades/?need={listing.need_id}",
                    "secondary_action_label": "Ver necessidade",
                    "reason": "Um produtor respondeu a uma necessidade sua.",
                },
                requires_action=True,
                priority=18,
            )
        )
    return rows


def _need_deadline_candidates(producer):
    rows = []
    now = timezone.now()
    deadline_limit = now + NEED_DEADLINE_WINDOW
    needs = (
        Need.objects
        .select_related("product")
        .filter(
            producer=producer,
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            needed_by_date__isnull=False,
            needed_by_date__lte=deadline_limit,
            product__is_active=True,
        )
        .order_by("needed_by_date", "-updated_at")
    )

    for need in needs:
        coverage = calculate_need_coverage(need)
        remaining_to_receive = _as_decimal(coverage.get("remaining_to_receive"))
        if remaining_to_receive <= Decimal("0.000"):
            continue

        unit = getattr(need.product, "unit", "") or ""
        remaining_label = _quantity_label(remaining_to_receive, unit)
        is_overdue = need.needed_by_date and need.needed_by_date <= now
        rows.append(
            _candidate(
                alert_type=AlertType.NEED_DEADLINE_APPROACHING,
                severity=AlertSeverity.CRITICAL if is_overdue else AlertSeverity.WARNING,
                category=AlertCategory.NEEDS,
                product=need.product,
                need=need,
                title=(
                    f"Prazo ultrapassado: {need.product.name}"
                    if is_overdue
                    else f"Prazo próximo para necessidade: {need.product.name}"
                ),
                description=f"Ainda faltam receber {remaining_label}.",
                payload={
                    "remaining_to_receive": str(remaining_to_receive),
                    "needed_by_date": need.needed_by_date.isoformat() if need.needed_by_date else "",
                    "action_url": f"/necessidades/?need={need.id}",
                    "action_label": "Ver necessidade",
                    "secondary_action_url": f"/recomendacoes/?product={need.product_id}",
                    "secondary_action_label": "Abrir recomendações",
                    "reason": "O prazo da necessidade está próximo e a quantidade ainda não foi recebida.",
                },
                requires_action=True,
                due_at=need.needed_by_date,
                priority=12 if is_overdue else 22,
            )
        )
    return rows


def _buy_opportunity_candidates(producer):
    rows = []
    needs = (
        Need.objects
        .select_related("product")
        .filter(
            producer=producer,
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    for need in needs:
        coverage = calculate_need_coverage(need)
        remaining_to_receive = _as_decimal(coverage.get("remaining_to_receive"))
        if remaining_to_receive <= Decimal("0.000"):
            continue

        matching_listings = (
            MarketplaceListing.objects
            .filter(
                product=need.product,
                status=ListingStatus.ACTIVE,
                quantity_available__gt=0,
                need_id__isnull=True,
            )
            .exclude(producer=producer)
            .order_by("unit_price", "-published_at")
        )
        first_listing = matching_listings.first()
        if not first_listing:
            continue

        total_available = Decimal("0.000")
        count = 0
        for listing in matching_listings.only("quantity_available"):
            total_available += _as_decimal(listing.quantity_available)
            count += 1

        unit = getattr(need.product, "unit", "") or ""
        available_label = _quantity_label(total_available, unit)
        remaining_label = _quantity_label(remaining_to_receive, unit)
        rows.append(
            _candidate(
                alert_type=AlertType.BUY_OPPORTUNITY,
                severity=AlertSeverity.INFO,
                category=AlertCategory.MARKETPLACE,
                product=need.product,
                need=need,
                listing=first_listing,
                context_key=_build_context_key(AlertType.BUY_OPPORTUNITY, need_id=need.id),
                title=f"Oportunidade para cobrir {need.product.name}",
                description=(
                    f"Existem {available_label} disponíveis no marketplace "
                    f"para uma necessidade com {remaining_label} por receber."
                ),
                payload={
                    "remaining_to_receive": str(remaining_to_receive),
                    "available_quantity": str(total_available),
                    "matching_listings_count": str(count),
                    "action_url": f"/recomendacoes/?product={need.product_id}",
                    "action_label": "Ver recomendações",
                    "secondary_action_url": f"/marketplace/?q={need.product.name}",
                    "secondary_action_label": "Ver marketplace",
                    "reason": "Há ofertas públicas que podem ajudar a cobrir uma necessidade sua.",
                },
                requires_action=False,
                priority=45,
            )
        )
    return rows


def _order_confirmation_candidates(producer):
    rows = []
    now = timezone.now()
    orders = (
        Order.objects
        .filter(
            items__seller_producer=producer,
            status=OrderStatus.PENDING,
        )
        .order_by("created_at")
        .distinct()
    )

    for order in orders:
        due_at = order.created_at + ORDER_CONFIRMATION_GRACE if order.created_at else None
        is_overdue = bool(due_at and due_at <= now)
        rows.append(
            _candidate(
                alert_type=AlertType.ORDER_REQUIRES_CONFIRMATION,
                severity=AlertSeverity.CRITICAL if is_overdue else AlertSeverity.WARNING,
                category=AlertCategory.ORDERS,
                title=f"Encomenda #{order.order_number} por confirmar",
                description=(
                    "A encomenda já ultrapassou o tempo recomendado para confirmação."
                    if is_overdue
                    else "Tem uma encomenda recebida a aguardar confirmação."
                ),
                payload={
                    "order_id": str(order.id),
                    "order_number": order.order_number,
                    "action_url": f"/encomendas/{order.id}/",
                    "action_label": "Gerir encomenda",
                    "reason": "O vendedor deve confirmar, preparar ou cancelar a encomenda.",
                },
                context_key=f"{AlertType.ORDER_REQUIRES_CONFIRMATION}:order:{order.id}",
                requires_action=True,
                due_at=due_at,
                priority=14 if is_overdue else 24,
            )
        )
    return rows


def _order_delivery_overdue_candidates(producer):
    rows = []
    now = timezone.now()
    cutoff = now - ORDER_DELIVERY_GRACE
    orders = (
        Order.objects
        .filter(
            buyer_producer=producer,
            status=OrderStatus.DELIVERING,
            updated_at__lte=cutoff,
        )
        .order_by("updated_at")
    )

    for order in orders:
        rows.append(
            _candidate(
                alert_type=AlertType.ORDER_DELIVERY_OVERDUE,
                severity=AlertSeverity.WARNING,
                category=AlertCategory.ORDERS,
                title=f"Entrega por confirmar na encomenda #{order.order_number}",
                description="A encomenda está em entrega há vários dias. Confirme receção ou contacte o produtor.",
                payload={
                    "order_id": str(order.id),
                    "order_number": order.order_number,
                    "action_url": f"/encomendas/{order.id}/?force_single=1",
                    "action_label": "Ver encomenda",
                    "secondary_action_url": f"/mensagens/encomenda/{order.id}/iniciar/",
                    "secondary_action_label": "Contactar produtor",
                    "reason": "A encomenda está em entrega há mais tempo do que o esperado.",
                },
                context_key=f"{AlertType.ORDER_DELIVERY_OVERDUE}:order:{order.id}",
                requires_action=True,
                due_at=order.updated_at + ORDER_DELIVERY_GRACE if order.updated_at else None,
                priority=16,
            )
        )
    return rows


def _listing_expiring_candidates(producer):
    rows = []
    now = timezone.now()
    cutoff = now + LISTING_EXPIRING_WINDOW
    listings = (
        MarketplaceListing.objects
        .select_related("product", "forecast", "stock")
        .filter(
            producer=producer,
            status=ListingStatus.ACTIVE,
            need_id__isnull=True,
            expires_at__isnull=False,
            expires_at__gt=now,
            expires_at__lte=cutoff,
        )
        .order_by("expires_at", "-updated_at")
    )

    for listing in listings:
        rows.append(
            _candidate(
                alert_type=AlertType.LISTING_EXPIRING_SOON,
                severity=AlertSeverity.WARNING,
                category=AlertCategory.MARKETPLACE,
                product=listing.product,
                forecast=getattr(listing, "forecast", None),
                listing=listing,
                title=f"Anúncio a expirar: {listing.product.name}",
                description="Este anúncio termina em breve. Reveja a oferta se ainda estiver disponível.",
                payload={
                    "listing_id": str(listing.id),
                    "expires_at": listing.expires_at.isoformat() if listing.expires_at else "",
                    "action_url": f"/marketplace/{listing.id}/editar/",
                    "action_label": "Rever anúncio",
                    "secondary_action_url": f"/marketplace/{listing.id}/",
                    "secondary_action_label": "Ver detalhe",
                    "reason": "A data de expiração do anúncio está próxima.",
                },
                requires_action=False,
                due_at=listing.expires_at,
                expires_at=listing.expires_at,
                priority=50,
            )
        )
    return rows


def _candidate_rows(producer):
    rows = []
    rows.extend(_critical_stock_candidates(producer))
    rows.extend(_surplus_candidates(producer))
    rows.extend(_need_candidates(producer))
    rows.extend(_need_response_candidates(producer))
    rows.extend(_need_deadline_candidates(producer))
    rows.extend(_buy_opportunity_candidates(producer))
    rows.extend(_sell_suggestion_candidates(producer))
    rows.extend(_order_confirmation_candidates(producer))
    rows.extend(_order_delivery_overdue_candidates(producer))
    rows.extend(_listing_expiring_candidates(producer))
    return rows


def _apply_candidate_to_alert(alert, candidate, *, now, force_active=False):
    update_fields = []

    field_values = {
        "severity": candidate["severity"],
        "category": candidate["category"],
        "context_key": candidate["key"],
        "title": candidate["title"],
        "description": candidate["description"],
        "source_system": AlertSourceSystem.INTERNAL,
        "payload": candidate["payload"],
        "product": candidate["product"],
        "need": candidate["need"],
        "forecast": candidate["forecast"],
        "listing": candidate["listing"],
        "requires_action": candidate["requires_action"],
        "due_at": candidate["due_at"],
        "expires_at": candidate["expires_at"],
        "priority": candidate["priority"],
    }

    for field_name, value in field_values.items():
        if getattr(alert, field_name) != value:
            setattr(alert, field_name, value)
            update_fields.append(field_name)

    if force_active and alert.status != AlertStatus.ACTIVE:
        alert.status = AlertStatus.ACTIVE
        update_fields.append("status")

    if update_fields:
        alert.updated_at = now
        update_fields.append("updated_at")
        alert.save(update_fields=list(dict.fromkeys(update_fields)))
        return True
    return False


@transaction.atomic
def sync_alerts_for_producer(producer, acting_user=None):
    now = timezone.now()
    candidates = _candidate_rows(producer)
    candidate_map = {row["key"]: row for row in candidates}

    existing_alerts = list(
        Alert.objects
        .select_for_update()
        .filter(
            producer=producer,
            type__in=MANAGED_ALERT_TYPES,
            status__in=ACTIVE_LIKE_ALERT_STATUSES,
        )
        .order_by("-created_at")
    )

    existing_map = {}
    duplicate_alerts = []
    for alert in existing_alerts:
        key = _alert_context_key(alert)
        if key not in existing_map:
            existing_map[key] = alert
        else:
            duplicate_alerts.append(alert)

    for duplicate in duplicate_alerts:
        duplicate.status = AlertStatus.RESOLVED
        duplicate.cleared_at = now
        duplicate.updated_at = now
        duplicate.save(update_fields=["status", "cleared_at", "updated_at"])
        record_alert_event(
            duplicate,
            AlertEventType.RESOLVED,
            performed_by=acting_user,
            notes="Resolução automática por deduplicação de contexto",
        )

    ignored_alerts = list(
        Alert.objects
        .select_for_update()
        .filter(
            producer=producer,
            type__in=MANAGED_ALERT_TYPES,
            status=AlertStatus.IGNORED,
        )
        .order_by("-updated_at", "-created_at")
    )
    ignored_map = {}
    for alert in ignored_alerts:
        key = _alert_context_key(alert)
        if key not in ignored_map:
            ignored_map[key] = alert

    resolved_suppressed_alerts = list(
        Alert.objects
        .select_for_update()
        .filter(
            producer=producer,
            type__in=MANAGED_ALERT_TYPES,
            status=AlertStatus.RESOLVED,
            cleared_at__isnull=True,
        )
        .order_by("-updated_at", "-created_at")
    )
    resolved_suppressed_map = {}
    for alert in resolved_suppressed_alerts:
        key = _alert_context_key(alert)
        if key not in resolved_suppressed_map:
            resolved_suppressed_map[key] = alert

    created_count = 0
    updated_count = 0
    resolved_count = len(duplicate_alerts)
    cleared_count = 0

    for key, candidate in candidate_map.items():
        existing = existing_map.get(key)
        if existing:
            changed = _apply_candidate_to_alert(
                existing,
                candidate,
                now=now,
                force_active=True,
            )
            if changed:
                updated_count += 1
            continue

        ignored_alert = ignored_map.get(key)
        if ignored_alert and ignored_alert.cleared_at is None:
            continue

        resolved_suppressed_alert = resolved_suppressed_map.get(key)
        if resolved_suppressed_alert and resolved_suppressed_alert.cleared_at is None:
            continue

        alert = Alert.objects.create(
            producer=producer,
            product=candidate["product"],
            need=candidate["need"],
            forecast=candidate["forecast"],
            listing=candidate["listing"],
            type=candidate["type"],
            severity=candidate["severity"],
            category=candidate["category"],
            context_key=candidate["key"],
            title=candidate["title"],
            description=candidate["description"],
            source_system=AlertSourceSystem.INTERNAL,
            status=AlertStatus.ACTIVE,
            payload=candidate["payload"],
            assumed_loss=False,
            requires_action=candidate["requires_action"],
            due_at=candidate["due_at"],
            expires_at=candidate["expires_at"],
            priority=candidate["priority"],
        )
        record_alert_event(
            alert,
            AlertEventType.CREATED,
            performed_by=acting_user,
            notes="Alerta criado automaticamente",
        )
        _queue_alert_delivery_for_producer(alert=alert, producer=producer)
        created_count += 1

    for key, alert in existing_map.items():
        if key in candidate_map:
            continue
        if alert.status not in ACTIVE_LIKE_ALERT_STATUSES:
            continue
        alert.status = AlertStatus.RESOLVED
        alert.cleared_at = now
        alert.updated_at = now
        alert.save(update_fields=["status", "cleared_at", "updated_at"])
        record_alert_event(
            alert,
            AlertEventType.RESOLVED,
            performed_by=acting_user,
            notes=AUTO_RESOLVED_NOTE,
        )
        resolved_count += 1

    for key, ignored_alert in ignored_map.items():
        if key in candidate_map:
            continue
        if ignored_alert.cleared_at is not None:
            continue
        ignored_alert.cleared_at = now
        ignored_alert.updated_at = now
        ignored_alert.save(update_fields=["cleared_at", "updated_at"])

    for key, resolved_suppressed_alert in resolved_suppressed_map.items():
        if key in candidate_map:
            continue
        if resolved_suppressed_alert.cleared_at is not None:
            continue
        resolved_suppressed_alert.cleared_at = now
        resolved_suppressed_alert.updated_at = now
        resolved_suppressed_alert.save(update_fields=["cleared_at", "updated_at"])
        record_alert_event(
            resolved_suppressed_alert,
            AlertEventType.CLEARED,
            performed_by=acting_user,
            notes="Condição de alerta resolvido deixou de existir.",
        )
        cleared_count += 1

    if created_count or resolved_count:
        _queue_alerts_badge_changed_for_user(user_id=getattr(producer, "user_id", None))

    return {
        "created": created_count,
        "updated": updated_count,
        "resolved": resolved_count,
        "cleared": cleared_count,
    }


@transaction.atomic
def expire_due_alerts(*, producer=None, acting_user=None):
    now = timezone.now()
    expiring_qs = (
        Alert.objects
        .select_for_update()
        .filter(
            status__in=[AlertStatus.ACTIVE, AlertStatus.READ, AlertStatus.IGNORED],
            expires_at__isnull=False,
            expires_at__lte=now,
        )
    )
    if producer:
        expiring_qs = expiring_qs.filter(producer=producer)

    expiring_alerts = list(expiring_qs.order_by("expires_at", "created_at"))
    for alert in expiring_alerts:
        alert.status = AlertStatus.CLEARED
        alert.cleared_at = now
        alert.updated_at = now
        alert.save(update_fields=["status", "cleared_at", "updated_at"])
        record_alert_event(
            alert,
            AlertEventType.CLEARED,
            performed_by=acting_user,
            notes="Alerta expirado automaticamente por tarefa agendada.",
        )
        _queue_alerts_badge_changed_for_user(user_id=getattr(alert.producer, "user_id", None))

    return len(expiring_alerts)


def run_operational_alerts_job(*, producer_id=None, limit=None, apply=False, acting_user=None):
    from apps.accounts.models import AccountStatus
    from apps.marketplace.services import expire_due_active_listings

    summary = {
        "mode": "apply" if apply else "dry-run",
        "producers_seen": 0,
        "producers_synced": 0,
        "listings_expired": 0,
        "ignored_expired": 0,
        "alerts_expired": 0,
        "created": 0,
        "updated": 0,
        "resolved": 0,
        "cleared": 0,
        "errors": 0,
    }

    producers_qs = (
        ProducerProfile.objects
        .select_related("user", "user__preferences")
        .filter(user__is_active=True, user__account_status=AccountStatus.ACTIVE)
        .order_by("display_name", "id")
    )
    if producer_id:
        producers_qs = producers_qs.filter(id=producer_id)
    if limit:
        producers_qs = producers_qs[: int(limit)]

    producers = list(producers_qs)
    summary["producers_seen"] = len(producers)

    if not apply:
        return summary

    summary["listings_expired"] = int(expire_due_active_listings() or 0)

    for producer in producers:
        try:
            summary["ignored_expired"] += expire_ignored_alerts_for_producer(
                producer=producer,
                acting_user=acting_user,
            )
            summary["alerts_expired"] += expire_due_alerts(
                producer=producer,
                acting_user=acting_user,
            )
            result = sync_alerts_for_producer(producer, acting_user=acting_user)
            summary["created"] += int(result.get("created") or 0)
            summary["updated"] += int(result.get("updated") or 0)
            summary["resolved"] += int(result.get("resolved") or 0)
            summary["cleared"] += int(result.get("cleared") or 0)
            summary["producers_synced"] += 1
        except Exception:
            summary["errors"] += 1
            logger.exception("Falha no job de alertas produtor_id=%s", producer.id)

    return summary


@transaction.atomic
def ignore_alert(alert, user, reason=None, *, snooze_key=None, queue_badge_update=True):
    if alert.status not in INTERACTIVE_ALERT_STATUSES:
        return False

    now = timezone.now()
    _key, label, delta = _get_snooze_option(snooze_key)
    alert.status = AlertStatus.IGNORED
    alert.ignored_at = now
    alert.snoozed_until = now + delta
    alert.cleared_at = None
    alert.ignored_reason = (reason or "").strip() or None
    alert.updated_at = now
    alert.save(update_fields=["status", "ignored_at", "snoozed_until", "cleared_at", "ignored_reason", "updated_at"])
    record_alert_event(
        alert,
        AlertEventType.IGNORED,
        performed_by=user,
        notes=alert.ignored_reason or f"Adiado pelo utilizador até {label}.",
    )
    if queue_badge_update:
        _queue_alerts_badge_changed_for_user(user_id=getattr(user, "id", None))
    return True


@transaction.atomic
def ignore_all_active_alerts(*, producer, user, reason=None, alert_type=None, category=None, q="", requires_action=False):
    if not producer or not user:
        return 0

    active_alerts_qs = (
        Alert.objects
        .select_for_update()
        .filter(
            producer=producer,
            status=AlertStatus.ACTIVE,
        )
    )
    normalized_type = normalize_alert_type(alert_type)
    if normalized_type:
        active_alerts_qs = active_alerts_qs.filter(type=normalized_type)
    normalized_category = normalize_alert_category(category)
    if normalized_category:
        active_alerts_qs = active_alerts_qs.filter(category=normalized_category)
    if requires_action:
        active_alerts_qs = active_alerts_qs.filter(requires_action=True)
    q = (q or "").strip()
    if q:
        active_alerts_qs = active_alerts_qs.filter(
            Q(title__icontains=q)
            | Q(description__icontains=q)
            | Q(product__name__icontains=q)
            | Q(payload__product_name__icontains=q)
            | Q(payload__counterpart_name__icontains=q)
        )

    active_alerts = list(active_alerts_qs.order_by("-updated_at", "-created_at"))
    if not active_alerts:
        return 0

    ignored_count = 0
    for alert in active_alerts:
        changed = ignore_alert(
            alert,
            user=user,
            reason=reason,
            snooze_key=DEFAULT_SNOOZE_KEY,
            queue_badge_update=False,
        )
        if changed:
            ignored_count += 1

    if ignored_count:
        _queue_alerts_badge_changed_for_user(user_id=getattr(user, "id", None))

    return ignored_count


@transaction.atomic
def reactivate_ignored_alert(alert, user):
    if alert.status != AlertStatus.IGNORED:
        return False

    now = timezone.now()
    alert.status = AlertStatus.ACTIVE
    alert.ignored_at = None
    alert.ignored_reason = None
    alert.snoozed_until = None
    alert.cleared_at = None
    alert.updated_at = now
    alert.save(
        update_fields=[
            "status",
            "ignored_at",
            "ignored_reason",
            "snoozed_until",
            "cleared_at",
            "updated_at",
        ]
    )
    _queue_alerts_badge_changed_for_user(user_id=getattr(user, "id", None))
    return True


@transaction.atomic
def expire_ignored_alerts_for_producer(*, producer, acting_user=None):
    if not producer:
        return 0

    now = timezone.now()
    cutoff = now - IGNORED_ALERT_TTL
    expiring_alerts = list(
        Alert.objects
        .select_for_update()
        .filter(
            producer=producer,
            status=AlertStatus.IGNORED,
            ignored_at__isnull=False,
        )
        .filter(Q(snoozed_until__isnull=False, snoozed_until__lte=now) | Q(snoozed_until__isnull=True, ignored_at__lte=cutoff))
        .order_by("ignored_at", "created_at")
    )
    if not expiring_alerts:
        return 0

    for alert in expiring_alerts:
        alert.status = AlertStatus.CLEARED
        if alert.cleared_at is None:
            alert.cleared_at = now
        alert.updated_at = now
        alert.save(update_fields=["status", "cleared_at", "updated_at"])
        record_alert_event(
            alert,
            AlertEventType.CLEARED,
            performed_by=acting_user,
            notes="Alerta ignorado expirado automaticamente após 30 minutos.",
        )

    return len(expiring_alerts)


@transaction.atomic
def resolve_alert(alert, user, notes=None):
    if alert.status not in INTERACTIVE_ALERT_STATUSES:
        return False

    now = timezone.now()
    alert.status = AlertStatus.RESOLVED
    if alert.type in MANAGED_ALERT_TYPES:
        alert.cleared_at = None
    else:
        alert.cleared_at = now
    alert.updated_at = now
    alert.save(update_fields=["status", "cleared_at", "updated_at"])
    record_alert_event(
        alert,
        AlertEventType.RESOLVED,
        performed_by=user,
        notes=(notes or "").strip() or "Resolução manual pelo utilizador",
    )
    _queue_alerts_badge_changed_for_user(user_id=getattr(user, "id", None))
    return True


def get_alert_for_producer(*, producer, alert_id):
    return (
        Alert.objects
        .select_related("product", "need", "forecast", "listing")
        .filter(id=alert_id, producer=producer)
        .first()
    )


def get_alert_tab_counts(*, producer):
    return {
        "active": Alert.objects.filter(producer=producer, status=AlertStatus.ACTIVE).count(),
        "ignored": Alert.objects.filter(producer=producer, status=AlertStatus.IGNORED).count(),
        "resolved": Alert.objects.filter(producer=producer, status=AlertStatus.RESOLVED).count(),
    }


def get_alert_type_filter_options(*, producer, tab="active", selected_type=None):
    selected_status = UI_TAB_STATUS_MAP.get(tab, AlertStatus.ACTIVE)
    normalized_selected_type = normalize_alert_type(selected_type)
    rows = (
        Alert.objects
        .filter(producer=producer, status=selected_status)
        .values("type")
        .annotate(count=Count("id"))
        .order_by("type")
    )

    options_by_value = {}
    for row in rows:
        alert_type = row.get("type")
        if not alert_type:
            continue
        options_by_value[alert_type] = {
            "value": alert_type,
            "label": get_alert_type_label(alert_type),
            "count": int(row.get("count") or 0),
            "selected": alert_type == normalized_selected_type,
        }

    if normalized_selected_type and normalized_selected_type not in options_by_value:
        options_by_value[normalized_selected_type] = {
            "value": normalized_selected_type,
            "label": get_alert_type_label(normalized_selected_type),
            "count": 0,
            "selected": True,
        }

    return sorted(options_by_value.values(), key=lambda item: item["label"].lower())


def get_alert_category_filter_options(*, producer, tab="active", selected_category=None):
    selected_status = UI_TAB_STATUS_MAP.get(tab, AlertStatus.ACTIVE)
    normalized_selected_category = normalize_alert_category(selected_category)
    rows = (
        Alert.objects
        .filter(producer=producer, status=selected_status)
        .values("category")
        .annotate(count=Count("id"))
        .order_by("category")
    )

    options_by_value = {}
    for row in rows:
        category = row.get("category")
        if not category:
            continue
        options_by_value[category] = {
            "value": category,
            "label": get_alert_category_label(category),
            "count": int(row.get("count") or 0),
            "selected": category == normalized_selected_category,
        }

    if normalized_selected_category and normalized_selected_category not in options_by_value:
        options_by_value[normalized_selected_category] = {
            "value": normalized_selected_category,
            "label": get_alert_category_label(normalized_selected_category),
            "count": 0,
            "selected": True,
        }

    return sorted(options_by_value.values(), key=lambda item: item["label"].lower())


def _alert_section_key(alert):
    if getattr(alert, "requires_action", False):
        return "now"
    category = getattr(alert, "category", None)
    if category in {AlertCategory.STOCK, AlertCategory.NEEDS, AlertCategory.ORDERS}:
        return "risk"
    if category == AlertCategory.MARKETPLACE:
        return "opportunity"
    return "info"


def build_alert_sections(alerts, *, active_tab="active"):
    if active_tab != "active":
        return [
            {
                "key": "history",
                "title": "Histórico",
                "description": "Alertas nesta vista.",
                "alerts": alerts,
            }
        ] if alerts else []

    section_map = {
        "now": {
            "key": "now",
            "title": "A fazer agora",
            "description": "Alertas que exigem uma decisão ou ação concreta.",
            "alerts": [],
        },
        "risk": {
            "key": "risk",
            "title": "Risco agrícola",
            "description": "Stock, necessidades, prazos e encomendas que podem afetar a operação.",
            "alerts": [],
        },
        "opportunity": {
            "key": "opportunity",
            "title": "Oportunidades",
            "description": "Situações que podem gerar venda, compra ou melhor aproveitamento.",
            "alerts": [],
        },
        "info": {
            "key": "info",
            "title": "Informação",
            "description": "Eventos úteis, sem ação urgente associada.",
            "alerts": [],
        },
    }
    for alert in alerts:
        section_map[_alert_section_key(alert)]["alerts"].append(alert)
    return [section for section in section_map.values() if section["alerts"]]


def list_alerts_for_producer(*, producer, tab="active", alert_type=None, category=None, q="", requires_action=False):
    selected_status = UI_TAB_STATUS_MAP.get(tab, AlertStatus.ACTIVE)
    alerts_qs = (
        Alert.objects
        .select_related("product", "need", "forecast", "listing")
        .filter(producer=producer, status=selected_status)
    )
    normalized_type = normalize_alert_type(alert_type)
    if normalized_type:
        alerts_qs = alerts_qs.filter(type=normalized_type)
    normalized_category = normalize_alert_category(category)
    if normalized_category:
        alerts_qs = alerts_qs.filter(category=normalized_category)
    if requires_action:
        alerts_qs = alerts_qs.filter(requires_action=True)
    q = (q or "").strip()
    if q:
        alerts_qs = alerts_qs.filter(
            Q(title__icontains=q)
            | Q(description__icontains=q)
            | Q(product__name__icontains=q)
            | Q(payload__product_name__icontains=q)
            | Q(payload__counterpart_name__icontains=q)
        )

    alerts = list(alerts_qs.order_by("priority", "-updated_at", "-created_at"))

    severity_labels = dict(AlertSeverity.choices)
    for alert in alerts:
        payload = alert.payload or {}
        alert.type_label = get_alert_type_label(alert.type)
        alert.category_label = get_alert_category_label(getattr(alert, "category", AlertCategory.SYSTEM))
        alert.severity_label = severity_labels.get(alert.severity, alert.severity)
        alert.action_url = payload.get("action_url")
        if payload.get("action_label"):
            alert.action_label = payload.get("action_label")
        elif alert.type == AlertType.MESSAGE_UNREAD:
            alert.action_label = "Ir para conversa"
        elif alert.type in ORDER_ALERT_TYPES:
            alert.action_label = "Ir para encomenda"
        else:
            alert.action_label = "Abrir contexto"

        secondary_action_url = payload.get("secondary_action_url")
        if not secondary_action_url and alert.type in ORDER_ALERT_TYPES:
            order_id = payload.get("order_id")
            if order_id:
                secondary_action_url = f"/mensagens/encomenda/{order_id}/iniciar/"
        alert.secondary_action_url = secondary_action_url

        secondary_action_label = payload.get("secondary_action_label")
        if not secondary_action_label and secondary_action_url and alert.type in ORDER_ALERT_TYPES:
            secondary_action_label = "Ir para conversa"
        alert.secondary_action_label = secondary_action_label

        alert.related_product_name = (
            alert.product.name
            if alert.product
            else payload.get("product_name")
        )
        alert.reason = payload.get("reason")
        alert.impact_label = payload.get("impact_label")
    return alerts
