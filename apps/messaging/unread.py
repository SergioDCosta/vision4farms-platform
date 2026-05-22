from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from apps.messaging.models import ConversationParticipant, Message
from apps.messaging.notifications import mark_message_notifications_read_for_conversation


def _get_unread_counts_for_user(*, user_id, conversation_ids, archived):
    if not conversation_ids:
        return {}

    unread_rows = (
        Message.objects
        .filter(conversation_id__in=conversation_ids)
        .exclude(sender_user_id=user_id)
        .filter(
            conversation__participants__user_id=user_id,
            conversation__participants__is_archived=bool(archived),
        )
        .filter(
            Q(conversation__participants__last_read_at__isnull=True)
            | Q(created_at__gt=F("conversation__participants__last_read_at"))
        )
        .values("conversation_id")
        .annotate(unread_count=Count("id"))
    )
    return {str(row["conversation_id"]): row["unread_count"] for row in unread_rows}


def _get_unread_totals_grouped_by_user(*, user_ids, archived):
    if not user_ids:
        return {}

    rows = (
        Message.objects
        .filter(
            conversation__participants__user_id__in=user_ids,
            conversation__participants__is_archived=bool(archived),
        )
        .exclude(sender_user_id=F("conversation__participants__user_id"))
        .filter(
            Q(conversation__participants__last_read_at__isnull=True)
            | Q(created_at__gt=F("conversation__participants__last_read_at"))
        )
        .values("conversation__participants__user_id")
        .annotate(unread_count=Count("id"))
    )
    return {
        str(row["conversation__participants__user_id"]): int(row["unread_count"] or 0)
        for row in rows
    }


def get_unread_totals_for_user(user):
    if not user:
        return {
            "active_unread_total": 0,
            "archived_unread_total": 0,
        }

    totals_by_user = get_unread_totals_for_user_ids([user.id])
    return totals_by_user.get(
        str(user.id),
        {"active_unread_total": 0, "archived_unread_total": 0},
    )


def get_client_messages_badge_state(user):
    totals = get_unread_totals_for_user(user)
    count = int(totals.get("active_unread_total") or 0)
    return {
        "visible": count > 0,
        "count": count,
        "tone": "orange",
    }


def get_unread_totals_for_user_ids(user_ids):
    normalized_ids = []
    seen = set()
    for user_id in user_ids or []:
        key = str(user_id)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized_ids.append(user_id)

    if not normalized_ids:
        return {}

    active_map = _get_unread_totals_grouped_by_user(
        user_ids=normalized_ids,
        archived=False,
    )
    archived_map = _get_unread_totals_grouped_by_user(
        user_ids=normalized_ids,
        archived=True,
    )

    totals = {}
    for user_id in normalized_ids:
        key = str(user_id)
        totals[key] = {
            "active_unread_total": int(active_map.get(key, 0)),
            "archived_unread_total": int(archived_map.get(key, 0)),
        }
    return totals


def get_unread_totals_for_conversation_participants(*, conversation):
    if not conversation:
        return []

    participant_ids = list(
        ConversationParticipant.objects
        .filter(conversation=conversation, user_id__isnull=False)
        .values_list("user_id", flat=True)
        .distinct()
    )
    totals_by_user = get_unread_totals_for_user_ids(participant_ids)
    results = []
    for user_id in participant_ids:
        user_key = str(user_id)
        totals = totals_by_user.get(
            user_key,
            {"active_unread_total": 0, "archived_unread_total": 0},
        )
        results.append(
            {
                "user_id": user_key,
                "active_unread_total": totals["active_unread_total"],
                "archived_unread_total": totals["archived_unread_total"],
            }
        )
    return results


def broadcast_unread_totals_for_user_ids(user_ids):
    totals_by_user = get_unread_totals_for_user_ids(user_ids or [])
    if not totals_by_user:
        return False

    try:
        channel_layer = get_channel_layer()
    except Exception:
        return False
    if not channel_layer:
        return False

    dispatched = False
    for user_id, totals in totals_by_user.items():
        try:
            async_to_sync(channel_layer.group_send)(
                f"messaging_user_{user_id}",
                {
                    "type": "unread_totals",
                    "active_unread_total": int(totals.get("active_unread_total") or 0),
                    "archived_unread_total": int(totals.get("archived_unread_total") or 0),
                },
            )
            dispatched = True
        except Exception:
            continue
    return dispatched


def mark_conversation_as_read(*, user, conversation):
    if not user or not conversation:
        return False

    participant = ConversationParticipant.objects.filter(
        conversation=conversation,
        user=user,
    ).first()
    if not participant:
        return False

    last_read_at = getattr(participant, "last_read_at", None)
    had_unread = (
        Message.objects
        .filter(conversation=conversation)
        .exclude(sender_user=user)
        .filter(
            Q(created_at__gt=last_read_at) if last_read_at else Q(created_at__isnull=False)
        )
        .exists()
    )

    now = timezone.now()
    ConversationParticipant.objects.filter(
        conversation=conversation,
        user=user,
    ).update(last_read_at=now)

    mark_message_notifications_read_for_conversation(user=user, conversation=conversation)

    if had_unread:
        transaction.on_commit(
            lambda: broadcast_unread_totals_for_user_ids([user.id])
        )
    return had_unread
