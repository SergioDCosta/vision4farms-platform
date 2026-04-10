from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.common.decorators import client_only_required, login_required
from apps.marketplace.models import MarketplaceListing
from apps.messaging.services import (
    MessagingServiceError,
    get_conversation_for_user,
    get_conversation_messages,
    get_current_producer_for_user,
    get_or_create_listing_contact_conversation,
    list_conversations_for_user,
    mark_conversation_as_read,
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
