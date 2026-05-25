from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.common.decorators import client_only_required
from apps.marketplace.models import MarketplaceListing
from apps.messaging.models import ConversationParticipant, Message, MessageType
from apps.orders.models import Order
from apps.messaging.services import (
    MessagingServiceError,
    MESSAGE_TAB_ACTIVE,
    MESSAGE_TAB_ARCHIVED,
    archive_conversation_for_user,
    create_file_message,
    get_conversation_for_user,
    get_unread_totals_for_user,
    get_conversation_messages,
    get_current_producer_for_user,
    get_or_create_listing_contact_conversation,
    get_or_create_order_contact_conversation,
    is_conversation_archived_for_user,
    list_conversations_for_user,
    mark_conversation_as_read,
    normalize_messages_tab,
    serialize_message_payload,
    unarchive_conversation_for_user,
)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _parse_message_limit(raw_value, *, default=150, maximum=600):
    try:
        value = int(raw_value or default)
    except (TypeError, ValueError):
        value = default
    return min(max(value, 50), maximum)


@client_only_required
def messages_index_view(request):
    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    selected_tab = normalize_messages_tab(request.GET.get("tab"))
    requested_conversation_id = (request.GET.get("c") or "").strip()
    force_list_view = (request.GET.get("view") or "").strip().lower() == "list"
    is_archived_tab = selected_tab == MESSAGE_TAB_ARCHIVED
    message_limit = _parse_message_limit(request.GET.get("message_limit"))

    listing_context = list_conversations_for_user(request.current_user, archived=is_archived_tab)
    conversation_entries = listing_context["conversations"]
    tab_unread_total = listing_context["total_unread"]
    unread_totals = get_unread_totals_for_user(request.current_user)

    active_conversation = None
    if requested_conversation_id:
        active_conversation = get_conversation_for_user(
            user=request.current_user,
            conversation_id=requested_conversation_id,
            archived=None,
        )
        if active_conversation:
            archived_state = is_conversation_archived_for_user(
                user=request.current_user,
                conversation_id=active_conversation.id,
            )
            if archived_state is not None:
                expected_tab = MESSAGE_TAB_ARCHIVED if archived_state else MESSAGE_TAB_ACTIVE
                if expected_tab != selected_tab:
                    selected_tab = expected_tab
                    is_archived_tab = selected_tab == MESSAGE_TAB_ARCHIVED
                    listing_context = list_conversations_for_user(
                        request.current_user,
                        archived=is_archived_tab,
                    )
                    conversation_entries = listing_context["conversations"]
                    tab_unread_total = listing_context["total_unread"]
        else:
            messages.warning(request, "Não foi possível abrir esta conversa.")

    active_messages = []
    active_entry = None
    message_history_has_more = False
    next_message_limit = message_limit
    other_participant_has_read = False
    if active_conversation:
        mark_conversation_as_read(user=request.current_user, conversation=active_conversation)
        active_messages = get_conversation_messages(conversation=active_conversation, limit=message_limit)
        total_message_count = active_conversation.messages.count()
        message_history_has_more = total_message_count > len(active_messages)
        next_message_limit = min(message_limit + 150, total_message_count)

        active_key = str(active_conversation.id)
        tab_unread_total = 0
        for entry in conversation_entries:
            if str(entry["conversation"].id) == active_key:
                entry["unread_count"] = 0
                active_entry = entry
            tab_unread_total += entry["unread_count"]
        unread_totals = get_unread_totals_for_user(request.current_user)

        other_participant = (
            ConversationParticipant.objects
            .filter(conversation=active_conversation)
            .exclude(user=request.current_user)
            .first()
        )
        if other_participant and other_participant.last_read_at:
            my_last_sent = (
                Message.objects
                .filter(conversation=active_conversation, sender_user=request.current_user)
                .order_by("-created_at")
                .values_list("created_at", flat=True)
                .first()
            )
            if my_last_sent and other_participant.last_read_at >= my_last_sent:
                other_participant_has_read = True

    context = {
        "page_title": "Mensagens",
        "selected_tab": selected_tab,
        "conversation_entries": conversation_entries,
        "active_conversation": active_conversation,
        "active_entry": active_entry,
        "active_messages": active_messages,
        "message_history_has_more": message_history_has_more,
        "next_message_limit": next_message_limit,
        "force_list_view": force_list_view,
        "total_unread": tab_unread_total,
        "active_unread_total": unread_totals["active_unread_total"],
        "archived_unread_total": unread_totals["archived_unread_total"],
        "other_participant_has_read": other_participant_has_read,
    }
    return render(request, "messaging/index.html", context)


@client_only_required
def start_listing_contact_view(request, listing_id):
    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    listing = get_object_or_404(
        MarketplaceListing.objects.select_related("producer__user", "product"),
        id=listing_id,
    )

    try:
        conversation, _ = get_or_create_listing_contact_conversation(
            current_user=request.current_user,
            listing=listing,
        )
    except MessagingServiceError as exc:
        messages.error(request, str(exc))
        return redirect("marketplace:detail", listing_id=listing.id)

    target_url = f"{reverse('messaging:index')}?tab={MESSAGE_TAB_ACTIVE}&c={conversation.id}"
    if _is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target_url
        return response

    return redirect(target_url)


@client_only_required
def start_order_contact_view(request, order_id):
    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    order = get_object_or_404(
        Order.objects
        .select_related("buyer_producer__user")
        .prefetch_related("items__seller_producer__user"),
        id=order_id,
    )

    try:
        conversation, _ = get_or_create_order_contact_conversation(
            current_user=request.current_user,
            order=order,
        )
    except MessagingServiceError as exc:
        messages.error(request, str(exc))
        return redirect("orders:detail", order_id=order.id)

    target_url = f"{reverse('messaging:index')}?tab={MESSAGE_TAB_ACTIVE}&c={conversation.id}"
    if _is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target_url
        return response

    return redirect(target_url)


@client_only_required
def upload_attachment_view(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método inválido."}, status=405)

    conversation_id = (request.POST.get("conversation_id") or "").strip()
    if not conversation_id:
        return JsonResponse({"ok": False, "error": "Conversa inválida."}, status=400)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return JsonResponse({"ok": False, "error": "Ficheiro não enviado."}, status=400)

    conversation = get_conversation_for_user(
        user=request.current_user,
        conversation_id=conversation_id,
        archived=None,
    )
    if not conversation:
        return JsonResponse({"ok": False, "error": "Sem acesso à conversa."}, status=403)

    try:
        message = create_file_message(
            conversation=conversation,
            sender_user=request.current_user,
            uploaded_file=uploaded_file,
            broadcast_realtime=True,
        )
        message_payload = serialize_message_payload(message=message)
    except MessagingServiceError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"ok": False, "error": "Não foi possível enviar o anexo."}, status=500)

    realtime_warning = ""
    if not getattr(message, "realtime_dispatched", False):
        realtime_warning = "Anexo enviado. Atualize a conversa se não aparecer imediatamente."

    sender_archived_state = is_conversation_archived_for_user(
        user=request.current_user,
        conversation_id=conversation.id,
    )
    return JsonResponse(
        {
            "ok": True,
            "message": message_payload,
            "conversation_unarchived": sender_archived_state is False,
            "target_tab": MESSAGE_TAB_ACTIVE if sender_archived_state is False else MESSAGE_TAB_ARCHIVED,
            "warning": realtime_warning,
        },
        status=200,
    )


@client_only_required
def archive_conversation_view(request, conversation_id):
    if request.method != "POST":
        return redirect("messaging:index")

    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    try:
        archive_result = archive_conversation_for_user(
            user=request.current_user,
            conversation_id=conversation_id,
        )
    except MessagingServiceError as exc:
        messages.error(request, str(exc))
        target_url = reverse("messaging:index")
    except Exception:
        messages.error(request, "Não foi possível arquivar a conversa.")
        target_url = reverse("messaging:index")
    else:
        if archive_result.get("archived"):
            messages.success(request, "Conversa arquivada com sucesso.")

        listing_context = list_conversations_for_user(request.current_user, archived=False)
        target_url = f"{reverse('messaging:index')}?tab={MESSAGE_TAB_ACTIVE}"
        if listing_context["conversations"]:
            first_conversation_id = listing_context["conversations"][0]["conversation"].id
            target_url = f"{target_url}&c={first_conversation_id}"

    if _is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target_url
        return response

    return redirect(target_url)


@client_only_required
def search_conversation_messages_view(request, conversation_id):
    q = (request.GET.get("q") or "").strip()
    if not q or len(q) < 2:
        return JsonResponse({"ok": True, "results": [], "count": 0})

    conversation = get_conversation_for_user(
        user=request.current_user,
        conversation_id=str(conversation_id),
        archived=None,
    )
    if not conversation:
        return JsonResponse({"ok": False, "error": "Sem acesso."}, status=403)

    results_qs = (
        Message.objects
        .filter(
            conversation=conversation,
            message_type=MessageType.TEXT,
            content__icontains=q,
        )
        .select_related("sender_user")
        .order_by("-created_at")[:25]
    )
    results = [serialize_message_payload(message=m) for m in results_qs]
    return JsonResponse({"ok": True, "results": results, "count": len(results)})


@client_only_required
def unarchive_conversation_view(request, conversation_id):
    if request.method != "POST":
        return redirect("messaging:index")

    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    try:
        result = unarchive_conversation_for_user(
            user=request.current_user,
            conversation_id=conversation_id,
        )
    except MessagingServiceError as exc:
        messages.error(request, str(exc))
        target_url = f"{reverse('messaging:index')}?tab={MESSAGE_TAB_ARCHIVED}"
    except Exception:
        messages.error(request, "Não foi possível desarquivar a conversa.")
        target_url = f"{reverse('messaging:index')}?tab={MESSAGE_TAB_ARCHIVED}"
    else:
        if result.get("unarchived"):
            messages.success(request, "Conversa desarquivada com sucesso.")
        target_url = f"{reverse('messaging:index')}?tab={MESSAGE_TAB_ACTIVE}&c={conversation_id}"

    if _is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target_url
        return response

    return redirect(target_url)
