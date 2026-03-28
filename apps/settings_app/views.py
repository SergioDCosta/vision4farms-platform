import uuid
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.hashers import check_password, make_password
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.common.decorators import login_required
from apps.accounts.models import UserRole
from apps.inventory.models import ProducerProfile
from apps.settings_app.forms import (
    AccountProfileForm,
    ChangePasswordForm,
    ProducerProfileSettingsForm,
    UserPreferencesForm,
)
from apps.settings_app.models import UserPreference


def _ensure_user_preference(user):
    preference = UserPreference.objects.filter(user=user).first()
    if preference:
        return preference

    return UserPreference.objects.create(
        id=uuid.uuid4(),
        user=user,
        alerts_in_app=True,
        alerts_email=True,
        alerts_sms=False,
        preferred_unit="kg",
        created_at=timezone.now(),
        updated_at=timezone.now(),
    )


def _profile_photo_url(preference):
    if not preference or not preference.profile_photo:
        return None

    photo_path = str(preference.profile_photo).strip()
    if not photo_path:
        return None

    if photo_path.startswith(("http://", "https://", "/")):
        base_url = photo_path
    else:
        base_url = f"{settings.MEDIA_URL}{photo_path.lstrip('/')}"

    if preference.updated_at:
        return f"{base_url}?v={int(preference.updated_at.timestamp())}"
    return base_url


def _user_initials(user):
    first_initial = (user.first_name or "").strip()[:1]
    last_initial = (user.last_name or "").strip()[:1]
    initials = f"{first_initial}{last_initial}".upper()
    return initials or "U"


def _save_profile_photo(user, uploaded_file):
    if not isinstance(uploaded_file, UploadedFile):
        raise ValueError("O ficheiro enviado para foto de perfil é inválido.")

    extension = Path(uploaded_file.name).suffix.lower() or ".jpg"
    filename = f"profile_photos/{user.id}/{timezone.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}{extension}"
    return default_storage.save(filename, uploaded_file)

def _delete_profile_photo(photo_path):
    if not photo_path:
        return False

    photo_path = str(photo_path).strip()
    if not photo_path:
        return False

    # Se por algum motivo vier com MEDIA_URL, limpar
    if photo_path.startswith(settings.MEDIA_URL):
        photo_path = photo_path[len(settings.MEDIA_URL):]

    photo_path = photo_path.lstrip("/").strip()

    try:
        if default_storage.exists(photo_path):
            default_storage.delete(photo_path)
            return True
    except Exception:
        pass

    return False

@login_required
def settings_view(request):
    user = request.current_user
    if not user:
        return redirect("accounts:login")

    preference = _ensure_user_preference(user)
    is_client_user = user.role == UserRole.CLIENTE
    producer_profile = ProducerProfile.objects.filter(user=user).first() if is_client_user else None

    account_form = AccountProfileForm(
        user=user,
        initial={
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
        },
    )
    producer_profile_form = (
        ProducerProfileSettingsForm(instance=producer_profile)
        if producer_profile else None
    )
    preferences_form = UserPreferencesForm(instance=preference)
    security_form = ChangePasswordForm()

    if request.method == "POST":
        form_type = (request.POST.get("form_type") or "").strip()

        if form_type == "account":
            account_form = AccountProfileForm(request.POST, user=user)
            if account_form.is_valid():
                changed_fields = []
                first_name = account_form.cleaned_data["first_name"]
                last_name = account_form.cleaned_data["last_name"]

                if user.first_name != first_name:
                    user.first_name = first_name
                    changed_fields.append("first_name")

                if user.last_name != last_name:
                    user.last_name = last_name
                    changed_fields.append("last_name")

                if changed_fields:
                    user.updated_at = timezone.now()
                    user.save(update_fields=changed_fields + ["updated_at"])
                    request.session["user_name"] = user.full_name
                    messages.success(request, "Dados da conta atualizados com sucesso.")
                else:
                    messages.info(request, "Não foram detetadas alterações nos dados da conta.")

                return redirect("settings_app:settings_index")

            messages.error(request, "Não foi possível guardar os dados da conta. Verifica os campos.")

        elif form_type == "producer_profile":
            if not producer_profile:
                messages.error(request, "Perfil de produtor não encontrado para esta conta.")
                return redirect("settings_app:settings_index")

            producer_profile_form = ProducerProfileSettingsForm(request.POST, instance=producer_profile)
            if producer_profile_form.is_valid():
                changed_fields = list(producer_profile_form.changed_data)

                if changed_fields:
                    updated_profile = producer_profile_form.save(commit=False)
                    updated_profile.updated_at = timezone.now()
                    updated_profile.save(update_fields=changed_fields + ["updated_at"])
                    messages.success(request, "Perfil de produtor atualizado com sucesso.")
                else:
                    messages.info(request, "Não foram detetadas alterações no perfil de produtor.")

                return redirect("settings_app:settings_index")

            messages.error(request, "Não foi possível guardar o perfil de produtor. Verifica os campos.")

        elif form_type == "preferences":
            preferences_form = UserPreferencesForm(request.POST, request.FILES, instance=preference)
            if preferences_form.is_valid():
                changed_fields = list(preferences_form.changed_data)

                uploaded_photo = request.FILES.get("profile_photo")
                remove_photo = request.POST.get("remove_profile_photo") in {"on", "true", "1"}

                # Se houver nova foto, substitui a antiga
                if uploaded_photo:
                    old_photo = preference.profile_photo
                    new_photo_path = _save_profile_photo(user, uploaded_photo)

                    preference.profile_photo = new_photo_path
                    if "profile_photo" not in changed_fields:
                        changed_fields.append("profile_photo")

                    if old_photo and old_photo != new_photo_path:
                        _delete_profile_photo(old_photo)

                # Se não houver upload novo e o utilizador quiser remover a atual
                elif remove_photo and preference.profile_photo:
                    old_photo = preference.profile_photo
                    preference.profile_photo = None
                    if "profile_photo" not in changed_fields:
                        changed_fields.append("profile_photo")
                    _delete_profile_photo(old_photo)

                if changed_fields:
                    updated_preference = preferences_form.save(commit=False)

                    # garantir que a foto final fica correta
                    updated_preference.profile_photo = preference.profile_photo
                    updated_preference.updated_at = timezone.now()
                    updated_preference.save(update_fields=list(set(changed_fields + ["updated_at"])))

                    messages.success(request, "Preferências atualizadas com sucesso.")
                else:
                    messages.info(request, "Não foram detetadas alterações nas preferências.")

                return redirect("settings_app:settings_index")

            messages.error(request, "Não foi possível guardar as preferências. Verifica os campos.")

        elif form_type == "security":
            security_form = ChangePasswordForm(request.POST)
            if security_form.is_valid():
                current_password = security_form.cleaned_data["current_password"]
                new_password = security_form.cleaned_data["new_password"]

                if not check_password(current_password, user.password):
                    security_form.add_error("current_password", "A palavra-passe atual está incorreta.")
                    messages.error(request, "Não foi possível alterar a palavra-passe.")
                else:
                    user.password = make_password(new_password)
                    user.updated_at = timezone.now()
                    user.save(update_fields=["password", "updated_at"])
                    messages.success(request, "Palavra-passe alterada com sucesso.")
                    return redirect("settings_app:settings_index")
            else:
                messages.error(request, "Não foi possível alterar a palavra-passe. Verifica os campos.")
        else:
            messages.error(request, "Ação inválida.")
            return redirect("settings_app:settings_index")

    context = {
        "page_title": "Definições e Perfil",
        "account_form": account_form,
        "producer_profile_form": producer_profile_form,
        "preferences_form": preferences_form,
        "security_form": security_form,
        "is_client_user": is_client_user and bool(producer_profile_form),
        "profile_photo_url": _profile_photo_url(preference),
        "avatar_initials": _user_initials(user),
    }
    return render(request, "settings/settings_panel.html", context)