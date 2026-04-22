import secrets
import threading
import logging
from datetime import timedelta
from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth.hashers import make_password, check_password
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import (
    User,
    AccountVerificationToken,
    UserRole,
    RegistrationSource,
    AccountStatus,
    VerificationPurpose,
)
from apps.inventory.models import ProducerProfile


logger = logging.getLogger(__name__)

LOGIN_DENIAL_INVALID_CREDENTIALS = "invalid_credentials"
LOGIN_DENIAL_ACCOUNT_DISABLED = "account_disabled"
LOGIN_DENIAL_EMAIL_NOT_CONFIRMED = "email_not_confirmed"
LOGIN_DENIAL_ACCOUNT_NOT_ACTIVE = "account_not_active"

VERIFICATION_EMAIL_TEMPLATES = {
    VerificationPurpose.SIGNUP_CONFIRMATION: {
        "subject": "emails/signup_confirmation_subject.txt",
        "text": "emails/signup_confirmation.txt",
        "html": "emails/signup_confirmation.html",
        "label": "signup_confirmation",
    },
    VerificationPurpose.ADMIN_INVITE: {
        "subject": "emails/admin_invite_subject.txt",
        "text": "emails/admin_invite.txt",
        "html": "emails/admin_invite.html",
        "label": "admin_invite",
    },
}


def _send_email_safely(email):
    try:
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Falha no envio de email em background.")


def _send_system_email(subject, text_body, html_body, recipient_list, async_send=False):
    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipient_list,
        reply_to=[settings.DEFAULT_REPLY_TO_EMAIL],
    )

    if html_body:
        email.attach_alternative(html_body, "text/html")

    if async_send:
        threading.Thread(target=_send_email_safely, args=(email,), daemon=True).start()
        return

    email.send(fail_silently=False)


def _build_public_absolute_url(request, relative_path):
    path = str(relative_path or "")
    app_base_url = (getattr(settings, "APP_BASE_URL", "") or "").strip().rstrip("/")
    if app_base_url:
        return urljoin(f"{app_base_url}/", path.lstrip("/"))
    return request.build_absolute_uri(path)


def _render_verification_email_bundle(*, purpose, context):
    bundle = VERIFICATION_EMAIL_TEMPLATES.get(purpose)
    if not bundle:
        raise ValueError(f"Verification purpose não suportado: {purpose}")
    return {
        "subject_template": bundle["subject"],
        "text_template": bundle["text"],
        "html_template": bundle["html"],
        "template_label": bundle["label"],
        "subject": render_to_string(bundle["subject"], context).strip(),
        "text_body": render_to_string(bundle["text"], context),
        "html_body": render_to_string(bundle["html"], context),
    }


def create_user_and_profile(form_data):
    email = form_data["email"].strip().lower()
    first_name = form_data["first_name"].strip()
    last_name = form_data["last_name"].strip()
    company = form_data.get("company", "").strip()
    user_type = form_data["user_type"]
    raw_password = form_data["password"]

    with transaction.atomic():
        user = User.objects.create(
            email=email,
            password=make_password(raw_password),
            first_name=first_name,
            last_name=last_name,
            role=UserRole.CLIENTE,
            registration_source=RegistrationSource.SELF_REGISTERED,
            account_status=AccountStatus.PENDING_EMAIL_CONFIRMATION,
            is_active=False,
            is_staff=False,
        )

        ProducerProfile.objects.create(
            user=user,
            display_name=f"{first_name} {last_name}".strip(),
            company_name=company or None,
            user_type=user_type,
            member_since=timezone.now(),
            completed_transactions_count=0,
            is_active_marketplace=True,
        )

    return user


def create_signup_verification_token(user):
    token = secrets.token_urlsafe(48)

    verification = AccountVerificationToken.objects.create(
        user=user,
        token=token,
        purpose=VerificationPurpose.SIGNUP_CONFIRMATION,
        expires_at=timezone.now() + timedelta(hours=24),
    )
    return verification


def create_admin_invite_token(user):
    token = secrets.token_urlsafe(48)
    now = timezone.now()

    with transaction.atomic():
        invalidate_pending_admin_invite_tokens(user, used_at=now)
        verification = AccountVerificationToken.objects.create(
            user=user,
            token=token,
            purpose=VerificationPurpose.ADMIN_INVITE,
            expires_at=timezone.now() + timedelta(hours=48),
        )

    return verification


def send_signup_confirmation_email(request, user, verification_token, async_send=False):
    purpose = VerificationPurpose.SIGNUP_CONFIRMATION
    verify_url = _build_public_absolute_url(
        request,
        reverse("accounts:verify_email", kwargs={"token": verification_token.token}),
    )

    context = {
        "first_name": user.first_name,
        "verify_url": verify_url,
    }
    email_bundle = _render_verification_email_bundle(purpose=purpose, context=context)

    logger.info(
        "Verification email prepared purpose=%s user_id=%s token_id=%s template=%s verify_url=%s",
        purpose,
        user.id,
        verification_token.id,
        email_bundle["template_label"],
        verify_url,
    )

    _send_system_email(
        subject=email_bundle["subject"],
        text_body=email_bundle["text_body"],
        html_body=email_bundle["html_body"],
        recipient_list=[user.email],
        async_send=async_send,
    )


def send_admin_invite_email(request, user, verification_token, async_send=False):
    purpose = VerificationPurpose.ADMIN_INVITE
    verify_url = _build_public_absolute_url(
        request,
        reverse("accounts:verify_email", kwargs={"token": verification_token.token}),
    )

    context = {
        "first_name": user.first_name or "Utilizador",
        "verify_url": verify_url,
    }
    email_bundle = _render_verification_email_bundle(purpose=purpose, context=context)

    logger.info(
        "Verification email prepared purpose=%s user_id=%s token_id=%s template=%s verify_url=%s",
        purpose,
        user.id,
        verification_token.id,
        email_bundle["template_label"],
        verify_url,
    )

    _send_system_email(
        subject=email_bundle["subject"],
        text_body=email_bundle["text_body"],
        html_body=email_bundle["html_body"],
        recipient_list=[user.email],
        async_send=async_send,
    )


def authenticate_user_with_reason(email, password):
    email = email.strip().lower()

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return None, LOGIN_DENIAL_INVALID_CREDENTIALS

    if user.account_status == AccountStatus.PENDING_EMAIL_CONFIRMATION:
        return None, LOGIN_DENIAL_EMAIL_NOT_CONFIRMED

    if user.account_status == AccountStatus.SUSPENDED or not user.is_active:
        return None, LOGIN_DENIAL_ACCOUNT_DISABLED

    if user.account_status != AccountStatus.ACTIVE:
        return None, LOGIN_DENIAL_ACCOUNT_NOT_ACTIVE

    if not check_password(password, user.password):
        return None, LOGIN_DENIAL_INVALID_CREDENTIALS

    return user, None


def authenticate_user_by_email(email, password):
    user, _ = authenticate_user_with_reason(email, password)
    return user


def mark_user_as_verified(user):
    now = timezone.now()
    user.email_verified_at = now
    user.account_status = AccountStatus.ACTIVE
    user.is_active = True
    user.updated_at = now
    user.save(update_fields=["email_verified_at", "account_status", "is_active", "updated_at"])


def validate_verification_token(token_value):
    try:
        token = AccountVerificationToken.objects.select_related("user").get(
            token=token_value,
            purpose__in=[
                VerificationPurpose.SIGNUP_CONFIRMATION,
                VerificationPurpose.ADMIN_INVITE,
            ],
            used_at__isnull=True,
        )
    except AccountVerificationToken.DoesNotExist:
        return None

    if token.expires_at < timezone.now():
        return None

    return token


def validate_admin_invite_token(token_value):
    try:
        token = AccountVerificationToken.objects.select_related("user").get(
            token=token_value,
            purpose=VerificationPurpose.ADMIN_INVITE,
            used_at__isnull=True,
        )
    except AccountVerificationToken.DoesNotExist:
        return None

    if token.expires_at < timezone.now():
        return None

    if token.user.account_status != AccountStatus.PENDING_EMAIL_CONFIRMATION:
        return None

    if token.user.is_active:
        return None

    return token


def invalidate_pending_admin_invite_tokens(user, used_at=None):
    mark_time = used_at or timezone.now()
    return AccountVerificationToken.objects.filter(
        user=user,
        purpose=VerificationPurpose.ADMIN_INVITE,
        used_at__isnull=True,
    ).update(used_at=mark_time)


def complete_invited_user_account(user, form_data):
    now = timezone.now()

    user.first_name = form_data["first_name"].strip()
    user.last_name = form_data["last_name"].strip()
    user.password = make_password(form_data["password"])
    user.account_status = AccountStatus.ACTIVE
    user.is_active = True
    user.email_verified_at = now
    user.updated_at = now
    user.save(
        update_fields=[
            "first_name",
            "last_name",
            "password",
            "account_status",
            "is_active",
            "email_verified_at",
            "updated_at",
        ]
    )

    if user.role == UserRole.CLIENTE:
        producer_profile, created = ProducerProfile.objects.get_or_create(
            user=user,
            defaults={
                "display_name": f"{user.first_name} {user.last_name}".strip(),
                "company_name": form_data.get("company") or None,
                "user_type": form_data.get("user_type") or None,
                "member_since": now,
                "completed_transactions_count": 0,
                "is_active_marketplace": True,
            },
        )

        if not created:
            producer_profile.display_name = f"{user.first_name} {user.last_name}".strip()
            producer_profile.company_name = form_data.get("company") or None
            producer_profile.user_type = form_data.get("user_type") or None
            producer_profile.updated_at = now
            producer_profile.save(
                update_fields=["display_name", "company_name", "user_type", "updated_at"]
            )


def login_user_manual(request, user, remember_me=False):
    request.session["user_id"] = str(user.id)
    request.session["user_email"] = user.email
    request.session["user_role"] = user.role
    request.session["user_name"] = user.full_name

    if remember_me:
        request.session.set_expiry(60 * 60 * 24 * 30)
    else:
        request.session.set_expiry(0)


def logout_user_manual(request):
    request.session.flush()


def create_password_reset_token(user):
    token = secrets.token_urlsafe(48)

    reset_token = AccountVerificationToken.objects.create(
        user=user,
        token=token,
        purpose=VerificationPurpose.PASSWORD_RESET,
        expires_at=timezone.now() + timedelta(hours=2),
    )
    return reset_token


def send_password_reset_email(request, user, reset_token, async_send=False):
    reset_url = _build_public_absolute_url(
        request,
        reverse("accounts:password_reset_confirm", kwargs={"token": reset_token.token}),
    )

    context = {
        "first_name": user.first_name,
        "reset_url": reset_url,
    }

    subject = render_to_string("emails/password_reset_subject.txt", context).strip()
    text_body = render_to_string("emails/password_reset.txt", context)
    html_body = render_to_string("emails/password_reset.html", context)

    logger.info(
        "Password reset email prepared user_id=%s token_id=%s verify_url=%s",
        user.id,
        reset_token.id,
        reset_url,
    )

    _send_system_email(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        recipient_list=[user.email],
        async_send=async_send,
    )


def validate_password_reset_token(token_value):
    try:
        token = AccountVerificationToken.objects.select_related("user").get(
            token=token_value,
            purpose=VerificationPurpose.PASSWORD_RESET,
            used_at__isnull=True,
        )
    except AccountVerificationToken.DoesNotExist:
        return None

    if token.expires_at < timezone.now():
        return None

    return token
