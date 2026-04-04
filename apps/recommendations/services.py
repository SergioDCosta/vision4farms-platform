from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from apps.inventory.models import Stock
from apps.catalog.models import Product
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.recommendations.models import (
    Recommendation,
    RecommendationItem,
    RecommendationSourceType,
    RecommendationStatus,
)


QTY_DECIMAL = Decimal("0.001")
MONEY_DECIMAL = Decimal("0.01")


class RecommendationGenerationError(Exception):
    pass


def quantize_qty(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(QTY_DECIMAL)


def quantize_money(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_DECIMAL, rounding=ROUND_HALF_UP)


def get_producer_products(producer):
    return (
        Product.objects
        .filter(
            producer_links__producer=producer,
            producer_links__is_active=True,
            is_active=True,
        )
        .distinct()
        .order_by("name")
    )


def calculate_current_deficit(producer, product):
    stock = Stock.objects.filter(producer=producer, product=product).first()
    if not stock:
        minimum_recommended = Decimal("0.000")
        reserved_quantity = Decimal("0.000")
        current_stock = Decimal("0.000")
    else:
        minimum_recommended = quantize_qty(Decimal(str(stock.minimum_threshold or 0)))
        reserved_quantity = quantize_qty(Decimal(str(stock.reserved_quantity or 0)))
        current_stock = quantize_qty(Decimal(str(stock.current_quantity or 0)))

    deficit = quantize_qty(max(minimum_recommended + reserved_quantity - current_stock, Decimal("0.000")))

    return {
        "minimum_recommended": minimum_recommended,
        "reserved_quantity": reserved_quantity,
        "current_stock": current_stock,
        "deficit_quantity": deficit,
    }

def _get_listing_available_quantity(listing) -> Decimal:
    value = getattr(listing, "quantity_available", None)
    if value is None:
        value = Decimal("0.000")
    return quantize_qty(value)


def _build_reason_list(position, listing):
    reasons = []

    if position == 1:
        reasons.append("Mais barato no total")
    reasons.append("Disponível agora")

    delivery_mode = getattr(listing, "delivery_mode", None)
    if delivery_mode in {"DELIVERY", "BOTH"}:
        reasons.append("Entrega disponível")

    return reasons


def _get_candidate_listings(product, buyer_producer):
    return (
        MarketplaceListing.objects
        .select_related("producer", "product")
        .filter(
            product=product,
            status=ListingStatus.ACTIVE,
            quantity_available__gt=0,
        )
        .exclude(producer=buyer_producer)
        .order_by("unit_price", "-quantity_available", "created_at")
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
    requested_quantity = quantize_qty(requested_quantity)
    if requested_quantity <= 0:
        raise RecommendationGenerationError("A quantidade pedida deve ser superior a zero.")

    listings = _get_candidate_listings(product, producer)

    remaining = requested_quantity
    estimated_total = Decimal("0.00")
    position = 1
    planned_items = []

    for listing in listings:
        if remaining <= 0:
            break

        available_quantity = _get_listing_available_quantity(listing)
        if available_quantity <= 0:
            continue

        allocated = min(remaining, available_quantity)
        subtotal = quantize_money(allocated * Decimal(str(listing.unit_price)))

        planned_items.append({
            "listing": listing,
            "seller_producer": listing.producer,
            "product": listing.product,
            "suggested_quantity": allocated,
            "unit_price": listing.unit_price,
            "subtotal": subtotal,
            "position": position,
            "is_selected": True,
            "reasons": _build_reason_list(position, listing),
        })

        remaining = quantize_qty(remaining - allocated)
        estimated_total += subtotal
        position += 1

    deficit_quantity = remaining if remaining > 0 else Decimal("0.000")
    estimated_total = quantize_money(estimated_total)

    if planned_items:
        if deficit_quantity > 0:
            summary_text = (
                f"Foram encontradas {len(planned_items)} oferta(s) para {product.name}, "
                f"mas ainda faltam {deficit_quantity} {product.unit} para cobrir totalmente a necessidade."
            )
            reason_summary = "Cobertura parcial com base nos anúncios ativos atualmente disponíveis."
        else:
            summary_text = (
                f"Foram encontradas {len(planned_items)} oferta(s) para {product.name} "
                f"e a necessidade pode ser totalmente satisfeita."
            )
            reason_summary = "Melhor combinação disponível com base em preço e disponibilidade."
    else:
        summary_text = (
            f"Não foram encontrados anúncios ativos para {product.name} "
            f"na quantidade pedida ({requested_quantity})."
        )
        reason_summary = "Sem ofertas suficientes no marketplace para compor a recomendação."

    recommendation = Recommendation.objects.create(
        producer=producer,
        product=product,
        generated_from_alert=generated_from_alert,
        requested_quantity=requested_quantity,
        deadline_date=deadline_date,
        deficit_quantity=deficit_quantity,
        source_type=source_type,
        status=RecommendationStatus.GENERATED,
        summary_text=summary_text,
        reason_summary=reason_summary,
        estimated_total=estimated_total,
        created_at=timezone.now(),
        updated_at=timezone.now(),
    )

    for item in planned_items:
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


def get_selected_items(recommendation):
    return recommendation.items.filter(is_selected=True).select_related(
        "seller_producer",
        "product",
        "listing",
    ).order_by("position", "created_at")


def get_market_alternative_listings(recommendation):
    selected_listing_ids = recommendation.items.filter(is_selected=True).values_list("listing_id", flat=True)
    return (
        MarketplaceListing.objects
        .select_related("producer", "product")
        .filter(
            product=recommendation.product,
            status=ListingStatus.ACTIVE,
            quantity_available__gt=0,
        )
        .exclude(producer=recommendation.producer)
        .exclude(id__in=selected_listing_ids)
        .order_by("unit_price", "-quantity_available", "created_at")
    )


def get_recommendation_totals(recommendation):
    items = list(get_selected_items(recommendation))
    total_quantity = Decimal("0.000")
    total_amount = Decimal("0.00")

    for item in items:
        total_quantity += Decimal(str(item.suggested_quantity))
        total_amount += Decimal(str(item.subtotal))

    return {
        "items": items,
        "selected_total_quantity": quantize_qty(total_quantity),
        "selected_total_amount": quantize_money(total_amount),
    }


def accept_recommendation(recommendation):
    recommendation.status = RecommendationStatus.ACCEPTED
    recommendation.accepted_at = timezone.now()
    recommendation.updated_at = timezone.now()
    recommendation.save(update_fields=["status", "accepted_at", "updated_at"])
    return recommendation
