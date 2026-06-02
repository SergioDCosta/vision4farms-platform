"""Alert domain services: badges."""

import logging
from apps.accounts.models import UserRole
from apps.alerts.models import Alert, AlertStatus
from apps.common.dates import parse_session_datetime as _parse_session_datetime
from apps.inventory.models import ProducerProfile
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone
from apps.alerts.constants import ALERTS_BADGE_GROUP_PREFIX, ALERTS_LAST_SEEN_SESSION_KEY

logger = logging.getLogger(__name__)


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
