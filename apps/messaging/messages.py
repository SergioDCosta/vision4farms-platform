import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.messaging.attachments import build_attachment_path, resolve_attachment_url, validate_attachment
from apps.messaging.exceptions import MessagingServiceError
from apps.messaging.models import ConversationParticipant, Message, MessageType
from apps.messaging.notifications import upsert_message_notifications_for_message
from apps.messaging.unread import get_unread_totals_for_conversation_participants


logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 2000
MESSAGE_RATE_LIMIT_COUNT = 30
MESSAGE_RATE_LIMIT_WINDOW_SECONDS = 60


def validate_text_message_content(content):
    normalized = str(content or "").strip()
    if not normalized:
        raise MessagingServiceError("A mensagem de texto está vazia.")
    if len(normalized) > MAX_MESSAGE_CHARS:
        raise MessagingServiceError(f"A mensagem não pode ter mais de {MAX_MESSAGE_CHARS} caracteres.")
    return normalized


def _enforce_message_rate_limit(*, sender_user, conversation):
    if not sender_user or not conversation:
        return

    cache_key = f"messaging:send-rate:{sender_user.id}:{conversation.id}"
    try:
        cache.add(cache_key, 0, timeout=MESSAGE_RATE_LIMIT_WINDOW_SECONDS)
        sent_count = cache.incr(cache_key)
    except Exception:
        logger.warning("Falha ao validar rate limit de mensagens.", exc_info=True)
        return

    if sent_count > MESSAGE_RATE_LIMIT_COUNT:
        raise MessagingServiceError("Está a enviar mensagens demasiado depressa. Aguarde um momento.")


def ensure_sender_active_participation(*, conversation, sender_user):
    participant = ConversationParticipant.objects.filter(
        conversation=conversation,
        user=sender_user,
    ).first()
    if not participant:
        raise MessagingServiceError("Sem acesso à conversa.")

    if participant.is_archived:
        participant.is_archived = False
        participant.save(update_fields=["is_archived"])


def _unarchive_recipients_after_new_message(*, conversation, sender_user):
    ConversationParticipant.objects.filter(
        conversation=conversation,
        is_archived=True,
    ).exclude(user=sender_user).update(is_archived=False)


def serialize_message_payload(*, message):
    sender_user = getattr(message, "sender_user", None)
    sender_name = (
        (sender_user.full_name or sender_user.email)
        if sender_user else "Utilizador"
    ) or "Utilizador"

    created_at = message.created_at or timezone.now()
    created_local = timezone.localtime(created_at)

    payload = {
        "id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "sender_id": str(message.sender_user_id) if message.sender_user_id else None,
        "sender_name": sender_name,
        "message_type": message.message_type,
        "content": message.content or "",
        "created_at": created_at.isoformat(),
        "created_at_label": created_local.strftime("%d/%m/%Y %H:%M"),
    }

    if message.message_type == MessageType.FILE:
        payload["attachment_url"] = resolve_attachment_url(message.attachment_url)
        payload["attachment_name"] = message.attachment_name
        payload["attachment_type"] = message.attachment_type

    return payload


def _touch_conversation_after_message(*, conversation, message_created_at):
    now = timezone.now()
    conversation.last_message_at = message_created_at or now
    conversation.updated_at = now
    conversation.save(update_fields=["last_message_at", "updated_at"])
    return now


def _mark_sender_read(*, conversation, sender_user, now):
    ConversationParticipant.objects.filter(
        conversation=conversation,
        user=sender_user,
    ).update(last_read_at=now)


def broadcast_message_created(*, conversation, message):
    try:
        channel_layer = get_channel_layer()
    except Exception:
        return False
    if not channel_layer:
        return False

    try:
        async_to_sync(channel_layer.group_send)(
            f"conversation_{conversation.id}",
            {
                "type": "message_created",
                "message": serialize_message_payload(message=message),
            },
        )

        unread_targets = get_unread_totals_for_conversation_participants(conversation=conversation)
        for target in unread_targets:
            async_to_sync(channel_layer.group_send)(
                f"messaging_user_{target['user_id']}",
                {
                    "type": "unread_totals",
                    "active_unread_total": target["active_unread_total"],
                    "archived_unread_total": target["archived_unread_total"],
                },
            )
        return True
    except Exception:
        logger.warning("Falha ao emitir mensagem por WebSocket.", exc_info=True)
        return False


def handle_message_post_send(*, conversation, message, sender_user, broadcast_realtime=False):
    _unarchive_recipients_after_new_message(conversation=conversation, sender_user=sender_user)
    now = _touch_conversation_after_message(
        conversation=conversation,
        message_created_at=message.created_at,
    )
    _mark_sender_read(conversation=conversation, sender_user=sender_user, now=now)
    upsert_message_notifications_for_message(
        conversation=conversation,
        message=message,
        sender_user=sender_user,
    )

    if broadcast_realtime:
        return broadcast_message_created(conversation=conversation, message=message)
    return False


def create_text_message(*, conversation, sender_user, content, broadcast_realtime=False):
    content = validate_text_message_content(content)
    ensure_sender_active_participation(conversation=conversation, sender_user=sender_user)
    _enforce_message_rate_limit(conversation=conversation, sender_user=sender_user)

    message = Message.objects.create(
        conversation=conversation,
        sender_user=sender_user,
        message_type=MessageType.TEXT,
        content=content,
    )
    message.realtime_dispatched = handle_message_post_send(
        conversation=conversation,
        message=message,
        sender_user=sender_user,
        broadcast_realtime=broadcast_realtime,
    )
    return message


@transaction.atomic
def create_file_message(*, conversation, sender_user, uploaded_file, broadcast_realtime=False):
    attachment_name, content_type = validate_attachment(uploaded_file)
    ensure_sender_active_participation(conversation=conversation, sender_user=sender_user)
    _enforce_message_rate_limit(conversation=conversation, sender_user=sender_user)

    storage_path = build_attachment_path(conversation.id, attachment_name)
    saved_path = default_storage.save(storage_path, uploaded_file)
    if not saved_path:
        raise MessagingServiceError("Não foi possível guardar o ficheiro.")

    try:
        message = Message.objects.create(
            conversation=conversation,
            sender_user=sender_user,
            message_type=MessageType.FILE,
            content=attachment_name,
            attachment_url=str(saved_path).lstrip("/"),
            attachment_name=attachment_name,
            attachment_type=content_type,
        )
    except Exception:
        try:
            default_storage.delete(saved_path)
        except Exception:
            pass
        raise

    message.realtime_dispatched = handle_message_post_send(
        conversation=conversation,
        message=message,
        sender_user=sender_user,
        broadcast_realtime=broadcast_realtime,
    )
    return message
