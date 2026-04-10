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
    create_file_message,
    delete_conversation_for_user,
    get_conversation_for_user,
    get_conversation_messages,
    get_current_producer_for_user,
    get_or_create_listing_contact_conversation,
    list_conversations_for_user,
    mark_conversation_as_read,
    serialize_message_payload,
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

    requested_conversation_id = (request.GET.get("c") or "").strip()
    listing_context = list_conversations_for_user(request.current_user)
    conversation_entries = listing_context["conversations"]
    total_unread = listing_context["total_unread"]

    active_conversation = None
    if requested_conversation_id:
        active_conversation = get_conversation_for_user(
            user=request.current_user,
            conversation_id=requested_conversation_id,
        )
        if not active_conversation:
            messages.warning(request, "Não foi possível abrir esta conversa.")

    if not active_conversation and conversation_entries:
        active_conversation = get_conversation_for_user(
            user=request.current_user,
            conversation_id=conversation_entries[0]["conversation"].id,
        )

    active_messages = []
    active_entry = None
    if active_conversation:
        mark_conversation_as_read(user=request.current_user, conversation=active_conversation)
        active_messages = get_conversation_messages(conversation=active_conversation)

        active_key = str(active_conversation.id)
        total_unread = 0
        for entry in conversation_entries:
            if str(entry["conversation"].id) == active_key:
                entry["unread_count"] = 0
                active_entry = entry
            total_unread += entry["unread_count"]

    context = {
        "page_title": "Mensagens",
        "conversation_entries": conversation_entries,
        "active_conversation": active_conversation,
        "active_entry": active_entry,
        "active_messages": active_messages,
        "total_unread": total_unread,
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

    target_url = f"{reverse('messaging:index')}?c={conversation.id}"
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
    except Exception:
        return JsonResponse({"ok": False, "error": "Anexo guardado, mas falhou o envio em tempo real."}, status=500)

    return JsonResponse({"ok": True, "message": message_payload}, status=200)


@login_required
@client_only_required
def delete_conversation_view(request, conversation_id):
    if request.method != "POST":
        return redirect("messaging:index")

    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    try:
        delete_result = delete_conversation_for_user(
            user=request.current_user,
            conversation_id=conversation_id,
        )
    except MessagingServiceError as exc:
        messages.error(request, str(exc))
        target_url = reverse("messaging:index")
    except Exception:
        messages.error(request, "Não foi possível eliminar a conversa.")
        target_url = reverse("messaging:index")
    else:
        if delete_result.get("purged"):
            messages.success(request, "Conversa eliminada para todos os participantes.")
        else:
            messages.success(request, "Conversa removida da tua caixa de mensagens.")

        listing_context = list_conversations_for_user(request.current_user)
        target_url = reverse("messaging:index")
        if listing_context["conversations"]:
            first_conversation_id = listing_context["conversations"][0]["conversation"].id
            target_url = f"{target_url}?c={first_conversation_id}"

    if _is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target_url
        return response

    return redirect(target_url)
