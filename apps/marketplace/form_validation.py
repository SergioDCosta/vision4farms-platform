from datetime import timedelta

from django.utils import timezone

from apps.marketplace.models import DeliveryMode, ListingStatus


def apply_delivery_validation(form, cleaned_data):
    delivery_mode = cleaned_data.get("delivery_mode")
    delivery_radius_km = cleaned_data.get("delivery_radius_km")

    if delivery_mode in {DeliveryMode.DELIVERY, DeliveryMode.BOTH}:
        if not delivery_radius_km:
            form.add_error("delivery_radius_km", "Indica o raio de entrega.")
    else:
        cleaned_data["delivery_radius_km"] = None
        cleaned_data["delivery_fee"] = None


def resolve_expiration(
    form,
    cleaned_data,
    *,
    allow_timer=False,
    reject_unknown_mode=False,
):
    expiration_mode = cleaned_data.get("expiration_mode") or "none"
    expires_at = cleaned_data.get("expires_at")
    now = timezone.now()
    expires_at_final = None

    if allow_timer and expiration_mode == "timer":
        expires_in = cleaned_data.get("expires_in")
        if expires_in is None:
            form.add_error("expires_in", "Indica a duração entre 6 e 720 horas.")
        else:
            expires_at_final = now + timedelta(hours=expires_in)
    elif expiration_mode == "date":
        if not expires_at:
            form.add_error("expires_at", "Indica a data/hora de expiração.")
        else:
            if timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(
                    expires_at,
                    timezone.get_current_timezone(),
                )
            expires_at_final = expires_at
    elif reject_unknown_mode and expiration_mode != "none":
        form.add_error("expiration_mode", "Modo de expiração inválido.")

    if (
        cleaned_data.get("status") == ListingStatus.ACTIVE
        and expires_at_final
        and expires_at_final <= now
    ):
        form.add_error("expires_at", "Para manter ativo, a expiração tem de ser no futuro.")

    if cleaned_data.get("status") == ListingStatus.EXPIRED and not expires_at_final:
        expires_at_final = now

    return expires_at_final
