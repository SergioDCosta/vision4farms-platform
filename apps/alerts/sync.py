"""Alert domain services: sync."""

import logging
from apps.alerts.models import Alert, AlertEventType, AlertSourceSystem, AlertStatus
from apps.inventory.models import ProducerProfile
from django.db import transaction
from django.utils import timezone
from apps.alerts.constants import ACTIVE_LIKE_ALERT_STATUSES, AUTO_RESOLVED_NOTE, MANAGED_ALERT_TYPES
from apps.alerts.actions import expire_ignored_alerts_for_producer
from apps.alerts.badges import _queue_alerts_badge_changed_for_user
from apps.alerts.candidates import _candidate_rows
from apps.alerts.delivery import _queue_alert_delivery_for_producer
from apps.alerts.events import record_alert_event
from apps.alerts.utils import _alert_context_key

logger = logging.getLogger(__name__)


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
