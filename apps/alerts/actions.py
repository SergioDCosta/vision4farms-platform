"""Alert domain services: actions."""

from apps.alerts.models import Alert, AlertEventType, AlertStatus
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from apps.alerts.constants import DEFAULT_SNOOZE_KEY, IGNORED_ALERT_TTL, INTERACTIVE_ALERT_STATUSES, MANAGED_ALERT_TYPES
from apps.alerts.badges import _queue_alerts_badge_changed_for_user
from apps.alerts.events import record_alert_event
from apps.alerts.utils import _get_snooze_option, normalize_alert_category, normalize_alert_type


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
            notes="Alerta adiado expirado automaticamente após o período definido.",
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
