"""Alert domain services: queries."""

from apps.alerts.models import Alert, AlertCategory, AlertSeverity, AlertStatus, AlertType
from django.db.models import Count, Q
from apps.alerts.constants import ORDER_ALERT_TYPES, UI_TAB_STATUS_MAP
from apps.alerts.utils import get_alert_category_label, get_alert_type_label, normalize_alert_category, normalize_alert_type


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
