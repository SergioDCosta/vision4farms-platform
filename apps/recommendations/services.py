from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict

from django.db import transaction
from django.utils import timezone

from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.recommendations.models import (
    Recommendation,
    RecommendationItem,
    RecommendationSourceType,
    RecommendationStatus,
)


MONEY_DECIMAL = Decimal("0.01")
QTY_DECIMAL = Decimal("0.001")


class RecommendationGenerationError(Exception):
    pass


def _to_decimal(value, label="valor"):
    try:
        decimal_value = Decimal(str(value))
    except Exception:
        raise RecommendationGenerationError(f"{label} inválido.")

    if decimal_value <= 0:
        raise RecommendationGenerationError(f"{label} deve ser superior a zero.")

    return decimal_value.quantize(QTY_DECIMAL)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_DECIMAL, rounding=ROUND_HALF_UP)


def _get_candidate_listings(product, buyer_producer):
    """
    Vai buscar anúncios ativos do produto pedido.
    Exclui anúncios do próprio produtor comprador.
    Ajusta aqui caso o teu model MarketplaceListing tenha nomes de campos ligeiramente diferentes.
    """
    return (
        MarketplaceListing.objects
        .select_related("producer", "product")
        .filter(
            product=product,
            status=ListingStatus.ACTIVE,
            available_quantity__gt=0,
        )
        .exclude(producer=buyer_producer)
        .order_by("unit_price", "-available_quantity", "created_at")
    )


def _build_reason_list(position: int, listing, allocated_quantity: Decimal) -> List[str]:
    reasons = []

    if position == 1:
        reasons.append("Melhor opção disponível no topo da ordenação.")
    reasons.append("Anúncio ativo no marketplace.")
    reasons.append(f"Quantidade sugerida: {allocated_quantity}")
    reasons.append(f"Preço unitário: {listing.unit_price}")

    return reasons


def _allocate_listing_plan(requested_quantity: Decimal, listings) -> Dict:
    remaining = requested_quantity
    position = 1
    items = []
    estimated_total = Decimal("0.00")

    for listing in listings:
        if remaining <= 0:
            break

        available_quantity = Decimal(str(listing.available_quantity)).quantize(QTY_DECIMAL)
        if available_quantity <= 0:
            continue

        allocated_quantity = min(remaining, available_quantity)
        subtotal = _money(allocated_quantity * listing.unit_price)

        item_data = {
            "listing": listing,
            "seller_producer": listing.producer,
            "product": listing.product,
            "suggested_quantity": allocated_quantity,
            "unit_price": listing.unit_price,
            "subtotal": subtotal,
            "position": position,
            "is_selected": True,
            "reasons": _build_reason_list(position, listing, allocated_quantity),
        }
        items.append(item_data)

        estimated_total += subtotal
        remaining -= allocated_quantity
        remaining = remaining.quantize(QTY_DECIMAL)
        position += 1

    deficit_quantity = remaining if remaining > 0 else Decimal("0.000")

    return {
        "items": items,
        "estimated_total": _money(estimated_total),
        "deficit_quantity": deficit_quantity,
    }


def _build_summary_text(product_name: str, requested_quantity: Decimal, items_count: int, deficit_quantity: Decimal) -> str:
    if items_count == 0:
        return (
            f"Não foram encontrados anúncios ativos para {product_name} "
            f"na quantidade pedida ({requested_quantity})."
        )

    if deficit_quantity > 0:
        return (
            f"Foram encontradas {items_count} opção(ões) para {product_name}, "
            f"mas continua a faltar {deficit_quantity} para satisfazer totalmente a necessidade."
        )

    return (
        f"Foram encontradas {items_count} opção(ões) para {product_name} "
        f"e a necessidade pode ser totalmente satisfeita."
    )


def _build_reason_summary(items_count: int, estimated_total: Decimal, deficit_quantity: Decimal) -> str:
    if items_count == 0:
        return "Sem anúncios compatíveis ativos no marketplace."

    if deficit_quantity > 0:
        return (
            f"A recomendação foi montada com {items_count} anúncio(s), "
            f"com total estimado de {estimated_total} e cobertura parcial da necessidade."
        )

    return (
        f"A recomendação foi montada com {items_count} anúncio(s), "
        f"com total estimado de {estimated_total} e cobertura total da necessidade."
    )


@transaction.atomic
def generate_recommendation(
    *,
    producer,
    product,
    requested_quantity,
    deadline_date=None,
    source_type=RecommendationSourceType.MANUAL,
    generated_from_alert=None,
):
    """
    Gera uma recomendação persistida na BD e os respetivos recommendation_items.

    Esta função não cria encomenda e não reserva stock.
    Apenas sugere a melhor composição possível com base nos anúncios ativos.
    """
    requested_quantity = _to_decimal(requested_quantity, label="Quantidade pedida")

    listings = _get_candidate_listings(product, producer)
    plan = _allocate_listing_plan(requested_quantity, listings)

    summary_text = _build_summary_text(
        product_name=product.name,
        requested_quantity=requested_quantity,
        items_count=len(plan["items"]),
        deficit_quantity=plan["deficit_quantity"],
    )

    reason_summary = _build_reason_summary(
        items_count=len(plan["items"]),
        estimated_total=plan["estimated_total"],
        deficit_quantity=plan["deficit_quantity"],
    )

    recommendation = Recommendation.objects.create(
        producer=producer,
        product=product,
        generated_from_alert=generated_from_alert,
        requested_quantity=requested_quantity,
        deadline_date=deadline_date,
        deficit_quantity=plan["deficit_quantity"],
        source_type=source_type,
        status=RecommendationStatus.GENERATED,
        summary_text=summary_text,
        reason_summary=reason_summary,
        estimated_total=plan["estimated_total"],
        created_at=timezone.now(),
        updated_at=timezone.now(),
    )

    for item in plan["items"]:
        RecommendationItem.objects.create(
            recommendation=recommendation,
            listing=item["listing"],
            seller_producer=item["seller_producer"],
            product=item["product"],
            suggested_quantity=item["suggested_quantity"],
            unit_price=item["unit_price"],
            subtotal=item["subtotal"],
            position=item["position"],
            is_selected=item["is_selected"],
            reasons=item["reasons"],
            created_at=timezone.now(),
        )

    return recommendation