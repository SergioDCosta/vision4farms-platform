from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, time

from django.db.models import Q
from django.db.models import Case, When, Value, IntegerField
from django.db import transaction
from django.utils import timezone

from apps.inventory.models import Stock
from apps.inventory.services import calculate_inventory_commitment_state
from apps.catalog.models import Product
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.needs.models import NeedSourceSystem, NeedStatus
from apps.needs.services import calculate_need_coverage
from apps.recommendations.models import (
    Recommendation,
    RecommendationItem,
    RecommendationSourceType,
    RecommendationStatus,
)


QTY_DECIMAL = Decimal("0.001")
MONEY_DECIMAL = Decimal("0.01")
RECOMMENDATION_DIRECTION_BUY = "BUY"
RECOMMENDATION_DIRECTION_SELL = "SELL"
RECOMMENDATION_DIRECTION_BALANCED = "BALANCED"
STOCK_WARNING_MARGIN_RATIO = Decimal("0.10")
TRACKABLE_CUSTOMER_DEMAND_STATUSES = {
    NeedStatus.OPEN,
    NeedStatus.PARTIALLY_COVERED,
    NeedStatus.COVERED,
}


class RecommendationGenerationError(Exception):
    pass


def quantize_qty(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(QTY_DECIMAL)


def quantize_money(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_DECIMAL, rounding=ROUND_HALF_UP)


def _stock_warning_upper_bound(safety_stock: Decimal) -> Decimal:
    safety_stock = quantize_qty(safety_stock)
    if safety_stock <= 0:
        return safety_stock
    return quantize_qty(safety_stock + (safety_stock * STOCK_WARNING_MARGIN_RATIO))


def _has_sell_recommendation(*, available_stock: Decimal, safety_stock: Decimal) -> bool:
    if available_stock <= safety_stock:
        return False
    if safety_stock <= 0:
        return available_stock > 0
    return available_stock > _stock_warning_upper_bound(safety_stock)


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


def _as_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _as_deadline_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        deadline = datetime.combine(value, time.max)
        return timezone.make_aware(deadline, timezone.get_current_timezone())
    return None


def _customer_demand_need_from_state(commitment_state):
    plan = commitment_state.get("plan") or {}
    need = plan.get("generated_need")
    if (
        need
        and getattr(need, "source_system", None) == NeedSourceSystem.CUSTOMER_DEMAND
        and getattr(need, "status", None) in TRACKABLE_CUSTOMER_DEMAND_STATUSES
    ):
        return need
    return None


def _build_metrics_from_commitment_state(commitment_state):
    safety_stock = quantize_qty(Decimal(str(commitment_state.get("total_external_demand") or 0)))
    reserved_quantity = quantize_qty(Decimal(str(commitment_state.get("reserved_quantity") or 0)))
    current_stock = quantize_qty(Decimal(str(commitment_state.get("current_quantity") or 0)))
    available_stock = quantize_qty(Decimal(str(commitment_state.get("available_stock_now") or 0)))
    physical_deficit = quantize_qty(Decimal(str(commitment_state.get("max_deficit") or 0)))
    sell_quantity = quantize_qty(Decimal(str(commitment_state.get("temporal_sellable_quantity") or 0)))
    customer_demand_need = _customer_demand_need_from_state(commitment_state)
    planned_quantity = Decimal("0.000")
    if customer_demand_need and physical_deficit > 0:
        planned_quantity = quantize_qty(calculate_need_coverage(customer_demand_need)["planned_qty"])
    buy_quantity = quantize_qty(max(physical_deficit - planned_quantity, Decimal("0.000")))

    if buy_quantity > 0:
        direction = RECOMMENDATION_DIRECTION_BUY
        suggested_quantity = buy_quantity
    elif commitment_state.get("state_key") == "excess" and sell_quantity > 0:
        direction = RECOMMENDATION_DIRECTION_SELL
        suggested_quantity = sell_quantity
    else:
        direction = RECOMMENDATION_DIRECTION_BALANCED
        suggested_quantity = Decimal("0.000")

    return {
        "safety_stock": safety_stock,
        "reserved_quantity": reserved_quantity,
        "current_stock": current_stock,
        "available_stock": available_stock,
        "physical_deficit_quantity": physical_deficit,
        "planned_need_quantity": planned_quantity,
        "deficit_quantity": buy_quantity,
        "buy_quantity": buy_quantity,
        "sell_quantity": sell_quantity,
        "suggested_quantity": suggested_quantity,
        "recommendation_direction": direction,
        "useful_forecast_total": quantize_qty(Decimal(str(commitment_state.get("useful_forecast_total") or 0))),
        "first_deficit_date": commitment_state.get("first_deficit_date"),
        "deadline_date": (
            getattr(customer_demand_need, "needed_by_date", None)
            or _as_deadline_datetime(commitment_state.get("first_deficit_date"))
        ),
        "customer_demand_need": customer_demand_need,
        "customer_demand_need_published": bool(
            customer_demand_need and getattr(customer_demand_need, "is_marketplace_published", False)
        ),
        "has_customer_demand_deficit": physical_deficit > 0,
    }


def calculate_current_deficit(producer, product):
    stock = Stock.objects.filter(producer=producer, product=product).first()
    commitment_state = calculate_inventory_commitment_state(producer, product, stock=stock)
    return _build_metrics_from_commitment_state(commitment_state)


def build_recommendation_inventory_rows(producer, products):
    products = list(products or [])
    if not producer or not products:
        return {
            "buy_rows": [],
            "sell_rows": [],
            "balanced_rows": [],
            "all_rows": [],
        }

    product_ids = [product.id for product in products]
    stock_by_product = {
        stock.product_id: stock
        for stock in Stock.objects.filter(
            producer=producer,
            product_id__in=product_ids,
        ).only(
            "product_id",
            "current_quantity",
            "reserved_quantity",
            "safety_stock",
        )
    }

    buy_rows = []
    sell_rows = []
    balanced_rows = []
    all_rows = []

    for product in products:
        stock = stock_by_product.get(product.id)
        commitment_state = calculate_inventory_commitment_state(producer, product, stock=stock)

        metrics = _build_metrics_from_commitment_state(commitment_state)
        row = metrics | {
            "product": product,
            "product_id": str(product.id),
            "product_name": product.name,
            "unit": product.unit,
        }
        all_rows.append(row)
        if row["recommendation_direction"] == RECOMMENDATION_DIRECTION_BUY:
            buy_rows.append(row)
        elif row["recommendation_direction"] == RECOMMENDATION_DIRECTION_SELL:
            sell_rows.append(row)
        else:
            balanced_rows.append(row)

    buy_rows.sort(key=lambda row: (-row["buy_quantity"], row["product_name"].lower()))
    sell_rows.sort(key=lambda row: (-row["sell_quantity"], row["product_name"].lower()))
    balanced_rows.sort(key=lambda row: row["product_name"].lower())

    return {
        "buy_rows": buy_rows,
        "sell_rows": sell_rows,
        "balanced_rows": balanced_rows,
        "all_rows": all_rows,
    }

def _get_listing_available_quantity(listing) -> Decimal:
    value = getattr(listing, "quantity_available", None)
    if value is None:
        value = Decimal("0.000")
    return quantize_qty(value)


def _build_reason_list(position, listing):
    reasons = []

    has_forecast_source = bool(getattr(listing, "forecast_id", None)) and not bool(
        getattr(listing, "stock_id", None)
    )
    if has_forecast_source:
        forecast = getattr(listing, "forecast", None)
        forecast_available_at = (
            getattr(forecast, "period_end", None)
            or getattr(forecast, "period_start", None)
            if forecast
            else None
        )
        if forecast_available_at:
            local_start = (
                timezone.localtime(forecast_available_at)
                if timezone.is_aware(forecast_available_at)
                else forecast_available_at
            )
            availability_text = f"Disponível no dia {local_start.strftime('%d/%m/%Y')}"
        else:
            availability_text = "Disponível mais tarde"

        reasons.append({
            "text": availability_text,
            "tone": "blue",
        })
    else:
        reasons.append({
            "text": "Disponível agora (prioridade)",
            "tone": "green",
        })

    if position == 1:
        reasons.append({
            "text": "Melhor opção inicial da recomendação",
            "tone": "amber",
        })

    delivery_mode = getattr(listing, "delivery_mode", None)
    if delivery_mode in {"DELIVERY", "BOTH"}:
        reasons.append({
            "text": "Entrega disponível",
            "tone": "amber",
        })

    return reasons


def _listing_is_available_by_deadline(listing, deadline_date):
    if not deadline_date or not getattr(listing, "forecast_id", None):
        return True
    forecast = getattr(listing, "forecast", None)
    deadline = _as_date(deadline_date)
    available_date = _as_date(
        getattr(forecast, "period_end", None) or getattr(forecast, "period_start", None)
        if forecast
        else None
    )
    return bool(available_date and deadline and available_date <= deadline)


def _get_candidate_listings(product, buyer_producer):
    return (
        MarketplaceListing.objects
        .select_related("producer", "product", "forecast")
        .filter(
            product=product,
            status=ListingStatus.ACTIVE,
            quantity_available__gt=0,
            need_id__isnull=True,
        )
        .filter(
            Q(stock_id__isnull=False, forecast_id__isnull=True)
            | Q(stock_id__isnull=True, forecast_id__isnull=False)
        )
        .annotate(
            availability_priority=Case(
                When(stock_id__isnull=False, forecast_id__isnull=True, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .exclude(producer=buyer_producer)
        .order_by("availability_priority", "unit_price", "-quantity_available", "created_at")
    )


@transaction.atomic
def generate_recommendation(
    *,
    producer,
    product,
    requested_quantity,
    deadline_date=None,
    need=None,
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
        if not _listing_is_available_by_deadline(listing, deadline_date):
            continue

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
        need=need,
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
        "listing__forecast",
    ).order_by("position", "created_at")


def get_market_alternative_listings(recommendation):
    selected_listing_ids = recommendation.items.filter(is_selected=True).values_list("listing_id", flat=True)
    candidates = (
        MarketplaceListing.objects
        .select_related("producer", "product", "forecast")
        .filter(
            product=recommendation.product,
            status=ListingStatus.ACTIVE,
            quantity_available__gt=0,
            need_id__isnull=True,
        )
        .filter(
            Q(stock_id__isnull=False, forecast_id__isnull=True)
            | Q(stock_id__isnull=True, forecast_id__isnull=False)
        )
        .annotate(
            availability_priority=Case(
                When(stock_id__isnull=False, forecast_id__isnull=True, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .exclude(producer=recommendation.producer)
        .exclude(id__in=selected_listing_ids)
        .order_by("availability_priority", "unit_price", "-quantity_available", "created_at")
    )
    return [
        listing
        for listing in candidates
        if _listing_is_available_by_deadline(listing, recommendation.deadline_date)
    ]


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

