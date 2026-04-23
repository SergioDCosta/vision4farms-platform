import uuid
from pathlib import Path
from urllib.parse import urlparse, unquote

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Count, F, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.inventory.models import ProducerProfile
from apps.marketplace.models import MarketplaceListing
from apps.orders.models import Order
from apps.messaging.models import (
    Conversation,
    ConversationParticipant,
    ConversationType,
    Message,
    MessageType,
)


class MessagingServiceError(Exception):
    pass


MESSAGE_TAB_ACTIVE = "active"
MESSAGE_TAB_ARCHIVED = "archived"


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".txt",
}
ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain",
}


def _normalize_attachment_name(original_name):
    filename = Path(original_name or "").name.strip()
    if not filename:
        return "anexo"
    if len(filename) <= 255:
        return filename

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    max_stem = max(1, 255 - len(suffix))
    return f"{stem[:max_stem]}{suffix}"


def _build_attachment_path(conversation_id, attachment_name):
    return f"messaging/attachments/{conversation_id}/{uuid.uuid4().hex}_{attachment_name}"


def _extract_cloudinary_storage_path(url_value):
    parsed = urlparse(str(url_value or "").strip())
    if "res.cloudinary.com" not in (parsed.netloc or "").lower():
        return None
    if "/upload/" not in parsed.path:
        return None

    suffix = parsed.path.split("/upload/", 1)[1]
    segments = [segment for segment in suffix.split("/") if segment]
    if not segments:
        return None

    # Expected Cloudinary pattern: /upload/<transformations?>/v123456/<public_id>
    public_id_start = 0
    for idx, segment in enumerate(segments):
        if segment.startswith("v") and segment[1:].isdigit():
            public_id_start = idx + 1
            break
    public_id_segments = segments[public_id_start:] if public_id_start < len(segments) else segments
    if not public_id_segments:
        return None
    return unquote("/".join(public_id_segments)).lstrip("/")


def _normalize_attachment_storage_value(stored_value):
    raw = str(stored_value or "").strip()
    if not raw:
        return ""

    lower_raw = raw.lower()
    if lower_raw.startswith("http://") or lower_raw.startswith("https://"):
        extracted = _extract_cloudinary_storage_path(raw)
        return extracted or raw

    media_url = str(getattr(settings, "MEDIA_URL", "") or "").strip()
    if media_url and raw.startswith(media_url):
        raw = raw[len(media_url):]

    return raw.lstrip("/")


def _resolve_attachment_url(stored_value):
    normalized = _normalize_attachment_storage_value(stored_value)
    if not normalized:
        return ""

    lower = normalized.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return normalized

    try:
        return default_storage.url(normalized)
    except Exception:
        media_url = str(getattr(settings, "MEDIA_URL", "/media/") or "/media/")
        return f"{media_url.rstrip('/')}/{normalized.lstrip('/')}"


def normalize_messages_tab(tab):
    value = str(tab or "").strip().lower()
    if value in {MESSAGE_TAB_ARCHIVED, "arquivadas"}:
        return MESSAGE_TAB_ARCHIVED
    if value in {MESSAGE_TAB_ACTIVE, "ativas"}:
        return MESSAGE_TAB_ACTIVE
    return MESSAGE_TAB_ACTIVE


def validate_attachment(uploaded_file):
    if not isinstance(uploaded_file, UploadedFile):
        raise MessagingServiceError("Ficheiro inválido.")

    file_size = getattr(uploaded_file, "size", 0) or 0
    if file_size <= 0:
        raise MessagingServiceError("Ficheiro vazio.")
    if file_size > MAX_ATTACHMENT_BYTES:
        raise MessagingServiceError("Ficheiro demasiado grande. Máximo 10MB.")

    attachment_name = _normalize_attachment_name(uploaded_file.name)
    extension = Path(attachment_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        extension_label = extension or "(sem extensão)"
        raise MessagingServiceError(f"Extensão '{extension_label}' não permitida.")

    content_type = (
        (getattr(uploaded_file, "content_type", None) or "")
        .strip()
        .lower()
        .split(";", 1)[0]
        .strip()
    )
    if not content_type or content_type not in ALLOWED_MIME_TYPES:
        raise MessagingServiceError("Tipo de ficheiro não permitido.")

    return attachment_name, content_type


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


def _ensure_sender_active_participation(*, conversation, sender_user):
    participant = ConversationParticipant.objects.filter(
        conversation=conversation,
        user=sender_user,
    ).first()
    if not participant:
        raise MessagingServiceError("Sem acesso à conversa.")

    if participant.is_archived:
        participant.is_archived = False
        participant.save(update_fields=["is_archived"])


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
        payload["attachment_url"] = _resolve_attachment_url(message.attachment_url)
        payload["attachment_name"] = message.attachment_name
        payload["attachment_type"] = message.attachment_type

    return payload


def create_text_message(*, conversation, sender_user, content):
    content = str(content or "").strip()
    if not content:
        raise MessagingServiceError("A mensagem de texto está vazia.")
    _ensure_sender_active_participation(conversation=conversation, sender_user=sender_user)

    message = Message.objects.create(
        conversation=conversation,
        sender_user=sender_user,
        message_type=MessageType.TEXT,
        content=content,
    )

    now = _touch_conversation_after_message(
        conversation=conversation,
        message_created_at=message.created_at,
    )
    _mark_sender_read(conversation=conversation, sender_user=sender_user, now=now)
    return message


@transaction.atomic
def create_file_message(*, conversation, sender_user, uploaded_file):
    attachment_name, content_type = validate_attachment(uploaded_file)
    _ensure_sender_active_participation(conversation=conversation, sender_user=sender_user)

    storage_path = _build_attachment_path(conversation.id, attachment_name)
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

    now = _touch_conversation_after_message(
        conversation=conversation,
        message_created_at=message.created_at,
    )
    _mark_sender_read(conversation=conversation, sender_user=sender_user, now=now)
    return message


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
    for message in messages:
        if message.message_type == MessageType.FILE:
            message.resolved_attachment_url = _resolve_attachment_url(message.attachment_url)
        else:
            message.resolved_attachment_url = ""
    return messages


def mark_conversation_as_read(*, user, conversation):
    if not user or not conversation:
        return
    ConversationParticipant.objects.filter(
        conversation=conversation,
        user=user,
    ).update(last_read_at=timezone.now())


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
