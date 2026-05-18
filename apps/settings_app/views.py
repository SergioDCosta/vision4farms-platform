from django.contrib import messages
from django.contrib.auth.hashers import check_password, make_password
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.accounts.models import UserRole
from apps.accounts.services import send_password_changed_email
from apps.common.audit import log_audit_event
from apps.common.decorators import login_required
from apps.inventory.models import ProducerProfile
from apps.settings_app.forms import (
    ChangePasswordForm,
    IdentityProfileForm,
    ProducerLocationForm,
    ProfilePhotoForm,
    UserPreferencesForm,
)
from apps.settings_app.services import (
    avatar_initials,
    ensure_user_preference,
    profile_photo_url,
    remove_profile_photo,
    update_identity_profile,
    update_notification_preferences,
    update_producer_location,
    update_profile_photo,
)


def _settings_redirect(anchor=""):
    response = redirect("settings_app:settings_index")
    if anchor:
        response["Location"] = f"{response['Location']}#{anchor}"
    return response


def _build_base_forms(*, user, preference, producer_profile):
    return {
        "identity_form": IdentityProfileForm(
            user=user,
            producer_profile=producer_profile,
        ),
        "location_form": (
            ProducerLocationForm(instance=producer_profile)
            if producer_profile else None
        ),
        "photo_form": ProfilePhotoForm(),
        "preferences_form": UserPreferencesForm(instance=preference, user=user),
        "security_form": ChangePasswordForm(user=user),
    }


@login_required
def settings_view(request):
    user = request.current_user
    if not user:
        return redirect("accounts:login")

    preference = ensure_user_preference(user)
    is_admin_user = user.role == UserRole.ADMIN
    is_client_user = user.role == UserRole.CLIENTE
    producer_profile = (
        ProducerProfile.objects.filter(user=user).first()
        if is_client_user else None
    )

    forms = _build_base_forms(
        user=user,
        preference=preference,
        producer_profile=producer_profile,
    )

    if request.method == "POST":
        form_type = (request.POST.get("form_type") or "").strip()

        if form_type == "identity_profile":
            forms["identity_form"] = IdentityProfileForm(
                request.POST,
                user=user,
                producer_profile=producer_profile,
            )
            if forms["identity_form"].is_valid():
                changed = update_identity_profile(
                    request=request,
                    user=user,
                    producer_profile=producer_profile,
                    form=forms["identity_form"],
                )
                if changed:
                    messages.success(request, "Identidade e perfil atualizados com sucesso.")
                else:
                    messages.info(request, "Não foram detetadas alterações na identidade e perfil.")
                return _settings_redirect("perfil-produtor")
            messages.error(request, "Não foi possível guardar a identidade e perfil. Verifica os campos.")

        elif form_type == "location":
            if not producer_profile:
                messages.error(request, "Perfil de produtor não encontrado para esta conta.")
                return _settings_redirect("localizacao")

            forms["location_form"] = ProducerLocationForm(
                request.POST,
                instance=producer_profile,
            )
            if forms["location_form"].is_valid():
                changed = update_producer_location(
                    request=request,
                    user=user,
                    producer_profile=producer_profile,
                    form=forms["location_form"],
                )
                if changed:
                    messages.success(request, "Localização atualizada com sucesso.")
                else:
                    messages.info(request, "Não foram detetadas alterações na localização.")
                return _settings_redirect("localizacao")
            messages.error(request, "Não foi possível guardar a localização. Verifica os campos.")

        elif form_type == "photo":
            forms["photo_form"] = ProfilePhotoForm(request.POST, request.FILES)
            if forms["photo_form"].is_valid():
                try:
                    update_profile_photo(
                        request=request,
                        user=user,
                        preference=preference,
                        uploaded_file=forms["photo_form"].cleaned_data["profile_photo"],
                    )
                    messages.success(request, "Foto de perfil atualizada com sucesso.")
                    return _settings_redirect("foto")
                except Exception:
                    messages.error(request, "Não foi possível guardar a foto. Tenta novamente.")
            else:
                messages.error(request, "Não foi possível guardar a foto. Verifica o ficheiro enviado.")

        elif form_type == "remove_photo":
            removed = remove_profile_photo(
                request=request,
                user=user,
                preference=preference,
            )
            if removed:
                messages.success(request, "Foto de perfil removida com sucesso.")
            else:
                messages.info(request, "Não existe foto de perfil para remover.")
            return _settings_redirect("foto")

        elif form_type == "preferences":
            forms["preferences_form"] = UserPreferencesForm(request.POST, instance=preference, user=user)
            if forms["preferences_form"].is_valid():
                changed = update_notification_preferences(
                    request=request,
                    user=user,
                    preference=preference,
                    form=forms["preferences_form"],
                )
                if changed:
                    messages.success(request, "Preferências de notificações atualizadas.")
                else:
                    messages.info(request, "Não foram detetadas alterações nas notificações.")
                return _settings_redirect("notificacoes")
            messages.error(request, "Não foi possível guardar as preferências. Verifica os campos.")

        elif form_type == "security":
            forms["security_form"] = ChangePasswordForm(request.POST, user=user)
            if forms["security_form"].is_valid():
                current_password = forms["security_form"].cleaned_data["current_password"]
                new_password = forms["security_form"].cleaned_data["new_password"]

                if not check_password(current_password, user.password):
                    forms["security_form"].add_error(
                        "current_password",
                        "A palavra-passe atual está incorreta.",
                    )
                    messages.error(request, "Não foi possível alterar a palavra-passe.")
                else:
                    user.password = make_password(new_password)
                    user.updated_at = timezone.now()
                    user.save(update_fields=["password", "updated_at"])
                    log_audit_event(
                        request=request,
                        user=user,
                        action="USER_PASSWORD_CHANGED",
                        entity_type="users",
                        entity_id=user.id,
                        notes="Utilizador alterou a palavra-passe nas definições.",
                        new_values={
                            "password_changed": True,
                            "sessions_invalidated": True,
                        },
                    )
                    send_password_changed_email(request, user, async_send=True)
                    request.session.flush()
                    messages.success(
                        request,
                        "Palavra-passe alterada com sucesso. Por segurança, inicia sessão novamente.",
                    )
                    return redirect("accounts:login")
            else:
                messages.error(request, "Não foi possível alterar a palavra-passe. Verifica os campos.")
        else:
            messages.error(request, "Ação inválida.")
            return _settings_redirect()

    context = {
        "page_title": "Definições",
        "is_admin_user": is_admin_user,
        "is_client_user": is_client_user and bool(forms["location_form"]),
        "profile_photo_url": profile_photo_url(preference),
        "avatar_initials": avatar_initials(user),
        **forms,
    }
    return render(request, "settings/settings_panel.html", context)
