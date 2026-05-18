import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from apps.common.audit import log_audit_event
from apps.common.media import resolve_media_url
from apps.settings_app.models import UserPreference


def ensure_user_preference(user):
    preference = UserPreference.objects.filter(user=user).first()
    if preference:
        return preference

    return UserPreference.objects.create(
        id=uuid.uuid4(),
        user=user,
        alerts_in_app=True,
        alerts_email=False,
        alerts_sms=False,
        created_at=timezone.now(),
        updated_at=timezone.now(),
    )


def avatar_initials(user):
    first_initial = (getattr(user, "first_name", "") or "").strip()[:1]
    last_initial = (getattr(user, "last_name", "") or "").strip()[:1]
    initials = f"{first_initial}{last_initial}".upper()
    return initials or "U"


def profile_photo_url(preference):
    base_url = resolve_media_url(getattr(preference, "profile_photo", None))
    if not base_url:
        return None

    updated_at = getattr(preference, "updated_at", None)
    if updated_at:
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}v={int(updated_at.timestamp())}"
    return base_url


def settings_snapshot(instance, fields):
    snapshot = {}
    for field in fields:
        value = getattr(instance, field, None)
        if field == "profile_photo":
            value = str(value or "")
        snapshot[field] = value
    return snapshot


def save_profile_photo(user, uploaded_file):
    if not isinstance(uploaded_file, UploadedFile):
        raise ValueError("O ficheiro enviado para foto de perfil é inválido.")

    extension = Path(uploaded_file.name).suffix.lower() or ".jpg"
    filename = (
        f"profile_photos/{user.id}/"
        f"{timezone.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}{extension}"
    )
    return default_storage.save(filename, uploaded_file)


def delete_profile_photo(photo_path):
    if not photo_path:
        return False

    raw_path = str(photo_path).strip()
    if not raw_path or raw_path.startswith(("http://", "https://")):
        return False

    if raw_path.startswith(settings.MEDIA_URL):
        raw_path = raw_path[len(settings.MEDIA_URL):]

    raw_path = raw_path.lstrip("/").strip()
    if not raw_path:
        return False

    try:
        default_storage.delete(raw_path)
        return True
    except Exception:
        return False


def _user_public_name(user):
    return f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()


def update_identity_profile(*, request, user, producer_profile, form):
    user_changed_fields = []
    profile_changed_fields = []
    old_user_values = {
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
    }
    old_profile_values = {}

    first_name = form.cleaned_data["first_name"]
    last_name = form.cleaned_data["last_name"]

    if user.first_name != first_name:
        user.first_name = first_name
        user_changed_fields.append("first_name")

    if user.last_name != last_name:
        user.last_name = last_name
        user_changed_fields.append("last_name")

    if producer_profile:
        profile_fields = ["company_name", "phone", "nif", "user_type"]
        old_profile_values = settings_snapshot(
            producer_profile,
            profile_fields + ["display_name"],
        )
        for field in profile_fields:
            new_value = form.cleaned_data.get(field)
            if getattr(producer_profile, field) != new_value:
                setattr(producer_profile, field, new_value)
                profile_changed_fields.append(field)

        new_display_name = _user_public_name(user)
        if new_display_name and producer_profile.display_name != new_display_name:
            producer_profile.display_name = new_display_name
            profile_changed_fields.append("display_name")

    if not user_changed_fields and not profile_changed_fields:
        return False

    if user_changed_fields:
        user.updated_at = timezone.now()
        user.save(update_fields=user_changed_fields + ["updated_at"])
        log_audit_event(
            request=request,
            user=user,
            action="USER_PROFILE_UPDATED",
            entity_type="users",
            entity_id=user.id,
            notes="Utilizador atualizou dados de identidade.",
            old_values=old_user_values,
            new_values={
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": user.email,
            },
        )

    request.session["user_name"] = user.full_name

    if producer_profile and profile_changed_fields:
        producer_profile.updated_at = timezone.now()
        producer_profile.save(update_fields=profile_changed_fields + ["updated_at"])
        log_audit_event(
            request=request,
            user=user,
            action="USER_PRODUCER_PROFILE_UPDATED",
            entity_type="producer_profiles",
            entity_id=producer_profile.id,
            notes="Utilizador atualizou dados operacionais do perfil.",
            old_values=old_profile_values,
            new_values=settings_snapshot(producer_profile, profile_changed_fields),
        )

    return True


def update_producer_location(*, request, user, producer_profile, form):
    changed_fields = list(form.changed_data)
    old_values = settings_snapshot(producer_profile, changed_fields)

    if not changed_fields:
        return False

    updated_profile = form.save(commit=False)
    updated_profile.updated_at = timezone.now()
    updated_profile.save(update_fields=changed_fields + ["updated_at"])
    log_audit_event(
        request=request,
        user=user,
        action="USER_PRODUCER_PROFILE_UPDATED",
        entity_type="producer_profiles",
        entity_id=producer_profile.id,
        notes="Utilizador atualizou a localização do produtor.",
        old_values=old_values,
        new_values=settings_snapshot(updated_profile, changed_fields),
    )
    return True


def update_notification_preferences(*, request, user, preference, form):
    allowed_fields = {"alerts_in_app", "alerts_email", "alerts_sms"}
    changed_fields = [
        field for field in form.changed_data
        if field in allowed_fields
    ]

    if not changed_fields:
        return False

    old_values = settings_snapshot(preference, changed_fields)
    updated_preference = form.save(commit=False)
    updated_preference.updated_at = timezone.now()
    updated_preference.save(update_fields=changed_fields + ["updated_at"])
    log_audit_event(
        request=request,
        user=user,
        action="USER_PREFERENCES_UPDATED",
        entity_type="user_preferences",
        entity_id=preference.id,
        notes="Utilizador atualizou preferências de notificações.",
        old_values=old_values,
        new_values=settings_snapshot(updated_preference, changed_fields),
    )
    return True


def update_profile_photo(*, request, user, preference, uploaded_file):
    old_photo = preference.profile_photo
    new_photo_path = save_profile_photo(user, uploaded_file)
    old_values = {"profile_photo": str(old_photo or "")}

    try:
        preference.profile_photo = new_photo_path
        preference.updated_at = timezone.now()
        preference.save(update_fields=["profile_photo", "updated_at"])
    except Exception:
        delete_profile_photo(new_photo_path)
        raise

    if old_photo and old_photo != new_photo_path:
        delete_profile_photo(old_photo)

    log_audit_event(
        request=request,
        user=user,
        action="USER_PREFERENCES_UPDATED",
        entity_type="user_preferences",
        entity_id=preference.id,
        notes="Utilizador atualizou a foto de perfil.",
        old_values=old_values,
        new_values={"profile_photo": str(new_photo_path or "")},
    )
    return True


def remove_profile_photo(*, request, user, preference):
    if not preference.profile_photo:
        return False

    old_photo = preference.profile_photo
    preference.profile_photo = None
    preference.updated_at = timezone.now()
    preference.save(update_fields=["profile_photo", "updated_at"])
    delete_profile_photo(old_photo)
    log_audit_event(
        request=request,
        user=user,
        action="USER_PROFILE_PHOTO_REMOVED",
        entity_type="user_preferences",
        entity_id=preference.id,
        notes="Utilizador removeu a foto de perfil.",
        old_values={"profile_photo": str(old_photo or "")},
        new_values={"profile_photo": ""},
    )
    return True
