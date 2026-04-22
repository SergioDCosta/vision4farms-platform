import logging
import uuid
from urllib.parse import urljoin

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import connection, transaction
from django.db.models import Count, Max
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from apps.accounts.models import AccountStatus, User, UserRole
from apps.inventory.models import ProducerProfile
from apps.support.models import SupportTicket, SupportTicketStatus


logger = logging.getLogger(__name__)
SUPPORT_ADMIN_LAST_SEEN_SESSION_KEY = "support_admin_last_seen_at"


class SupportServiceError(Exception):
    pass


def _parse_session_datetime(value):
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if not parsed:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _build_public_absolute_url(request, relative_path):
    path = str(relative_path or "")
    app_base_url = (getattr(settings, "APP_BASE_URL", "") or "").strip().rstrip("/")
    if app_base_url and not app_base_url.startswith(("http://", "https://")):
        app_base_url = f"https://{app_base_url.lstrip('/')}"
    if app_base_url:
        return urljoin(f"{app_base_url}/", path.lstrip("/"))
    return request.build_absolute_uri(path)


def _send_system_email(*, subject, text_body, html_body, recipient_list):
    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipient_list,
        reply_to=[settings.DEFAULT_REPLY_TO_EMAIL],
    )
    if html_body:
        email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)


def _resolve_support_recipients(ticket):
    admin_emails = []
    admins_qs = User.objects.filter(
        role=UserRole.ADMIN,
        is_active=True,
        account_status=AccountStatus.ACTIVE,
    ).exclude(id=ticket.requester_user_id)

    for admin in admins_qs:
        email = (admin.email or "").strip().lower()
        if email and email not in admin_emails:
            admin_emails.append(email)

    if admin_emails:
        return admin_emails

    fallback_email = (getattr(settings, "SUPPORT_CONTACT_EMAIL", "") or "").strip().lower()
    if fallback_email:
        return [fallback_email]

    return []


def _build_ticket_urls(request, ticket):
    admin_detail_url = _build_public_absolute_url(
        request,
        reverse("support:admin_ticket_detail", kwargs={"ticket_id": ticket.id}),
    )
    settings_url = _build_public_absolute_url(
        request,
        reverse("settings_app:settings_index"),
    )
    return admin_detail_url, settings_url


def _ticket_snapshot(ticket):
    return {
        "id": str(ticket.id),
        "ticket_number": ticket.ticket_number,
        "status": ticket.status,
        "subject": ticket.subject,
        "requester_user_id": str(ticket.requester_user_id),
        "assigned_admin_id": str(ticket.assigned_admin_id) if ticket.assigned_admin_id else None,
        "claimed_at": ticket.claimed_at.isoformat() if ticket.claimed_at else None,
        "admin_replied_at": ticket.admin_replied_at.isoformat() if ticket.admin_replied_at else None,
        "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
    }


def get_admin_support_badge_state(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.ADMIN:
        return {"visible": False, "count": 0, "tone": "orange"}

    aggregate = (
        SupportTicket.objects
        .filter(status=SupportTicketStatus.OPEN)
        .aggregate(
            open_count=Count("id"),
            latest_open_created_at=Max("created_at"),
        )
    )
    open_count = int(aggregate.get("open_count") or 0)
    if open_count <= 0:
        return {"visible": False, "count": 0, "tone": "orange"}

    latest_open_created_at = aggregate.get("latest_open_created_at")
    last_seen_at = _parse_session_datetime(
        request.session.get(SUPPORT_ADMIN_LAST_SEEN_SESSION_KEY)
    )
    has_unseen_new = bool(
        latest_open_created_at and (
            not last_seen_at or latest_open_created_at > last_seen_at
        )
    )
    return {
        "visible": True,
        "count": open_count,
        "tone": "red" if has_unseen_new else "orange",
    }


def mark_admin_support_seen(request):
    user = getattr(request, "current_user", None)
    if not user or getattr(user, "role", None) != UserRole.ADMIN:
        return
    request.session[SUPPORT_ADMIN_LAST_SEEN_SESSION_KEY] = timezone.now().isoformat()
    request.session.modified = True


def create_support_ticket(*, requester_user, subject, message):
    producer_profile = ProducerProfile.objects.filter(user=requester_user).first()
    requester_name = requester_user.full_name or requester_user.email or "Utilizador"
    ticket_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO public.support_tickets (
                id,
                requester_user_id,
                status,
                subject,
                message,
                requester_name_snapshot,
                requester_email_snapshot,
                requester_role_snapshot,
                requester_company_snapshot,
                requester_phone_snapshot
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [
                str(ticket_id),
                str(requester_user.id),
                SupportTicketStatus.OPEN,
                subject,
                message,
                requester_name,
                requester_user.email,
                requester_user.role,
                getattr(producer_profile, "company_name", None),
                getattr(producer_profile, "phone", None),
            ],
        )
        cursor.fetchone()
    ticket = SupportTicket.objects.get(id=ticket_id)
    return ticket


@transaction.atomic
def claim_support_ticket(*, ticket_id, admin_user):
    ticket = (
        SupportTicket.objects.select_for_update()
        .filter(id=ticket_id)
        .first()
    )
    if not ticket:
        raise SupportServiceError("Ticket de suporte não encontrado.")

    if ticket.status != SupportTicketStatus.OPEN:
        raise SupportServiceError("Este ticket já foi aceite por outro administrador.")

    now = timezone.now()
    ticket.assigned_admin = admin_user
    ticket.status = SupportTicketStatus.CLAIMED
    ticket.claimed_at = now
    ticket.updated_at = now
    ticket.save(update_fields=["assigned_admin", "status", "claimed_at", "updated_at"])
    return ticket


@transaction.atomic
def reply_support_ticket(*, ticket_id, admin_user, reply_message):
    ticket = (
        SupportTicket.objects.select_for_update()
        .filter(id=ticket_id)
        .first()
    )
    if not ticket:
        raise SupportServiceError("Ticket de suporte não encontrado.")

    if ticket.status == SupportTicketStatus.CLOSED:
        raise SupportServiceError("Este ticket já está fechado.")

    if ticket.status != SupportTicketStatus.CLAIMED:
        raise SupportServiceError("Só podes responder tickets que já estejam aceites.")

    if ticket.assigned_admin_id != admin_user.id:
        raise SupportServiceError("Este ticket está atribuído a outro administrador.")

    now = timezone.now()
    ticket.admin_reply_message = reply_message
    ticket.admin_replied_at = now
    ticket.status = SupportTicketStatus.CLOSED
    ticket.closed_at = now
    ticket.updated_at = now
    ticket.save(
        update_fields=[
            "admin_reply_message",
            "admin_replied_at",
            "status",
            "closed_at",
            "updated_at",
        ]
    )
    return ticket


def send_support_ticket_created_to_admins(request, ticket):
    recipients = _resolve_support_recipients(ticket)
    if not recipients:
        raise SupportServiceError("Não existem destinatários de suporte configurados.")

    admin_detail_url, _ = _build_ticket_urls(request, ticket)
    context = {
        "ticket": ticket,
        "admin_detail_url": admin_detail_url,
    }
    subject = render_to_string("emails/support_ticket_admin_subject.txt", context).strip()
    text_body = render_to_string("emails/support_ticket_admin.txt", context)
    html_body = render_to_string("emails/support_ticket_admin.html", context)
    _send_system_email(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        recipient_list=recipients,
    )
    logger.info(
        "Support ticket admin notification sent ticket_id=%s ticket_number=%s recipients=%s",
        ticket.id,
        ticket.ticket_number,
        len(recipients),
    )


def send_support_ticket_acknowledgement(request, ticket):
    _, settings_url = _build_ticket_urls(request, ticket)
    context = {
        "ticket": ticket,
        "settings_url": settings_url,
    }
    subject = render_to_string("emails/support_ticket_ack_subject.txt", context).strip()
    text_body = render_to_string("emails/support_ticket_ack.txt", context)
    html_body = render_to_string("emails/support_ticket_ack.html", context)
    _send_system_email(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        recipient_list=[ticket.requester_email_snapshot],
    )


def send_support_ticket_reply_to_requester(request, ticket):
    _, settings_url = _build_ticket_urls(request, ticket)
    context = {
        "ticket": ticket,
        "settings_url": settings_url,
    }
    subject = render_to_string("emails/support_ticket_reply_subject.txt", context).strip()
    text_body = render_to_string("emails/support_ticket_reply.txt", context)
    html_body = render_to_string("emails/support_ticket_reply.html", context)
    _send_system_email(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        recipient_list=[ticket.requester_email_snapshot],
    )


def build_ticket_snapshot(ticket):
    return _ticket_snapshot(ticket)
