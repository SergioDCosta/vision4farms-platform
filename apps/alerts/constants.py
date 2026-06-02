"""Constants shared by alert domain services."""



from apps.alerts.models import AlertStatus, AlertType

from datetime import timedelta





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

EMAIL_ALERT_TYPES = {
    AlertType.CRITICAL_STOCK,
    AlertType.NEED_RESPONSE_RECEIVED,
    AlertType.NEED_DEADLINE_APPROACHING,
    AlertType.NEED_UNDERCOVERED,
    AlertType.ORDER_REQUIRES_CONFIRMATION,
    AlertType.ORDER_DELIVERY_OVERDUE,
}
