import re

from django.db import transaction
from django.utils import timezone

from apps.common.formatting import format_quantity
from apps.notifications_app.models import Notification, NotificationType


QUANTITY_WITH_UNIT_RE = re.compile(r"(?<![\w])(\d+(?:[.,]\d{1,3})?)(?=\s*kg\b)")


def _normalize_quantity_text(text):
    if not text:
        return text

    def replace(match):
        return str(format_quantity(match.group(1).replace(",", ".")))

    return QUANTITY_WITH_UNIT_RE.sub(replace, str(text))


def create_notification(
    *,
    user,
    notification_type,
    title,
    body=None,
    action_url=None,
    alert=None,
    order=None,
    message=None,
    recommendation=None,
):
    if not user:
        return None

    return Notification.objects.create(
        user=user,
        alert=alert,
        order=order,
        message=message,
        recommendation=recommendation,
        type=notification_type,
        title=(title or "").strip()[:255] or "Notificação",
        body=_normalize_quantity_text((body or "").strip()) or None,
        action_url=(action_url or "").strip() or None,
        is_read=False,
    )


def create_message_notification(*, user, message, sender_name, preview_text, action_url):
    title = f"Nova mensagem de {(sender_name or 'Utilizador').strip() or 'Utilizador'}"
    return create_notification(
        user=user,
        notification_type=NotificationType.MESSAGE,
        title=title,
        body=(preview_text or "").strip() or "Tem uma nova mensagem.",
        action_url=action_url,
        message=message,
    )


def create_order_update_notification(*, user, order, title, body, action_url):
    return create_notification(
        user=user,
        notification_type=NotificationType.ORDER_UPDATE,
        title=title,
        body=body,
        action_url=action_url,
        order=order,
    )


def create_alert_notification(*, user, alert):
    if not user or not alert:
        return None

    payload = alert.payload or {}
    title = (alert.title or "").strip()[:255] or "Notificação"
    body = _normalize_quantity_text((alert.description or "").strip()) or None
    action_url = (payload.get("action_url") or "").strip() or None

    with transaction.atomic():
        notifications = list(
            Notification.objects
            .select_for_update()
            .filter(user=user, alert=alert, type=NotificationType.ALERT)
            .order_by("-created_at", "-id")
        )
        if notifications:
            notification = notifications[0]
            duplicate_ids = [item.id for item in notifications[1:]]
            if duplicate_ids:
                Notification.objects.filter(id__in=duplicate_ids).delete()

            now = timezone.now()
            notification.title = title
            notification.body = body
            notification.action_url = action_url
            notification.is_read = False
            notification.read_at = None
            notification.created_at = now
            notification.save(update_fields=["title", "body", "action_url", "is_read", "read_at", "created_at"])
            return notification

        return create_notification(
            user=user,
            notification_type=NotificationType.ALERT,
            title=title,
            body=body,
            action_url=action_url,
            alert=alert,
        )


def list_recent_notifications_for_user(*, user, limit=8):
    if not user:
        return []

    notifications = list(
        Notification.objects
        .select_related("alert")
        .filter(user=user)
        .order_by("-created_at")[:limit]
    )
    for notification in notifications:
        if notification.type != NotificationType.ALERT or not notification.alert:
            continue
        payload = notification.alert.payload or {}
        notification.title = notification.alert.title
        notification.body = notification.alert.description
        notification.action_url = payload.get("action_url") or notification.action_url
    for notification in notifications:
        notification.body = _normalize_quantity_text(notification.body)
    return notifications


@transaction.atomic
def clear_recent_notifications_for_user(*, user):
    if not user:
        return 0

    deleted_count, _ = Notification.objects.filter(user=user).delete()
    return deleted_count


@transaction.atomic
def mark_notifications_read_for_user(*, user, notification_ids=None):
    if not user:
        return 0

    qs = Notification.objects.select_for_update().filter(user=user, is_read=False)
    if notification_ids:
        qs = qs.filter(id__in=notification_ids)

    now = timezone.now()
    notifications = list(qs)
    for notification in notifications:
        notification.is_read = True
        notification.read_at = now
        notification.save(update_fields=["is_read", "read_at"])
    return len(notifications)
