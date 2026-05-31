import logging

from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.shortcuts import render, redirect
from django.utils import timezone
from django_ratelimit.decorators import ratelimit

from apps.common.audit import describe_user_agent, log_audit_event
from apps.accounts.forms import (
    LoginForm,
    RegisterForm,
    PasswordResetRequestForm,
    PasswordResetConfirmForm,
    AdminInviteCompleteForm,
)
from apps.accounts.models import User, VerificationPurpose
from apps.accounts.services import (
    create_user_and_profile,
    create_signup_verification_token,
    send_signup_confirmation_email,
    authenticate_user_with_reason,
    LOGIN_DENIAL_ACCOUNT_DISABLED,
    LOGIN_DENIAL_ACCOUNT_NOT_ACTIVE,
    LOGIN_DENIAL_EMAIL_NOT_CONFIRMED,
    login_user_manual,
    logout_user_manual,
    validate_verification_token,
    mark_user_as_verified,
    create_password_reset_token,
    send_password_reset_email,
    send_password_changed_email,
    get_support_contact_email,
    validate_password_reset_token,
    validate_admin_invite_token,
    complete_invited_user_account,
    invalidate_pending_admin_invite_tokens,
)

logger = logging.getLogger(__name__)


def _support_contact_email():
    return get_support_contact_email()


def _support_contact_message(prefix):
    return f"{prefix} Se precisares de ajuda, contacta o suporte em {_support_contact_email()}."


@ratelimit(key="ip", rate="10/5m", method="POST", block=False)
@ratelimit(key="post:email", rate="5/5m", method="POST", block=False)
def login_view(request):
    if request.current_user:
        if request.current_user.role == "ADMIN":
            return redirect("dashboard:gestor")
        return redirect("dashboard:painel")

    form = LoginForm(request.POST or None)

    if request.method == "POST" and getattr(request, "limited", False):
        messages.error(
            request,
            "Demasiadas tentativas de login. Tente novamente dentro de alguns minutos.",
        )
        return render(request, "accounts/login.html", {"form": form})

    if request.method == "POST" and form.is_valid():
        try:
            email = form.cleaned_data["email"]
            password = form.cleaned_data["password"]
            remember_me = form.cleaned_data["remember_me"]

            user, denial_reason = authenticate_user_with_reason(email, password)

            if user is None:
                if denial_reason == LOGIN_DENIAL_EMAIL_NOT_CONFIRMED:
                    messages.error(
                        request,
                        _support_contact_message(
                            "A conta ainda não tem o email confirmado."
                        ),
                    )
                elif denial_reason in {LOGIN_DENIAL_ACCOUNT_DISABLED, LOGIN_DENIAL_ACCOUNT_NOT_ACTIVE}:
                    messages.error(
                        request,
                        _support_contact_message(
                            "A conta encontra-se desativada ou indisponível para login."
                        ),
                    )
                else:
                    messages.error(request, "Credenciais inválidas.")
            else:
                now = timezone.now()
                user.last_login = now
                user.updated_at = now
                user.save(update_fields=["last_login", "updated_at"])

                login_user_manual(request, user, remember_me=remember_me)
                device = describe_user_agent(request.META.get("HTTP_USER_AGENT"))
                log_audit_event(
                    request=request,
                    user=user,
                    action="USER_LOGIN",
                    entity_type="users",
                    entity_id=user.id,
                    notes=f"Início de sessão em {device['label']}.",
                    new_values={
                        "device": device,
                        "remember_me": bool(remember_me),
                    },
                )

                if user.role == "ADMIN":
                    return redirect("dashboard:gestor")
                return redirect("dashboard:painel")
        except Exception:
            logger.exception("Erro inesperado durante o login.")
            messages.error(
                request,
                _support_contact_message(
                    "Não foi possível iniciar sessão de momento por um erro inesperado."
                ),
            )

    return render(request, "accounts/login.html", {"form": form})


@ratelimit(key="ip", rate="5/30m", method="POST", block=False)
def register_view(request):
    form = RegisterForm(request.POST or None)

    if request.method == "POST" and getattr(request, "limited", False):
        messages.error(
            request,
            "Muitas tentativas de registo. Tente novamente dentro de alguns minutos.",
        )
        return render(request, "accounts/register.html", {"form": form})

    if request.method == "POST" and form.is_valid():
        email_delivery_failed = False

        try:
            with transaction.atomic():
                user = create_user_and_profile(form.cleaned_data)
                token = create_signup_verification_token(user)
        except Exception:
            logger.exception("Erro ao criar conta por registo público.")
            messages.error(
                request,
                _support_contact_message(
                    "Não foi possível criar a conta de momento."
                ),
            )
            return render(request, "accounts/register.html", {"form": form})

        try:
            send_signup_confirmation_email(request, user, token, async_send=False)
        except Exception:
            logger.exception(
                "Conta criada, mas falhou o envio do email de confirmação. user_id=%s",
                user.id,
            )
            email_delivery_failed = True

        request.session["registration_email"] = user.email
        request.session["registration_email_delivery_failed"] = email_delivery_failed
        request.session["registration_support_email"] = _support_contact_email()
        return redirect("accounts:register_success")

    return render(request, "accounts/register.html", {"form": form})


def register_success_view(request):
    email = request.session.get("registration_email")
    email_delivery_failed = bool(
        request.session.get("registration_email_delivery_failed")
    )
    support_email = (
        request.session.get("registration_support_email")
        or _support_contact_email()
    )
    return render(
        request,
        "accounts/register_success.html",
        {
            "email": email,
            "email_delivery_failed": email_delivery_failed,
            "support_email": support_email,
        },
    )


def verify_email_view(request, token):
    token_obj = validate_verification_token(token)

    if not token_obj:
        messages.error(request, "O link de confirmação é inválido ou expirou.")
        return redirect("accounts:login")

    if token_obj.purpose == VerificationPurpose.ADMIN_INVITE:
        return redirect("accounts:admin_invite_complete", token=token_obj.token)

    token_obj.used_at = timezone.now()
    token_obj.save(update_fields=["used_at"])

    mark_user_as_verified(token_obj.user)

    messages.success(request, "Conta ativada com sucesso. Já pode iniciar sessão.")
    return redirect("accounts:login")


def admin_invite_complete_view(request, token):
    token_obj = validate_admin_invite_token(token)

    if not token_obj:
        messages.error(request, "O link de convite é inválido ou expirou.")
        return redirect("accounts:login")

    user = token_obj.user
    show_user_type = user.role != "ADMIN"
    invite_payload = token_obj.invite_payload or {}
    form = AdminInviteCompleteForm(
        request.POST or None,
        user_role=user.role,
        user=user,
        initial={
            "first_name": user.first_name,
            "last_name": user.last_name,
            "company": invite_payload.get("company", ""),
            "user_type": invite_payload.get("user_type", ""),
        },
    )

    if request.method == "POST" and form.is_valid():
        complete_invited_user_account(user, form.cleaned_data)

        now = timezone.now()
        invalidate_pending_admin_invite_tokens(user, used_at=now)

        messages.success(request, "Conta ativada com sucesso. Já pode iniciar sessão.")
        return redirect("accounts:login")

    return render(
        request,
        "accounts/admin_invite_complete.html",
        {
            "form": form,
            "invited_email": user.email,
            "token": token,
            "show_user_type": show_user_type,
            "personal_message": invite_payload.get("personal_message", ""),
        },
    )


def logout_view(request):
    logout_user_manual(request)
    return redirect("accounts:login")


@ratelimit(key="ip", rate="10/30m", method="POST", block=False)
@ratelimit(key="post:email", rate="5/30m", method="POST", block=False)
def password_reset_request_view(request):
    form = PasswordResetRequestForm(request.POST or None)

    if request.method == "POST" and getattr(request, "limited", False):
        messages.error(
            request,
            "Demasiadas tentativas de recuperação. Tente novamente dentro de alguns minutos.",
        )
        return render(request, "accounts/password_reset_request.html", {"form": form})

    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].strip().lower()

        user = User.objects.filter(email=email, is_active=True).first()
        if user:
            token = create_password_reset_token(user)
            send_password_reset_email(request, user, token, async_send=True)

        messages.success(
            request,
            "Se existir uma conta com esse email, enviámos um link de recuperação."
        )
        return redirect("accounts:login")

    return render(request, "accounts/password_reset_request.html", {"form": form})


def password_reset_confirm_view(request, token):
    token_obj = validate_password_reset_token(token)

    if not token_obj:
        messages.error(request, "O link de recuperação é inválido ou expirou.")
        return redirect("accounts:login")

    form = PasswordResetConfirmForm(request.POST or None, user=token_obj.user)

    if request.method == "POST" and form.is_valid():
        token_obj.user.password = make_password(form.cleaned_data["password"])
        token_obj.user.updated_at = timezone.now()
        token_obj.user.save(update_fields=["password", "updated_at"])
        send_password_changed_email(request, token_obj.user, async_send=True)
        log_audit_event(
            request=request,
            user=token_obj.user,
            action="USER_PASSWORD_RESET_COMPLETED",
            entity_type="users",
            entity_id=token_obj.user.id,
            notes="Utilizador redefiniu a palavra-passe através da recuperação de conta.",
            new_values={
                "password_reset_completed": True,
                "sessions_invalidated": True,
            },
        )

        token_obj.used_at = timezone.now()
        token_obj.save(update_fields=["used_at"])

        messages.success(request, "Palavra-passe alterada com sucesso.")
        return redirect("accounts:login")

    return render(
        request,
        "accounts/password_reset_confirm.html",
        {
            "form": form,
            "token": token,
        },
    )
