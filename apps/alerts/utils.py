"""Alert domain services: utils."""

from apps.alerts.models import AlertCategory, AlertType
from apps.common.formatting import format_quantity
from decimal import Decimal
from django.utils import formats
from apps.alerts.constants import DEFAULT_SNOOZE_KEY, SNOOZE_OPTIONS


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
        AlertType.CRITICAL_STOCK: "Défice nos pedidos externos",
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
