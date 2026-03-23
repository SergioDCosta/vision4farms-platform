import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import make_password, check_password
from django.core.mail import EmailMultiAlternatives
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


def _send_system_email(subject, text_body, html_body, recipient_list):
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


def create_user_and_profile(form_data):
    email = form_data["email"].strip().lower()
    first_name = form_data["first_name"].strip()
    last_name = form_data["last_name"].strip()
    company = form_data.get("company", "").strip()
    user_type = form_data["user_type"]
    raw_password = form_data["password"]

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


def send_signup_confirmation_email(request, user, verification_token):
    verify_url = request.build_absolute_uri(
        reverse("accounts:verify_email", kwargs={"token": verification_token.token})
    )

    context = {
        "first_name": user.first_name,
        "verify_url": verify_url,
    }

    subject = render_to_string("emails/signup_confirmation_subject.txt", context).strip()
    text_body = render_to_string("emails/signup_confirmation.txt", context)
    html_body = render_to_string("emails/signup_confirmation.html", context)

    _send_system_email(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        recipient_list=[user.email],
    )


def authenticate_user_by_email(email, password):
    email = email.strip().lower()

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return None

    if not user.is_active:
        return None

    if user.account_status != AccountStatus.ACTIVE:
        return None

    if not check_password(password, user.password):
        return None

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
            purpose=VerificationPurpose.SIGNUP_CONFIRMATION,
            used_at__isnull=True,
        )
    except AccountVerificationToken.DoesNotExist:
        return None

    if token.expires_at < timezone.now():
        return None

    return token


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


def send_password_reset_email(request, user, reset_token):
    reset_url = request.build_absolute_uri(
        reverse("accounts:password_reset_confirm", kwargs={"token": reset_token.token})
    )

    context = {
        "first_name": user.first_name,
        "reset_url": reset_url,
    }

    subject = render_to_string("emails/password_reset_subject.txt", context).strip()
    text_body = render_to_string("emails/password_reset.txt", context)
    html_body = render_to_string("emails/password_reset.html", context)

    _send_system_email(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        recipient_list=[user.email],
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