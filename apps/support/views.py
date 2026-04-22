import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from apps.common.decorators import admin_required, login_required
from apps.dashboard.models import AuditLog
from apps.support.forms import SupportTicketCreateForm, SupportTicketReplyForm
from apps.support.models import SupportTicket, SupportTicketStatus
from apps.support.services import (
    SupportServiceError,
    build_ticket_snapshot,
    claim_support_ticket,
    create_support_ticket,
    get_admin_support_badge_state,
    mark_admin_support_seen,
    reply_support_ticket,
    send_support_ticket_acknowledgement,
    send_support_ticket_created_to_admins,
    send_support_ticket_reply_to_requester,
)
from apps.support.consumers import SUPPORT_ADMIN_BADGE_GROUP


logger = logging.getLogger(__name__)


def _get_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _log_support_action(
    *,
    request,
    action,
    ticket,
    notes,
    old_values=None,
    new_values=None,
):
    AuditLog.objects.create(
        user=request.current_user,
        action=action,
        entity_type="support_tickets",
        entity_id=ticket.id if ticket else None,
        old_values=old_values,
        new_values=new_values,
        ip_address=_get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT"),
        notes=notes,
    )


def _support_rate_limit_key(group, request):
    user = getattr(request, "current_user", None)
    if user and getattr(user, "id", None):
        return f"user:{user.id}"
    return f"ip:{_get_client_ip(request) or 'unknown'}"


def _redirect_to_settings(request):
    next_url = (request.POST.get("next") or "").strip()
    if next_url:
        return redirect(next_url)
    return redirect("settings_app:settings_index")


def _broadcast_support_badge_changed():
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            SUPPORT_ADMIN_BADGE_GROUP,
            {"type": "support_badge_changed"},
        )
    except Exception:
        logger.exception("Falha ao emitir atualização realtime do badge de suporte.")


@login_required
@require_POST
@ratelimit(key=_support_rate_limit_key, rate="5/30m", method="POST", block=False)
def support_ticket_create_view(request):
    if getattr(request, "limited", False):
        messages.error(
            request,
            "Demasiados pedidos de suporte em pouco tempo. Tenta novamente daqui a alguns minutos.",
        )
        return _redirect_to_settings(request)

    form = SupportTicketCreateForm(request.POST or None)
    if not form.is_valid():
        errors = []
        for field_errors in form.errors.values():
            errors.extend(field_errors)
        messages.error(
            request,
            errors[0] if errors else "Não foi possível enviar o pedido de suporte.",
        )
        return _redirect_to_settings(request)

    subject = form.cleaned_data["subject"]
    body = form.cleaned_data["message"]
    ticket = create_support_ticket(
        requester_user=request.current_user,
        subject=subject,
        message=body,
    )

    _log_support_action(
        request=request,
        action="SUPPORT_TICKET_CREATED",
        ticket=ticket,
        notes=f"Pedido de suporte #{ticket.ticket_number} criado por {ticket.requester_email_snapshot}.",
        new_values=build_ticket_snapshot(ticket),
    )
    _broadcast_support_badge_changed()

    email_failures = []
    try:
        send_support_ticket_created_to_admins(request, ticket)
    except Exception:
        logger.exception("Falha no envio do ticket de suporte para admins ticket_id=%s", ticket.id)
        email_failures.append("Não foi possível notificar os administradores por email.")

    try:
        send_support_ticket_acknowledgement(request, ticket)
    except Exception:
        logger.exception("Falha no envio do ACK de suporte para utilizador ticket_id=%s", ticket.id)
        email_failures.append("Não foi possível enviar o email de confirmação para a tua conta.")

    messages.success(request, f"Pedido enviado com sucesso. Ticket #{ticket.ticket_number}.")
    if email_failures:
        messages.warning(request, " ".join(email_failures))

    return _redirect_to_settings(request)


@admin_required
def admin_support_tickets_view(request):
    if request.method == "GET":
        mark_admin_support_seen(request)

    q = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip().upper()
    allowed_statuses = {SupportTicketStatus.OPEN, SupportTicketStatus.CLAIMED, SupportTicketStatus.CLOSED}
    if status_filter not in allowed_statuses:
        status_filter = ""

    tickets = SupportTicket.objects.select_related("requester_user", "assigned_admin").order_by("-created_at")
    if status_filter:
        tickets = tickets.filter(status=status_filter)

    if q:
        search_filters = (
            Q(subject__icontains=q)
            | Q(message__icontains=q)
            | Q(requester_name_snapshot__icontains=q)
            | Q(requester_email_snapshot__icontains=q)
            | Q(requester_company_snapshot__icontains=q)
        )
        if q.isdigit():
            search_filters = search_filters | Q(ticket_number=int(q))
        tickets = tickets.filter(search_filters)

    context = {
        "admin_tab": "suporte",
        "tickets": tickets[:200],
        "q": q,
        "selected_status": status_filter,
        "status_choices": SupportTicketStatus.choices,
    }
    if request.htmx and request.headers.get("HX-Target") == "support-tickets-table":
        return render(request, "dashboard/admin/partials/support_tickets_table.html", context)
    return render(request, "dashboard/admin/support_tickets.html", context)


@admin_required
def admin_support_ticket_detail_view(request, ticket_id):
    if request.method == "GET":
        mark_admin_support_seen(request)

    ticket = get_object_or_404(
        SupportTicket.objects.select_related("requester_user", "assigned_admin"),
        id=ticket_id,
    )
    context = {
        "admin_tab": "suporte",
        "ticket": ticket,
        "reply_form": SupportTicketReplyForm(),
    }
    return render(request, "dashboard/admin/support_ticket_detail.html", context)


@admin_required
def admin_support_sidebar_state_view(request):
    return JsonResponse(get_admin_support_badge_state(request))


@admin_required
@require_POST
def admin_support_ticket_claim_view(request, ticket_id):
    ticket_before = SupportTicket.objects.filter(id=ticket_id).first()
    old_values = build_ticket_snapshot(ticket_before) if ticket_before else None

    try:
        ticket = claim_support_ticket(ticket_id=ticket_id, admin_user=request.current_user)
    except SupportServiceError as exc:
        messages.error(request, str(exc))
        return redirect("support:admin_ticket_detail", ticket_id=ticket_id)

    _log_support_action(
        request=request,
        action="SUPPORT_TICKET_CLAIMED",
        ticket=ticket,
        notes=f"Ticket #{ticket.ticket_number} aceite por {request.current_user.email}.",
        old_values=old_values,
        new_values=build_ticket_snapshot(ticket),
    )
    _broadcast_support_badge_changed()
    messages.success(request, f"Ticket #{ticket.ticket_number} aceite com sucesso.")
    return redirect("support:admin_ticket_detail", ticket_id=ticket.id)


@admin_required
@require_POST
def admin_support_ticket_reply_view(request, ticket_id):
    form = SupportTicketReplyForm(request.POST or None)
    if not form.is_valid():
        errors = []
        for field_errors in form.errors.values():
            errors.extend(field_errors)
        messages.error(
            request,
            errors[0] if errors else "Resposta inválida para o ticket.",
        )
        return redirect("support:admin_ticket_detail", ticket_id=ticket_id)

    ticket_before = SupportTicket.objects.filter(id=ticket_id).first()
    old_values = build_ticket_snapshot(ticket_before) if ticket_before else None

    try:
        ticket = reply_support_ticket(
            ticket_id=ticket_id,
            admin_user=request.current_user,
            reply_message=form.cleaned_data["reply_message"],
        )
    except SupportServiceError as exc:
        messages.error(request, str(exc))
        return redirect("support:admin_ticket_detail", ticket_id=ticket_id)

    new_values = build_ticket_snapshot(ticket)
    _log_support_action(
        request=request,
        action="SUPPORT_TICKET_REPLIED",
        ticket=ticket,
        notes=f"Administrador respondeu ao ticket #{ticket.ticket_number}.",
        old_values=old_values,
        new_values=new_values,
    )
    _log_support_action(
        request=request,
        action="SUPPORT_TICKET_CLOSED",
        ticket=ticket,
        notes=f"Ticket #{ticket.ticket_number} fechado automaticamente após resposta.",
        old_values=old_values,
        new_values=new_values,
    )

    try:
        send_support_ticket_reply_to_requester(request, ticket)
    except Exception:
        logger.exception("Falha no envio da resposta de suporte por email ticket_id=%s", ticket.id)
        messages.warning(
            request,
            "Ticket fechado com sucesso, mas falhou o envio do email para o utilizador.",
        )
    else:
        messages.success(request, f"Resposta enviada e ticket #{ticket.ticket_number} fechado.")

    _broadcast_support_badge_changed()

    return redirect("support:admin_ticket_detail", ticket_id=ticket.id)
