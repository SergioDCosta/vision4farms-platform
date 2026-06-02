"""Alert domain services: delivery."""

import logging
import threading
from apps.alerts.models import AlertDelivery, AlertDeliveryChannel, AlertDeliveryStatus, AlertSeverity
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from apps.alerts.constants import EMAIL_ALERT_TYPES

logger = logging.getLogger(__name__)


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
