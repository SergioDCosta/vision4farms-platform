import logging

from apps.inventory.models import ProducerProfile
from apps.messaging.models import ConversationParticipant, MessageType


logger = logging.getLogger(__name__)


def upsert_message_notifications_for_message(*, conversation, message, sender_user):
    if not conversation or not message or not sender_user:
        return

    try:
        from apps.notifications_app.services import create_message_notification
    except Exception:
        return

    sender_label = (sender_user.full_name or sender_user.email or "Utilizador").strip() or "Utilizador"
    if message.message_type == MessageType.FILE:
        attachment_name = (message.attachment_name or message.content or "anexo").strip()
        preview = f"Enviou um anexo: {attachment_name}"
    else:
        content = (message.content or "").strip()
        preview = content if len(content) <= 120 else f"{content[:117]}..."

    participants = list(
        ConversationParticipant.objects
        .select_related("user")
        .filter(conversation=conversation, user_id__isnull=False)
    )
    recipient_user_ids = {
        participant.user_id
        for participant in participants
        if participant.user_id and participant.user_id != sender_user.id
    }
    if not recipient_user_ids:
        return

    recipient_profiles = ProducerProfile.objects.filter(user_id__in=recipient_user_ids)
    action_url = f"/mensagens/?tab=active&c={conversation.id}"

    for producer in recipient_profiles:
        user = getattr(producer, "user", None)
        if not user:
            continue
        try:
            create_message_notification(
                user=user,
                message=message,
                sender_name=sender_label,
                preview_text=preview,
                action_url=action_url,
            )
        except Exception:
            logger.warning("Falha ao criar notificação de mensagem.", exc_info=True)


def mark_message_notifications_read_for_conversation(*, user, conversation):
    try:
        from apps.notifications_app.services import mark_message_notifications_read_for_conversation as mark_read
    except Exception:
        return

    try:
        mark_read(user=user, conversation=conversation)
    except Exception:
        logger.warning("Falha ao marcar notificações de mensagem como lidas.", exc_info=True)
