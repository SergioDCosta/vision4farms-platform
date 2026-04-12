from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.shortcuts import render, redirect
from django.utils import timezone
from django_ratelimit.decorators import ratelimit

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
    authenticate_user_by_email,
    login_user_manual,
    logout_user_manual,
    validate_verification_token,
    mark_user_as_verified,
    create_password_reset_token,
    send_password_reset_email,
    validate_password_reset_token,
    validate_admin_invite_token,
    complete_invited_user_account,
)


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
        email = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        remember_me = form.cleaned_data["remember_me"]

        user = authenticate_user_by_email(email, password)

        if user is None:
            messages.error(request, "Credenciais inválidas ou conta ainda não ativa.")
        else:
            now = timezone.now()
            user.last_login = now
            user.updated_at = now
            user.save(update_fields=["last_login", "updated_at"])

            login_user_manual(request, user, remember_me=remember_me)

            if user.role == "ADMIN":
                return redirect("dashboard:gestor")
            return redirect("dashboard:painel")

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
        user = create_user_and_profile(form.cleaned_data)
        token = create_signup_verification_token(user)
        send_signup_confirmation_email(request, user, token, async_send=True)

        request.session["registration_email"] = user.email
        return redirect("accounts:register_success")

    return render(request, "accounts/register.html", {"form": form})


def register_success_view(request):
    email = request.session.get("registration_email")
    return render(request, "accounts/register_success.html", {"email": email})


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
    form = AdminInviteCompleteForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        complete_invited_user_account(user, form.cleaned_data)

        token_obj.used_at = timezone.now()
        token_obj.save(update_fields=["used_at"])

        messages.success(request, "Conta ativada com sucesso. Já pode iniciar sessão.")
        return redirect("accounts:login")

    return render(
        request,
        "accounts/admin_invite_complete.html",
        {
            "form": form,
            "invited_email": user.email,
            "token": token,
        },
    )


def logout_view(request):
    logout_user_manual(request)
    return redirect("accounts:login")


def password_reset_request_view(request):
    form = PasswordResetRequestForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].strip().lower()

        user = User.objects.filter(email=email, is_active=True).first()
        if user:
            token = create_password_reset_token(user)
            send_password_reset_email(request, user, token)

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

    form = PasswordResetConfirmForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        token_obj.user.password = make_password(form.cleaned_data["password"])
        token_obj.user.updated_at = timezone.now()
        token_obj.user.save(update_fields=["password", "updated_at"])

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
