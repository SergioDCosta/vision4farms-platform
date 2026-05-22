from django.db import transaction
from django.db.models import Count, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.accounts.models import AccountStatus
from apps.inventory.models import ProducerProfile
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.messaging.attachments import resolve_attachment_url
from apps.messaging.exceptions import MessagingServiceError
from apps.messaging.models import Conversation, ConversationParticipant, ConversationType, Message, MessageType
from apps.messaging.unread import _get_unread_counts_for_user
from apps.orders.models import Order
from apps.settings_app.models import UserPreference


MESSAGE_TAB_ACTIVE = "active"
MESSAGE_TAB_ARCHIVED = "archived"


def normalize_messages_tab(tab):
    value = str(tab or "").strip().lower()
    if value in {MESSAGE_TAB_ARCHIVED, "arquivadas"}:
        return MESSAGE_TAB_ARCHIVED
    if value in {MESSAGE_TAB_ACTIVE, "ativas"}:
        return MESSAGE_TAB_ACTIVE
    return MESSAGE_TAB_ACTIVE


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


def _resolve_profile_photo_url(photo_path):
    raw_path = str(photo_path or "").strip()
    if not raw_path:
        return ""
    return resolve_attachment_url(raw_path)


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


def list_conversations_for_user(user, *, archived=False):
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
            participants__is_archived=bool(archived),
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
    unread_map = _get_unread_counts_for_user(
        user_id=user.id,
        conversation_ids=conversation_ids,
        archived=archived,
    )
    last_message_map = _get_last_messages_by_conversation(conversations)

    counterpart_user_by_conversation = {}
    counterpart_user_ids = set()
    for conversation in conversations:
        participants = list(conversation.participants.all())
        counterpart = next((p.user for p in participants if p.user_id != user.id), None)
        counterpart_user_by_conversation[str(conversation.id)] = counterpart
        counterpart_id = getattr(counterpart, "id", None)
        if counterpart_id:
            counterpart_user_ids.add(counterpart_id)

    counterpart_photo_map = {}
    if counterpart_user_ids:
        preferences = (
            UserPreference.objects
            .filter(user_id__in=counterpart_user_ids)
            .only("user_id", "profile_photo")
        )
        counterpart_photo_map = {
            str(preference.user_id): _resolve_profile_photo_url(preference.profile_photo)
            for preference in preferences
        }

    entries = []
    total_unread = 0
    for conversation in conversations:
        conv_key = str(conversation.id)
        unread_count = unread_map.get(conv_key, 0)
        total_unread += unread_count
        last_message = last_message_map.get(conv_key)
        counterpart = counterpart_user_by_conversation.get(conv_key)
        counterpart_id = str(getattr(counterpart, "id", "")) if counterpart else ""
        entries.append(
            {
                "conversation": conversation,
                "title": _build_conversation_title(conversation, user),
                "context_label": _build_listing_context_label(conversation),
                "counterpart_name": _counterpart_name(counterpart),
                "counterpart_photo_url": counterpart_photo_map.get(counterpart_id, ""),
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


def get_conversation_for_user(*, user, conversation_id, archived=None):
    queryset = (
        Conversation.objects
        .filter(
            id=conversation_id,
            is_active=True,
            participants__user=user,
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
    if archived is not None:
        queryset = queryset.filter(participants__is_archived=bool(archived))
    return queryset.first()


def is_conversation_archived_for_user(*, user, conversation_id):
    participant = (
        ConversationParticipant.objects
        .filter(
            conversation_id=conversation_id,
            conversation__is_active=True,
            user=user,
        )
        .values("is_archived")
        .first()
    )
    if not participant:
        return None
    return bool(participant["is_archived"])


def get_conversation_messages(*, conversation, limit=150):
    if not conversation:
        return []

    message_queryset = (
        Message.objects
        .select_related("sender_user")
        .filter(conversation=conversation)
        .order_by("-created_at")[:limit]
    )
    messages = list(reversed(list(message_queryset)))
    previous_date = None
    for message in messages:
        if message.message_type == MessageType.FILE:
            message.resolved_attachment_url = resolve_attachment_url(message.attachment_url)
        else:
            message.resolved_attachment_url = ""
        current_date = timezone.localtime(message.created_at).date() if message.created_at else None
        message.show_date_divider = current_date != previous_date
        previous_date = current_date
    return messages


@transaction.atomic
def archive_conversation_for_user(*, user, conversation_id):
    if not user:
        raise MessagingServiceError("Utilizador inválido.")

    participant = (
        ConversationParticipant.objects
        .select_for_update()
        .select_related("conversation")
        .filter(
            conversation_id=conversation_id,
            user=user,
            conversation__is_active=True,
        )
        .first()
    )

    if not participant:
        raise MessagingServiceError("Conversa não encontrada.")

    if participant.is_archived:
        return {"archived": True, "conversation_id": str(participant.conversation_id)}

    participant.is_archived = True
    participant.save(update_fields=["is_archived"])
    return {"archived": True, "conversation_id": str(participant.conversation_id)}


@transaction.atomic
def unarchive_conversation_for_user(*, user, conversation_id):
    if not user:
        raise MessagingServiceError("Utilizador inválido.")

    participant = (
        ConversationParticipant.objects
        .select_for_update()
        .filter(
            conversation_id=conversation_id,
            user=user,
            conversation__is_active=True,
        )
        .first()
    )
    if not participant:
        raise MessagingServiceError("Conversa não encontrada.")

    if not participant.is_archived:
        return {"unarchived": True, "conversation_id": str(participant.conversation_id)}

    participant.is_archived = False
    participant.save(update_fields=["is_archived"])
    return {"unarchived": True, "conversation_id": str(participant.conversation_id)}


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


def _find_order_contact_conversation(*, order, user_a_id, user_b_id):
    return (
        Conversation.objects
        .filter(
            conversation_type=ConversationType.ORDER_CONTACT,
            order=order,
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


def validate_listing_contact_allowed(*, current_user, listing):
    if not isinstance(listing, MarketplaceListing):
        raise MessagingServiceError("Anúncio inválido para iniciar conversa.")
    if not current_user:
        raise MessagingServiceError("Utilizador inválido.")

    if getattr(listing, "need_id", None):
        raise MessagingServiceError("Esta proposta pertence a uma necessidade. Use a página de necessidades.")

    if listing.status != ListingStatus.ACTIVE:
        raise MessagingServiceError("Este anúncio já não está disponível para contacto.")

    if listing.expires_at and listing.expires_at <= timezone.now():
        raise MessagingServiceError("Este anúncio expirou e já não está disponível para contacto.")

    if listing.quantity_available is not None and listing.quantity_available <= 0:
        raise MessagingServiceError("Este anúncio já não tem quantidade disponível para contacto.")

    product = getattr(listing, "product", None)
    if product and not getattr(product, "is_active", True):
        raise MessagingServiceError("Este produto já não está ativo.")

    seller_producer = getattr(listing, "producer", None)
    seller_user = getattr(seller_producer, "user", None)
    if not seller_user:
        raise MessagingServiceError("Não foi possível identificar o produtor deste anúncio.")

    if seller_user.id == current_user.id:
        raise MessagingServiceError("Não pode contactar o seu próprio anúncio.")

    if not getattr(seller_user, "is_active", False) or seller_user.account_status != AccountStatus.ACTIVE:
        raise MessagingServiceError("Este produtor já não está disponível para contacto.")

    return seller_user


@transaction.atomic
def get_or_create_listing_contact_conversation(*, current_user, listing):
    seller_user = validate_listing_contact_allowed(current_user=current_user, listing=listing)

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


@transaction.atomic
def get_or_create_order_contact_conversation(*, current_user, order):
    if not isinstance(order, Order):
        raise MessagingServiceError("Encomenda inválida para iniciar conversa.")

    order = (
        Order.objects
        .select_related("buyer_producer__user")
        .prefetch_related("items__seller_producer__user")
        .filter(id=order.id)
        .first()
    )
    if not order:
        raise MessagingServiceError("Encomenda não encontrada.")

    buyer_user = getattr(getattr(order, "buyer_producer", None), "user", None)
    if not buyer_user:
        raise MessagingServiceError("Não foi possível identificar o comprador desta encomenda.")

    from apps.orders.services import is_order_forecast_only

    seller_users = []
    seen_seller_ids = set()
    for item in order.items.all():
        seller_user = getattr(getattr(item, "seller_producer", None), "user", None)
        seller_user_id = getattr(seller_user, "id", None)
        if not seller_user_id or seller_user_id in seen_seller_ids:
            continue
        seen_seller_ids.add(seller_user_id)
        seller_users.append(seller_user)

    if not is_order_forecast_only(order):
        raise MessagingServiceError("Este chat está disponível apenas para encomendas de pré-venda.")

    if len(seller_users) != 1:
        raise MessagingServiceError("Esta encomenda não é elegível para chat 1:1.")

    seller_user = seller_users[0]
    if current_user.id not in {buyer_user.id, seller_user.id}:
        raise MessagingServiceError("Sem acesso a esta encomenda.")

    counterpart_user = seller_user if current_user.id == buyer_user.id else buyer_user
    existing_conversation = _find_order_contact_conversation(
        order=order,
        user_a_id=current_user.id,
        user_b_id=counterpart_user.id,
    )
    if existing_conversation:
        ConversationParticipant.objects.filter(
            conversation=existing_conversation,
            user=current_user,
        ).update(is_archived=False)
        return existing_conversation, False

    title = f"Encomenda #{order.order_number} — {_counterpart_name(counterpart_user)}"
    conversation = Conversation.objects.create(
        conversation_type=ConversationType.ORDER_CONTACT,
        title=title,
        order=order,
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
        user=counterpart_user,
        is_archived=False,
    )

    return conversation, True
