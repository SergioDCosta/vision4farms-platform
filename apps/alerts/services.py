import logging
from datetime import timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Count, Max
from django.db import transaction
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from apps.accounts.models import UserRole
from apps.alerts.models import (
    Alert,
    AlertEvent,
    AlertEventType,
    AlertSeverity,
    AlertSourceSystem,
    AlertStatus,
    AlertType,
)
from apps.inventory.models import Need, NeedStatus, ProducerProfile, ProductionForecast, Stock
from apps.inventory.services import calculate_need_coverage
from apps.marketplace.services import get_forecast_available_quantity


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
    AlertType.SELL_SUGGESTION,
}
ORDER_ALERT_TYPES = {
    AlertType.ORDER_PURCHASE_CREATED,
    AlertType.ORDER_CONFIRMED,
    AlertType.ORDER_IN_PROGRESS,
    AlertType.ORDER_DELIVERING,
    AlertType.ORDER_CANCELLED,
    AlertType.ORDER_COMPLETED,
}
AUTO_RESOLVED_NOTE = "Resolução automática por fim da condição"
ALERTS_LAST_SEEN_SESSION_KEY = "alerts_last_seen_at"
ALERTS_BADGE_GROUP_PREFIX = "alerts_badge_user_"
IGNORED_ALERT_TTL = timedelta(minutes=30)


logger = logging.getLogger(__name__)


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


def get_alert_type_label(alert_type):
    labels = {
        AlertType.CRITICAL_STOCK: "Stock crítico",
        AlertType.SURPLUS_AVAILABLE: "Excedente / oportunidade de venda",
        AlertType.EXTERNAL_DEFICIT: "Need sem cobertura suficiente",
        AlertType.SELL_SUGGESTION: "Pré-venda disponível para publicar",
        AlertType.ORDER_PURCHASE_CREATED: "Nova compra recebida",
        AlertType.ORDER_CONFIRMED: "Encomenda confirmada",
        AlertType.ORDER_IN_PROGRESS: "Encomenda em preparação",
        AlertType.ORDER_DELIVERING: "Encomenda em entrega",
        AlertType.ORDER_CANCELLED: "Encomenda cancelada",
        AlertType.ORDER_COMPLETED: "Receção confirmada",
        AlertType.MESSAGE_UNREAD: "Nova mensagem",
    }
    return labels.get(str(alert_type), str(alert_type))


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
    return _build_context_key(
        alert.type,
        product_id=getattr(alert, "product_id", None),
        need_id=getattr(alert, "need_id", None),
        forecast_id=getattr(alert, "forecast_id", None),
        listing_id=getattr(alert, "listing_id", None),
    )


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

    severity = (
        AlertSeverity.WARNING
        if alert_type == AlertType.ORDER_CANCELLED
        else AlertSeverity.INFO
    )
    alert = Alert.objects.create(
        producer=target_producer,
        product=getattr(first_item, "product", None),
        listing=getattr(first_item, "listing", None),
        need=None,
        forecast=None,
        type=alert_type,
        severity=severity,
        title=title,
        description=description,
        source_system=AlertSourceSystem.INTERNAL,
        status=AlertStatus.ACTIVE,
        payload=payload,
        assumed_loss=False,
    )
    record_alert_event(
        alert,
        AlertEventType.CREATED,
        performed_by=acting_user,
        notes="Alerta de encomenda criado automaticamente.",
    )
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

    alert = (
        Alert.objects
        .select_for_update()
        .filter(
            producer=target_producer,
            type=AlertType.MESSAGE_UNREAD,
            status=AlertStatus.ACTIVE,
            payload__conversation_id=str(conversation_id),
        )
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
                    "updated_at",
                ]
            )
        _queue_alerts_badge_changed_for_user(user_id=target_producer.user_id)
        return alert

    alert = Alert.objects.create(
        producer=target_producer,
        type=AlertType.MESSAGE_UNREAD,
        severity=AlertSeverity.INFO,
        title=title,
        description=description,
        source_system=AlertSourceSystem.INTERNAL,
        status=AlertStatus.ACTIVE,
        payload=payload,
        assumed_loss=False,
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
            payload__conversation_id=str(conversation_id),
        )
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
        available_quantity = _as_decimal(stock.current_quantity) - _as_decimal(stock.reserved_quantity)
        safety_stock = _as_decimal(stock.safety_stock)
        if available_quantity > safety_stock:
            continue

        unit = getattr(stock.product, "unit", "") or ""
        rows.append(
            {
                "key": _build_context_key(AlertType.CRITICAL_STOCK, product_id=stock.product_id),
                "type": AlertType.CRITICAL_STOCK,
                "severity": AlertSeverity.CRITICAL,
                "product": stock.product,
                "need": None,
                "forecast": None,
                "listing": None,
                "title": f"Stock crítico: {stock.product.name}",
                "description": (
                    f"Disponível: {available_quantity} {unit} · "
                    f"Stock de segurança: {safety_stock} {unit}."
                ),
                "payload": {
                    "available_quantity": str(available_quantity),
                    "safety_stock": str(safety_stock),
                    "action_url": f"/inventario/stock/{stock.product_id}/",
                    "action_label": "Ver detalhe do stock",
                    "secondary_action_url": f"/recomendacoes/?product={stock.product_id}",
                    "secondary_action_label": "Abrir recomendações",
                },
            }
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
        available_quantity = _as_decimal(stock.current_quantity) - _as_decimal(stock.reserved_quantity)
        safety_stock = _as_decimal(stock.safety_stock)
        if available_quantity <= safety_stock:
            continue

        surplus_threshold = _as_decimal(stock.surplus_threshold)
        real_surplus = max(available_quantity - safety_stock, Decimal("0.000"))
        if real_surplus < surplus_threshold:
            continue

        unit = getattr(stock.product, "unit", "") or ""
        rows.append(
            {
                "key": _build_context_key(AlertType.SURPLUS_AVAILABLE, product_id=stock.product_id),
                "type": AlertType.SURPLUS_AVAILABLE,
                "severity": AlertSeverity.INFO,
                "product": stock.product,
                "need": None,
                "forecast": None,
                "listing": None,
                "title": f"Excedente disponível: {stock.product.name}",
                "description": (
                    f"Excedente real: {real_surplus} {unit} "
                    f"(limiar: {surplus_threshold} {unit})."
                ),
                "payload": {
                    "real_surplus": str(real_surplus),
                    "surplus_threshold": str(surplus_threshold),
                    "action_url": (
                        f"/marketplace/publicar/?source=stock&product={stock.product_id}&from=inventory"
                    ),
                    "action_label": "Publicar no marketplace",
                },
            }
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
        rows.append(
            {
                "key": _build_context_key(AlertType.EXTERNAL_DEFICIT, need_id=need.id),
                "type": AlertType.EXTERNAL_DEFICIT,
                "severity": AlertSeverity.WARNING,
                "product": need.product,
                "need": need,
                "forecast": None,
                "listing": None,
                "title": f"Need sem cobertura suficiente: {need.product.name}",
                "description": (
                    f"Em falta para planear: {remaining_to_plan} {unit}."
                ),
                "payload": {
                    "required_quantity": str(coverage.get("required_quantity")),
                    "planned_qty": str(coverage.get("planned_qty")),
                    "completed_qty": str(coverage.get("completed_qty")),
                    "remaining_to_plan": str(remaining_to_plan),
                    "action_url": f"/marketplace/?tab=necessidades&need={need.id}",
                    "action_label": "Ver necessidade",
                    "secondary_action_url": f"/recomendacoes/?product={need.product_id}",
                    "secondary_action_label": "Abrir recomendações",
                },
            }
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
        rows.append(
            {
                "key": _build_context_key(AlertType.SELL_SUGGESTION, forecast_id=forecast.id),
                "type": AlertType.SELL_SUGGESTION,
                "severity": AlertSeverity.INFO,
                "product": forecast.product,
                "need": None,
                "forecast": forecast,
                "listing": None,
                "title": "Pré-venda disponível para publicar",
                "description": (
                    f"{forecast.product.name}: {saleable} {unit} disponíveis para pré-venda."
                ),
                "payload": {
                    "saleable_quantity": str(saleable),
                    "action_url": (
                        f"/marketplace/publicar/?source=forecast&product={forecast.product_id}&forecast={forecast.id}"
                    ),
                    "action_label": "Publicar pré-venda",
                },
            }
        )
    return rows


def _candidate_rows(producer):
    rows = []
    rows.extend(_critical_stock_candidates(producer))
    rows.extend(_surplus_candidates(producer))
    rows.extend(_need_candidates(producer))
    rows.extend(_sell_suggestion_candidates(producer))
    return rows


def _apply_candidate_to_alert(alert, candidate, *, now, force_active=False):
    update_fields = []

    field_values = {
        "severity": candidate["severity"],
        "title": candidate["title"],
        "description": candidate["description"],
        "source_system": AlertSourceSystem.INTERNAL,
        "payload": candidate["payload"],
        "product": candidate["product"],
        "need": candidate["need"],
        "forecast": candidate["forecast"],
        "listing": candidate["listing"],
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
            title=candidate["title"],
            description=candidate["description"],
            source_system=AlertSourceSystem.INTERNAL,
            status=AlertStatus.ACTIVE,
            payload=candidate["payload"],
            assumed_loss=False,
        )
        record_alert_event(
            alert,
            AlertEventType.CREATED,
            performed_by=acting_user,
            notes="Alerta criado automaticamente",
        )
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
def ignore_alert(alert, user, reason=None, *, queue_badge_update=True):
    if alert.status == AlertStatus.IGNORED:
        return False

    now = timezone.now()
    alert.status = AlertStatus.IGNORED
    alert.ignored_at = now
    alert.cleared_at = None
    alert.ignored_reason = (reason or "").strip() or None
    alert.updated_at = now
    alert.save(update_fields=["status", "ignored_at", "cleared_at", "ignored_reason", "updated_at"])
    record_alert_event(
        alert,
        AlertEventType.IGNORED,
        performed_by=user,
        notes=alert.ignored_reason or "Ignorado manualmente pelo utilizador",
    )
    if queue_badge_update:
        _queue_alerts_badge_changed_for_user(user_id=getattr(user, "id", None))
    return True


@transaction.atomic
def ignore_all_active_alerts(*, producer, user, reason=None):
    if not producer or not user:
        return 0

    active_alerts = list(
        Alert.objects
        .select_for_update()
        .filter(
            producer=producer,
            status=AlertStatus.ACTIVE,
        )
        .order_by("-updated_at", "-created_at")
    )
    if not active_alerts:
        return 0

    ignored_count = 0
    for alert in active_alerts:
        changed = ignore_alert(
            alert,
            user=user,
            reason=reason,
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
    alert.cleared_at = None
    alert.updated_at = now
    alert.save(
        update_fields=[
            "status",
            "ignored_at",
            "ignored_reason",
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
            ignored_at__lte=cutoff,
        )
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
    if alert.status == AlertStatus.RESOLVED:
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


def list_alerts_for_producer(*, producer, tab="active"):
    selected_status = UI_TAB_STATUS_MAP.get(tab, AlertStatus.ACTIVE)
    alerts = list(
        Alert.objects
        .select_related("product", "need", "forecast", "listing")
        .filter(producer=producer, status=selected_status)
        .order_by("-updated_at", "-created_at")
    )

    severity_labels = dict(AlertSeverity.choices)
    for alert in alerts:
        payload = alert.payload or {}
        alert.type_label = get_alert_type_label(alert.type)
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
    return alerts
