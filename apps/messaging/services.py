from django.db import transaction
from django.db.models import Count, F, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.accounts.models import User
from apps.inventory.models import ProducerProfile
from apps.marketplace.models import MarketplaceListing
from apps.messaging.models import (
    Conversation,
    ConversationParticipant,
    ConversationType,
    Message,
)


class MessagingServiceError(Exception):
    pass


def get_current_producer_for_user(user):
    if not user:
        return None
    return ProducerProfile.objects.filter(user=user).first()


def _conversation_sort_annotation():
    return Coalesce("last_message_at", "updated_at", "created_at")


def _counterpart_name(user):
    if not user:
        return "Utilizador"
    return user.full_name or user.email or "Utilizador"


def _build_listing_context_label(conversation):
    listing = getattr(conversation, "listing", None)
    if conversation.conversation_type == ConversationType.ORDER_CONTACT:
        return "Contacto de encomenda"
    if conversation.conversation_type != ConversationType.LISTING_CONTACT:
        return "Conversa direta"
    if not listing:
        return "Contacto de anúncio"

    source_label = "Pré-venda" if getattr(listing, "forecast_id", None) else "Disponível agora"
    product_name = getattr(getattr(listing, "product", None), "name", None) or "Produto"
    return f"{product_name} · {source_label}"


def _build_conversation_title(conversation, current_user):
    if conversation.title:
        return conversation.title

    participants = list(getattr(conversation, "participants", []).all())
    counterpart = next((p.user for p in participants if p.user_id != current_user.id), None)
    counterpart_label = _counterpart_name(counterpart)

    if conversation.conversation_type == ConversationType.LISTING_CONTACT:
        listing = getattr(conversation, "listing", None)
        if listing and getattr(listing, "product", None):
            return f"{listing.product.name} — {counterpart_label}"
    return counterpart_label


def _build_preview_text(message):
    if not message:
        return "Sem mensagens ainda."
    content = (message.content or "").strip()
    if not content:
        return "Mensagem sem conteúdo."
    if len(content) <= 90:
        return content
    return f"{content[:87]}..."


def _get_last_messages_by_conversation(conversations):
    last_message_ids = [
        conversation.last_message_id
        for conversation in conversations
        if getattr(conversation, "last_message_id", None)
    ]
    if not last_message_ids:
        return {}

    message_map = {
        message.id: message
        for message in Message.objects.select_related("sender_user").filter(id__in=last_message_ids)
    }
    return {
        str(conversation.id): message_map.get(conversation.last_message_id)
        for conversation in conversations
    }


def _get_unread_counts_for_user(*, user, conversation_ids):
    if not conversation_ids:
        return {}

    unread_rows = (
        Message.objects
        .filter(conversation_id__in=conversation_ids)
        .exclude(sender_user=user)
        .filter(conversation__participants__user=user, conversation__participants__is_archived=False)
        .filter(
            Q(conversation__participants__last_read_at__isnull=True)
            | Q(created_at__gt=F("conversation__participants__last_read_at"))
        )
        .values("conversation_id")
        .annotate(unread_count=Count("id"))
    )
    return {str(row["conversation_id"]): row["unread_count"] for row in unread_rows}


def list_conversations_for_user(user):
    latest_message_id_subquery = (
        Message.objects
        .filter(conversation_id=OuterRef("pk"))
        .order_by("-created_at")
        .values("id")[:1]
    )

    conversation_queryset = (
        Conversation.objects
        .filter(
            is_active=True,
            participants__user=user,
            participants__is_archived=False,
        )
        .select_related("listing__product")
        .prefetch_related(
            Prefetch(
                "participants",
                queryset=ConversationParticipant.objects.select_related("user"),
            )
        )
        .annotate(
            sort_at=_conversation_sort_annotation(),
            last_message_id=Subquery(latest_message_id_subquery),
        )
        .order_by("-sort_at", "-created_at")
        .distinct()
    )

    conversations = list(conversation_queryset)
    conversation_ids = [conversation.id for conversation in conversations]
    unread_map = _get_unread_counts_for_user(user=user, conversation_ids=conversation_ids)
    last_message_map = _get_last_messages_by_conversation(conversations)

    entries = []
    total_unread = 0
    for conversation in conversations:
        conv_key = str(conversation.id)
        unread_count = unread_map.get(conv_key, 0)
        total_unread += unread_count
        last_message = last_message_map.get(conv_key)

        participants = list(conversation.participants.all())
        counterpart = next((p.user for p in participants if p.user_id != user.id), None)
        entries.append(
            {
                "conversation": conversation,
                "title": _build_conversation_title(conversation, user),
                "context_label": _build_listing_context_label(conversation),
                "counterpart_name": _counterpart_name(counterpart),
                "last_message": last_message,
                "preview_text": _build_preview_text(last_message),
                "preview_at": (
                    timezone.localtime(last_message.created_at)
                    if last_message and last_message.created_at
                    else None
                ),
                "unread_count": unread_count,
            }
        )

    return {
        "conversations": entries,
        "total_unread": total_unread,
    }


def get_conversation_for_user(*, user, conversation_id):
    queryset = (
        Conversation.objects
        .filter(
            id=conversation_id,
            is_active=True,
            participants__user=user,
            participants__is_archived=False,
        )
        .select_related("listing__product")
        .prefetch_related(
            Prefetch(
                "participants",
                queryset=ConversationParticipant.objects.select_related("user"),
            )
        )
        .distinct()
    )
    return queryset.first()


def get_conversation_messages(*, conversation, limit=150):
    if not conversation:
        return []

    message_queryset = (
        Message.objects
        .select_related("sender_user")
        .filter(conversation=conversation)
        .order_by("-created_at")[:limit]
    )
    return list(reversed(list(message_queryset)))


def mark_conversation_as_read(*, user, conversation):
    if not user or not conversation:
        return
    ConversationParticipant.objects.filter(
        conversation=conversation,
        user=user,
        is_archived=False,
    ).update(last_read_at=timezone.now())


def _find_listing_contact_conversation(*, listing, user_a_id, user_b_id):
    return (
        Conversation.objects
        .filter(
            conversation_type=ConversationType.LISTING_CONTACT,
            listing=listing,
            is_active=True,
        )
        .annotate(
            participants_count=Count("participants", distinct=True),
            matched_count=Count(
                "participants",
                filter=Q(participants__user_id__in=[user_a_id, user_b_id]),
                distinct=True,
            ),
        )
        .filter(participants_count=2, matched_count=2)
        .order_by("-updated_at")
        .first()
    )


@transaction.atomic
def get_or_create_listing_contact_conversation(*, current_user, listing):
    if not isinstance(listing, MarketplaceListing):
        raise MessagingServiceError("Anúncio inválido para iniciar conversa.")

    seller_producer = getattr(listing, "producer", None)
    seller_user = getattr(seller_producer, "user", None)
    if not seller_user:
        raise MessagingServiceError("Não foi possível identificar o produtor deste anúncio.")

    if seller_user.id == current_user.id:
        raise MessagingServiceError("Não pode contactar o seu próprio anúncio.")

    existing_conversation = _find_listing_contact_conversation(
        listing=listing,
        user_a_id=current_user.id,
        user_b_id=seller_user.id,
    )
    if existing_conversation:
        ConversationParticipant.objects.filter(
            conversation=existing_conversation,
            user=current_user,
        ).update(is_archived=False)
        return existing_conversation, False

    title = f"{listing.product.name} — {_counterpart_name(seller_user)}"
    conversation = Conversation.objects.create(
        conversation_type=ConversationType.LISTING_CONTACT,
        title=title,
        listing=listing,
        created_by=current_user,
        is_active=True,
    )

    ConversationParticipant.objects.create(
        conversation=conversation,
        user=current_user,
        last_read_at=timezone.now(),
        is_archived=False,
    )
    ConversationParticipant.objects.create(
        conversation=conversation,
        user=seller_user,
        is_archived=False,
    )

    return conversation, True
