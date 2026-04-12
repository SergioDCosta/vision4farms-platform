from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.common.decorators import client_only_required, login_required
from apps.marketplace.models import MarketplaceListing
from apps.messaging.services import (
    MessagingServiceError,
    MESSAGE_TAB_ACTIVE,
    MESSAGE_TAB_ARCHIVED,
    archive_conversation_for_user,
    create_file_message,
    get_conversation_for_user,
    get_unread_totals_for_conversation_participants,
    get_unread_totals_for_user,
    get_conversation_messages,
    get_current_producer_for_user,
    get_or_create_listing_contact_conversation,
    is_conversation_archived_for_user,
    list_conversations_for_user,
    mark_conversation_as_read,
    normalize_messages_tab,
    serialize_message_payload,
    unarchive_conversation_for_user,
)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


@login_required
@client_only_required
def messages_index_view(request):
    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    selected_tab = normalize_messages_tab(request.GET.get("tab"))
    requested_conversation_id = (request.GET.get("c") or "").strip()
    is_archived_tab = selected_tab == MESSAGE_TAB_ARCHIVED

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

    if not active_conversation and conversation_entries:
        active_conversation = get_conversation_for_user(
            user=request.current_user,
            conversation_id=conversation_entries[0]["conversation"].id,
            archived=is_archived_tab,
        )

    active_messages = []
    active_entry = None
    if active_conversation:
        mark_conversation_as_read(user=request.current_user, conversation=active_conversation)
        active_messages = get_conversation_messages(conversation=active_conversation)

        active_key = str(active_conversation.id)
        tab_unread_total = 0
        for entry in conversation_entries:
            if str(entry["conversation"].id) == active_key:
                entry["unread_count"] = 0
                active_entry = entry
            tab_unread_total += entry["unread_count"]
        unread_totals = get_unread_totals_for_user(request.current_user)

    context = {
        "page_title": "Mensagens",
        "selected_tab": selected_tab,
        "conversation_entries": conversation_entries,
        "active_conversation": active_conversation,
        "active_entry": active_entry,
        "active_messages": active_messages,
        "total_unread": tab_unread_total,
        "active_unread_total": unread_totals["active_unread_total"],
        "archived_unread_total": unread_totals["archived_unread_total"],
    }
    return render(request, "messaging/index.html", context)


@login_required
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


@login_required
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
        )
        message_payload = serialize_message_payload(message=message)
    except MessagingServiceError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"ok": False, "error": "Não foi possível enviar o anexo."}, status=500)

    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"conversation_{conversation.id}",
                {
                    "type": "message_created",
                    "message": message_payload,
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
    except Exception:
        return JsonResponse({"ok": False, "error": "Anexo guardado, mas falhou o envio em tempo real."}, status=500)

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
        },
        status=200,
    )


@login_required
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


@login_required
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
