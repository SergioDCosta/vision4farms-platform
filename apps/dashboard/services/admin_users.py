import logging
from dataclasses import dataclass

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import (
    AccountStatus,
    AccountVerificationToken,
    RegistrationSource,
    User,
    UserRole,
    VerificationPurpose,
)
from apps.accounts.services import (
    create_admin_invite_token,
    revoke_pending_admin_invite_tokens,
    send_admin_invite_email,
)
from apps.common.audit import log_audit_event
from apps.dashboard.models import AuditLog
from apps.dashboard.services.admin_audit import build_user_activity_rows
from apps.inventory.models import ProducerProfile


logger = logging.getLogger(__name__)


@dataclass
class AdminUserActionResult:
    ok: bool
    message: str
    user: User
    action: str = ""


def user_snapshot(user, producer_profile=None):
    return {
        "id": str(user.id),
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "role": user.role,
        "registration_source": user.registration_source,
        "account_status": user.account_status,
        "email_verified_at": user.email_verified_at.isoformat()
        if user.email_verified_at
        else None,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "company_name": producer_profile.company_name if producer_profile else None,
        "user_type": getattr(producer_profile, "user_type", None)
        if producer_profile
        else None,
    }


def log_admin_action(
    *,
    request,
    action,
    entity_type,
    entity_id=None,
    notes=None,
    old_values=None,
    new_values=None,
):
    return log_audit_event(
        request=request,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_values=old_values,
        new_values=new_values,
        notes=notes,
    )


def build_admin_users_context(*, q="", page_number=None):
    q = (q or "").strip()
    users_qs = User.objects.all().order_by("-created_at")

    if q:
        users_qs = users_qs.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(email__icontains=q)
            | Q(role__icontains=q)
        )

    paginator = Paginator(users_qs, 10)
    return {
        "admin_tab": "utilizadores",
        "page_obj": paginator.get_page(page_number),
        "q": q,
    }


def build_admin_user_detail_context(user_obj):
    producer_profile = ProducerProfile.objects.filter(user=user_obj).first()
    invitation = build_admin_invitation_context(user_obj)
    related_logs = (
        AuditLog.objects.filter(Q(entity_type="users", entity_id=user_obj.id) | Q(user=user_obj))
        .select_related("user")
        .order_by("-created_at")[:50]
    )
    return {
        "admin_tab": "utilizadores",
        "user_obj": user_obj,
        "producer_profile": producer_profile,
        "invitation": invitation,
        "related_logs": related_logs,
        "related_activity_rows": build_user_activity_rows(related_logs),
    }


def _build_invite_payload(cleaned_data):
    return {
        "company": (cleaned_data.get("company") or "").strip(),
        "user_type": cleaned_data.get("user_type") or "",
        "personal_message": (cleaned_data.get("personal_message") or "").strip(),
    }


def _latest_admin_invite_token(user_obj):
    return (
        AccountVerificationToken.objects.filter(
            user=user_obj,
            purpose=VerificationPurpose.ADMIN_INVITE,
        )
        .select_related("revoked_by_user")
        .order_by("-created_at")
        .first()
    )


def build_admin_invitation_context(user_obj):
    if user_obj.registration_source != RegistrationSource.ADMIN_CREATED:
        return None

    token = _latest_admin_invite_token(user_obj)
    if user_obj.account_status == AccountStatus.ACTIVE:
        return {
            "status": "accepted",
            "label": "Aceite",
            "tone": "green",
            "token": token,
        }
    if not token:
        return {
            "status": "missing",
            "label": "Sem convite ativo",
            "tone": "grey",
            "token": None,
        }
    if token.revoked_at:
        return {
            "status": "revoked",
            "label": "Revogado",
            "tone": "red",
            "token": token,
        }
    if token.used_at:
        return {
            "status": "used",
            "label": "Utilizado",
            "tone": "grey",
            "token": token,
        }
    if token.expires_at < timezone.now():
        return {
            "status": "expired",
            "label": "Expirado",
            "tone": "amber",
            "token": token,
        }
    if not token.sent_at:
        return {
            "status": "delivery_failed",
            "label": "Envio por confirmar",
            "tone": "amber",
            "token": token,
        }
    return {
        "status": "pending",
        "label": "Pendente",
        "tone": "amber",
        "token": token,
    }


def create_invited_user_from_admin_form(*, request, form):
    try:
        with transaction.atomic():
            role = form.cleaned_data["role"]
            user = User.objects.create(
                email=form.cleaned_data["email"],
                password="",
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
                role=role,
                registration_source=RegistrationSource.ADMIN_CREATED,
                account_status=AccountStatus.PENDING_EMAIL_CONFIRMATION,
                is_active=False,
                is_staff=(role == UserRole.ADMIN),
            )
            verification = create_admin_invite_token(
                user,
                invite_payload=_build_invite_payload(form.cleaned_data),
                revoked_by_user=request.current_user,
            )
            log_admin_action(
                request=request,
                action="USER_INVITED",
                entity_type="users",
                entity_id=user.id,
                notes=f"Administrador convidou utilizador {user.email}.",
                new_values=user_snapshot(user),
            )

            def send_invite():
                try:
                    send_admin_invite_email(request, user, verification)
                except Exception:
                    logger.exception("Falha ao enviar convite admin user_id=%s", user.id)
                    messages.warning(
                        request,
                        "O utilizador foi criado, mas falhou o envio do email de convite.",
                    )

            transaction.on_commit(send_invite)
            return user
    except IntegrityError:
        form.add_error("email", "Este email já está registado.")
        return None


def resend_admin_invite(*, request, user_obj):
    if user_obj.registration_source != RegistrationSource.ADMIN_CREATED:
        return AdminUserActionResult(
            ok=False,
            message="Este utilizador não foi criado por convite administrativo.",
            user=user_obj,
        )
    if user_obj.account_status != AccountStatus.PENDING_EMAIL_CONFIRMATION:
        return AdminUserActionResult(
            ok=False,
            message="Esta conta já não está pendente de ativação.",
            user=user_obj,
        )

    latest_token = _latest_admin_invite_token(user_obj)
    verification = create_admin_invite_token(
        user_obj,
        invite_payload=(latest_token.invite_payload if latest_token else {}),
        revoked_by_user=request.current_user,
    )
    try:
        send_admin_invite_email(request, user_obj, verification)
    except Exception:
        logger.exception("Falha ao reenviar convite admin user_id=%s", user_obj.id)
        return AdminUserActionResult(
            ok=False,
            message="O novo convite foi criado, mas falhou o envio do email.",
            user=user_obj,
        )

    log_admin_action(
        request=request,
        action="USER_INVITE_RESENT",
        entity_type="users",
        entity_id=user_obj.id,
        notes=f"Administrador reenviou convite para {user_obj.email}.",
        new_values={"invite_token_id": str(verification.id)},
    )
    return AdminUserActionResult(
        ok=True,
        message="Convite reenviado com sucesso.",
        user=user_obj,
        action="resent",
    )


def revoke_admin_invite(*, request, user_obj):
    if user_obj.registration_source != RegistrationSource.ADMIN_CREATED:
        return AdminUserActionResult(
            ok=False,
            message="Este utilizador não foi criado por convite administrativo.",
            user=user_obj,
        )
    if user_obj.account_status != AccountStatus.PENDING_EMAIL_CONFIRMATION:
        return AdminUserActionResult(
            ok=False,
            message="Esta conta já não está pendente de ativação.",
            user=user_obj,
        )

    revoked_count = revoke_pending_admin_invite_tokens(
        user_obj,
        revoked_by_user=request.current_user,
    )
    if not revoked_count:
        return AdminUserActionResult(
            ok=False,
            message="Não existe um convite ativo para revogar.",
            user=user_obj,
        )

    log_admin_action(
        request=request,
        action="USER_INVITE_REVOKED",
        entity_type="users",
        entity_id=user_obj.id,
        notes=f"Administrador revogou convite de {user_obj.email}.",
        new_values={"revoked": True},
    )
    return AdminUserActionResult(
        ok=True,
        message="Convite revogado com sucesso.",
        user=user_obj,
        action="revoked",
    )


def confirm_user_email_by_admin(*, request, user_obj, justification):
    if request.current_user and request.current_user.id == user_obj.id:
        return AdminUserActionResult(
            ok=False,
            message="Não pode confirmar manualmente a sua própria conta.",
            user=user_obj,
        )

    if user_obj.account_status != AccountStatus.PENDING_EMAIL_CONFIRMATION:
        return AdminUserActionResult(
            ok=False,
            message="Esta conta já não está pendente de confirmação por email.",
            user=user_obj,
        )

    producer_profile = ProducerProfile.objects.filter(user=user_obj).first()
    old_snapshot = user_snapshot(user_obj, producer_profile)
    now = timezone.now()

    with transaction.atomic():
        user_obj.email_verified_at = now
        user_obj.updated_at = now
        if user_obj.registration_source == RegistrationSource.ADMIN_CREATED:
            user_obj.save(update_fields=["email_verified_at", "updated_at"])
        else:
            user_obj.account_status = AccountStatus.ACTIVE
            user_obj.is_active = True
            user_obj.save(
                update_fields=["email_verified_at", "account_status", "is_active", "updated_at"]
            )
            AccountVerificationToken.objects.filter(
                user=user_obj,
                purpose__in=[
                    VerificationPurpose.SIGNUP_CONFIRMATION,
                    VerificationPurpose.ADMIN_INVITE,
                ],
                used_at__isnull=True,
            ).update(used_at=now)

    user_obj.refresh_from_db()
    log_admin_action(
        request=request,
        action="USER_EMAIL_CONFIRMED_BY_ADMIN",
        entity_type="users",
        entity_id=user_obj.id,
        notes=(
            f"Administrador confirmou manualmente o email de {user_obj.email}. "
            f"Justificação: {justification}"
        ),
        old_values=old_snapshot,
        new_values=user_snapshot(user_obj, producer_profile),
    )

    return AdminUserActionResult(
        ok=True,
        message=(
            "Email confirmado manualmente. O utilizador ainda deve concluir o convite "
            "e definir a sua palavra-passe."
            if user_obj.registration_source == RegistrationSource.ADMIN_CREATED
            else "Conta confirmada manualmente com sucesso."
        ),
        user=user_obj,
        action="confirmed",
    )


def toggle_user_status_by_admin(*, request, user_obj):
    if user_obj.id == request.current_user.id:
        return AdminUserActionResult(
            ok=False,
            message="Não pode suspender ou reativar a sua própria conta.",
            user=user_obj,
        )

    if (
        not user_obj.is_active
        and user_obj.account_status == AccountStatus.PENDING_EMAIL_CONFIRMATION
    ):
        return AdminUserActionResult(
            ok=False,
            message=(
                "Esta conta está pendente de confirmação de email. "
                "Só ficará ativa depois do utilizador confirmar a conta."
            ),
            user=user_obj,
        )

    producer_profile = ProducerProfile.objects.filter(user=user_obj).first()
    old_snapshot = user_snapshot(user_obj, producer_profile)
    now = timezone.now()

    if user_obj.is_active:
        user_obj.is_active = False
        if user_obj.account_status == AccountStatus.ACTIVE:
            user_obj.account_status = AccountStatus.SUSPENDED
        action = "USER_SUSPENDED"
        note = f"Administrador suspendeu utilizador {user_obj.email}."
        success_msg = "Utilizador suspenso com sucesso."
        result_action = "suspended"
    else:
        user_obj.is_active = True
        if user_obj.account_status == AccountStatus.SUSPENDED:
            user_obj.account_status = AccountStatus.ACTIVE
        action = "USER_REACTIVATED"
        note = f"Administrador reativou utilizador {user_obj.email}."
        success_msg = "Utilizador reativado com sucesso."
        result_action = "reactivated"

    user_obj.updated_at = now
    user_obj.save(update_fields=["is_active", "account_status", "updated_at"])

    log_admin_action(
        request=request,
        action=action,
        entity_type="users",
        entity_id=user_obj.id,
        notes=note,
        old_values=old_snapshot,
        new_values=user_snapshot(user_obj, producer_profile),
    )

    return AdminUserActionResult(
        ok=True,
        message=success_msg,
        user=user_obj,
        action=result_action,
    )
