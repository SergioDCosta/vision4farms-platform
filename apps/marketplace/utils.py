from decimal import Decimal

from apps.marketplace.constants import QTY_DECIMAL
from apps.marketplace.models import DeliveryMode


def quantize_qty(value):
    return Decimal(str(value)).quantize(QTY_DECIMAL)


def get_producer_display_name(producer):
    if not producer:
        return "Produtor"

    if getattr(producer, "display_name", None):
        return producer.display_name

    if getattr(producer, "company_name", None):
        return producer.company_name

    user = getattr(producer, "user", None)
    if user:
        full_name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
        if full_name:
            return full_name
        return user.email

    return "Produtor"


def get_producer_initials(producer):
    name = get_producer_display_name(producer)
    parts = [p for p in name.split() if p.strip()]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    if parts:
        return parts[0][:2].upper()
    return "PR"


def get_producer_location(producer):
    if not producer:
        return "Localização não indicada"

    city = (getattr(producer, "city", None) or "").strip()
    district = (getattr(producer, "district", None) or "").strip()

    if city and district:
        return f"{city}, {district}"
    if city:
        return city
    if district:
        return district

    return "Localização não indicada"


def build_delivery_text(listing):
    mode = listing.delivery_mode
    radius = listing.delivery_radius_km
    fee = listing.delivery_fee

    if mode == DeliveryMode.PICKUP:
        return "Levantamento na exploração."

    if mode == DeliveryMode.DELIVERY:
        parts = ["Entrega disponível"]
        if radius:
            parts.append(f"num raio de {radius} km")
        if fee is not None:
            parts.append(f"(taxa adicional de {fee}€)")
        return " ".join(parts) + "."

    if mode == DeliveryMode.BOTH:
        parts = ["Levantamento na exploração ou entrega disponível"]
        if radius:
            parts.append(f"num raio de {radius} km")
        if fee is not None:
            parts.append(f"(taxa adicional de {fee}€)")
        return " ".join(parts) + "."

    return "Condições de entrega a combinar com o produtor."
