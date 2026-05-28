from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Min, Max, Count, Case, When, Value, CharField
from django.utils import timezone

from apps.catalog.models import Product, ProductCategory
from apps.common.audit import log_audit_event
from apps.inventory.models import ProducerProfile, ProducerProduct, ProductionForecast, Stock
from apps.marketplace.models import MarketplaceListing, ListingStatus, DeliveryMode
from apps.needs.models import NeedResponseStatus, NeedStatus


QTY_DECIMAL = Decimal("0.001")
LISTING_SOURCE_STOCK = "stock"
LISTING_SOURCE_FORECAST = "forecast"
MARKETPLACE_EDITABLE_STATUSES = {
    ListingStatus.ACTIVE,
    ListingStatus.CANCELLED,
    ListingStatus.EXPIRED,
}
MARKETPLACE_FINAL_STATUSES = {
    ListingStatus.RESERVED,
    ListingStatus.CLOSED,
}


class MarketplaceServiceError(Exception):
    pass


def _listing_audit_values(listing):
    if getattr(listing, "need_id", None):
        origin = "need_response"
    elif getattr(listing, "forecast_id", None):
        origin = LISTING_SOURCE_FORECAST
    else:
        origin = LISTING_SOURCE_STOCK
    return {
        "listing_id": str(getattr(listing, "id", "")) or None,
        "producer_id": str(getattr(listing, "producer_id", "")) or None,
        "product_id": str(getattr(listing, "product_id", "")) or None,
        "product_name": getattr(getattr(listing, "product", None), "name", None),
        "origin": origin,
        "stock_id": str(getattr(listing, "stock_id", "")) or None,
        "forecast_id": str(getattr(listing, "forecast_id", "")) or None,
        "need_id": str(getattr(listing, "need_id", "")) or None,
        "quantity_total": str(quantize_qty(getattr(listing, "quantity_total", 0) or 0)),
        "quantity_available": str(quantize_qty(getattr(listing, "quantity_available", 0) or 0)),
        "quantity_reserved": str(quantize_qty(getattr(listing, "quantity_reserved", 0) or 0)),
        "unit_price": str(getattr(listing, "unit_price", "")) or None,
        "delivery_mode": getattr(listing, "delivery_mode", None),
        "status": getattr(listing, "status", None),
    }


def quantize_qty(value):
    return Decimal(str(value)).quantize(QTY_DECIMAL)


def get_current_producer_for_user(user):
    if not user:
        return None
    return ProducerProfile.objects.filter(user=user).first()


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


def _valid_listing_source_filter():
    return (
        Q(stock_id__isnull=False, forecast_id__isnull=True)
        | Q(stock_id__isnull=True, forecast_id__isnull=False)
    )


def _validate_listing_source_xor(stock=None, forecast=None):
    has_stock = bool(stock)
    has_forecast = bool(forecast)
    if has_stock == has_forecast:
        raise MarketplaceServiceError(
            "Configuração inválida da oferta: selecione exatamente uma origem (stock atual ou previsão futura)."
        )


def _get_open_forecast_published_quantity(forecast, *, exclude_listing_id=None):
    if not forecast:
        return Decimal("0.000")

    from django.db.models import Sum, F

    qs = MarketplaceListing.objects.filter(
        forecast=forecast,
        status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
    )
    if exclude_listing_id:
        qs = qs.exclude(id=exclude_listing_id)

    result = qs.aggregate(total=Sum(F("quantity_available") + F("quantity_reserved")))
    return quantize_qty(result["total"] or Decimal("0.000"))


def _get_open_stock_published_quantity(stock, *, exclude_listing_id=None):
    """
    Soma quantity_available + quantity_reserved dos listings ACTIVE/RESERVED
    para o mesmo stock, excluindo o próprio ao editar.
    """
    if not stock:
        return Decimal("0.000")

    from django.db.models import Sum, F

    qs = MarketplaceListing.objects.filter(
        stock=stock,
        status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
        need_id__isnull=True,
    )
    if exclude_listing_id:
        qs = qs.exclude(id=exclude_listing_id)

    result = qs.aggregate(
        total=Sum(F("quantity_available") + F("quantity_reserved"))
    )
    return quantize_qty(result["total"] or Decimal("0.000"))


def _get_pending_stock_need_response_quantity(stock, *, exclude_listing_id=None):
    """
    Quantidade prometida em propostas privadas ainda não convertidas em encomenda.

    Uma proposta pendente não cria reserva física, mas não pode deixar o mesmo
    stock ser novamente prometido ou publicado.
    """
    if not stock:
        return Decimal("0.000")

    from django.db.models import Sum

    qs = MarketplaceListing.objects.filter(
        stock=stock,
        need_id__isnull=False,
        status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
        need_response_status=NeedResponseStatus.PENDING,
        order_items__isnull=True,
    )
    if exclude_listing_id:
        qs = qs.exclude(id=exclude_listing_id)

    result = qs.aggregate(total=Sum("quantity_available"))
    return quantize_qty(result["total"] or Decimal("0.000"))


def _get_uncommitted_forecast_quantity(forecast, *, exclude_listing_id=None):
    forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    published_quantity = _get_open_forecast_published_quantity(
        forecast,
        exclude_listing_id=exclude_listing_id,
    )
    return quantize_qty(
        max(forecast_quantity - reserved_quantity - published_quantity, Decimal("0.000"))
    )


def get_forecast_available_quantity(forecast, *, exclude_listing_id=None):
    source_available = _get_uncommitted_forecast_quantity(
        forecast,
        exclude_listing_id=exclude_listing_id,
    )
    if source_available <= 0:
        return source_available

    from apps.inventory.services import calculate_inventory_commitment_state

    commitment_state = calculate_inventory_commitment_state(
        forecast.producer,
        forecast.product,
        exclude_listing_id=exclude_listing_id,
    )
    if not commitment_state.get("has_external_demands"):
        return source_available

    safe_margin = quantize_qty(
        commitment_state.get("temporal_sellable_quantity") or Decimal("0.000")
    )
    return quantize_qty(min(source_available, safe_margin))


def get_base_listing_queryset():
    return (
        MarketplaceListing.objects
        .select_related(
            "producer",
            "producer__user",
            "product",
            "stock",
            "forecast",
            "need",
            "need__producer",
            "need__producer__user",
        )
        .filter(_valid_listing_source_filter())
        .order_by("-published_at", "-created_at")
    )


def _retired_listing_filter():
    """Identify removed listings while keeping merely disabled listings manageable."""
    return (
        Q(status=ListingStatus.CANCELLED, quantity_available__lte=0)
        & (
            Q(expires_at__isnull=False, expires_at__lte=timezone.now())
            | Q(photo_path__isnull=True)
            | Q(photo_path="")
        )
    )


def is_listing_retired_in_marketplace(listing):
    if not listing or getattr(listing, "need_id", None):
        return False
    if getattr(listing, "status", None) != ListingStatus.CANCELLED:
        return False
    if Decimal(str(getattr(listing, "quantity_available", 0) or 0)) > 0:
        return False
    expires_at = getattr(listing, "expires_at", None)
    is_expired_marker = bool(expires_at and expires_at <= timezone.now())
    has_no_photo = not getattr(listing, "photo_path", None)
    return is_expired_marker or has_no_photo


def is_listing_editable_in_marketplace(listing):
    if not listing or getattr(listing, "need_id", None):
        return False
    if is_listing_retired_in_marketplace(listing):
        return False
    return getattr(listing, "status", None) in MARKETPLACE_EDITABLE_STATUSES


def is_listing_toggleable_in_marketplace(listing):
    if not listing or getattr(listing, "need_id", None):
        return False
    if is_listing_retired_in_marketplace(listing):
        return False
    return getattr(listing, "status", None) in MARKETPLACE_EDITABLE_STATUSES


def is_listing_retirable_in_marketplace(listing):
    if not listing or getattr(listing, "need_id", None):
        return False
    if is_listing_retired_in_marketplace(listing):
        return False
    if getattr(listing, "status", None) in MARKETPLACE_FINAL_STATUSES:
        return False
    return Decimal(str(getattr(listing, "quantity_reserved", 0) or 0)) <= 0


def expire_due_active_listings():
    now = timezone.now()
    need_response_qs = MarketplaceListing.objects.filter(
        status=ListingStatus.ACTIVE,
        expires_at__isnull=False,
        expires_at__lte=now,
        need_id__isnull=False,
    )
    listing_qs = MarketplaceListing.objects.filter(
        status=ListingStatus.ACTIVE,
        expires_at__isnull=False,
        expires_at__lte=now,
        need_id__isnull=True,
    )
    expiring = list(need_response_qs) + list(listing_qs)
    need_responses_expired = need_response_qs.update(
        status=ListingStatus.EXPIRED,
        need_response_status=NeedResponseStatus.EXPIRED,
        updated_at=now,
    )
    listings_expired = listing_qs.update(
        status=ListingStatus.EXPIRED,
        updated_at=now,
    )
    for listing in expiring:
        old_values = _listing_audit_values(listing)
        listing.status = ListingStatus.EXPIRED
        log_audit_event(
            action="LISTING_EXPIRED",
            entity_type="marketplace_listings",
            entity_id=listing.id,
            notes="Anúncio ou proposta expirou automaticamente após atingir a data limite.",
            old_values=old_values,
            new_values=_listing_audit_values(listing),
        )
    return need_responses_expired + listings_expired


def _apply_listing_filters(qs, *, q="", category_id="", origin="", only_available=False):
    if q:
        q = q.strip()
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(notes__icontains=q)
            | Q(producer__display_name__icontains=q)
            | Q(producer__company_name__icontains=q)
            | Q(producer__city__icontains=q)
            | Q(producer__district__icontains=q)
            | Q(producer__user__first_name__icontains=q)
            | Q(producer__user__last_name__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    if origin == LISTING_SOURCE_STOCK:
        qs = qs.filter(stock_id__isnull=False, forecast_id__isnull=True)
    elif origin == LISTING_SOURCE_FORECAST:
        qs = qs.filter(stock_id__isnull=True, forecast_id__isnull=False)

    if only_available:
        qs = qs.filter(status=ListingStatus.ACTIVE, quantity_available__gt=0)

    return qs


def _apply_listing_sort(qs, *, sort="recent"):
    if sort == "price_asc":
        return qs.order_by("unit_price", "-published_at", "-created_at")
    if sort == "price_desc":
        return qs.order_by("-unit_price", "-published_at", "-created_at")
    if sort == "quantity_desc":
        return qs.order_by("-quantity_available", "-published_at", "-created_at")
    return qs.order_by("-published_at", "-created_at")


def get_public_listings(*, producer=None, q="", category_id="", origin="", sort="recent", only_available=True):
    # Os anúncios do próprio produtor também aparecem no feed geral: o cartão
    # distingue-os com "O seu anúncio" e a guarda de compra impede comprá-los.
    qs = get_base_listing_queryset().filter(
        status=ListingStatus.ACTIVE,
        quantity_available__gt=0,
        need_id__isnull=True,
        product__is_active=True,
    )

    qs = _apply_listing_filters(
        qs,
        q=q,
        category_id=category_id,
        origin=origin,
        only_available=only_available,
    )
    return _apply_listing_sort(qs, sort=sort)


def retire_listing(*, listing, acting_user=None):
    old_values = _listing_audit_values(listing)
    now = timezone.now()
    listing.status = ListingStatus.CANCELLED
    listing.quantity_available = Decimal("0.000")
    listing.photo_path = None
    listing.expires_at = now
    listing.updated_at = now
    listing.save(update_fields=["status", "quantity_available", "photo_path", "expires_at", "updated_at"])
    log_audit_event(
        actor=acting_user or getattr(listing, "_audit_actor", None),
        action="LISTING_RETIRED",
        entity_type="marketplace_listings",
        entity_id=getattr(listing, "id", None),
        notes="Anúncio retirado do marketplace pelo produtor.",
        old_values=old_values,
        new_values=_listing_audit_values(listing),
    )
    return listing


def get_my_listings(*, producer, q="", category_id="", origin="", sort="recent", only_available=False):
    qs = (
        get_base_listing_queryset()
        .filter(producer=producer, need_id__isnull=True)
        .exclude(_retired_listing_filter())
    )
    qs = _apply_listing_filters(
        qs,
        q=q,
        category_id=category_id,
        origin=origin,
        only_available=only_available,
    )
    return _apply_listing_sort(qs, sort=sort)


def get_listing_categories_for_queryset(listings_qs):
    category_ids = (
        listings_qs.exclude(product__category_id__isnull=True)
        .values_list("product__category_id", flat=True)
        .distinct()
    )

    return ProductCategory.objects.filter(id__in=category_ids).order_by("name")


def get_listing_detail_queryset(*, producer=None):
    qs = get_base_listing_queryset()

    if producer:
        return qs.filter(
            Q(
                need_id__isnull=True,
                status=ListingStatus.ACTIVE,
                quantity_available__gt=0,
                product__is_active=True,
            )
            | (Q(producer=producer) & ~_retired_listing_filter())
            | Q(need__producer=producer)
        )

    return qs.filter(
        need_id__isnull=True,
        status=ListingStatus.ACTIVE,
        quantity_available__gt=0,
        product__is_active=True,
    )


def get_producer_products(producer):
    product_ids = ProducerProduct.objects.filter(
        producer=producer,
        is_active=True,
    ).values_list("product_id", flat=True)

    return Product.objects.filter(
        id__in=product_ids,
        is_active=True,
    ).order_by("name")


def get_stock_for_product(producer, product):
    return Stock.objects.filter(producer=producer, product=product).first()


def get_marketplace_eligible_forecasts(producer, *, product=None):
    qs = ProductionForecast.objects.filter(
        producer=producer,
        is_marketplace_enabled=True,
    ).select_related("product", "product__category")

    if product:
        qs = qs.filter(product=product)

    forecasts = []
    for forecast in qs.order_by("-period_start", "-created_at"):
        if get_forecast_available_quantity(forecast) > 0:
            forecasts.append(forecast)
    return forecasts


def get_stock_available_quantity(stock):
    if not stock:
        return Decimal("0.000")

    current_quantity = Decimal(str(stock.current_quantity or 0))
    reserved_quantity = Decimal(str(stock.reserved_quantity or 0))
    return quantize_qty(current_quantity - reserved_quantity)


def get_max_publishable_quantity(stock, *, exclude_listing_id=None):
    """
    Excedente publicável calculado pela regra temporal dos compromissos externos,
    descontando a quantidade já anunciada em listings ativos do mesmo stock.
    Se não existirem pedidos externos ativos, usa o stock disponível atual.
    """
    if not stock:
        return Decimal("0.000")

    from apps.inventory.services import calculate_inventory_commitment_state

    commitment_state = calculate_inventory_commitment_state(
        stock.producer,
        stock.product,
        stock=stock,
        exclude_listing_id=exclude_listing_id,
    )
    base_max = quantize_qty(commitment_state.get("temporal_sellable_quantity") or Decimal("0.000"))
    if commitment_state.get("has_external_demands"):
        return base_max
    already_published = _get_open_stock_published_quantity(stock, exclude_listing_id=exclude_listing_id)
    pending_need_responses = _get_pending_stock_need_response_quantity(
        stock,
        exclude_listing_id=exclude_listing_id,
    )
    return quantize_qty(
        max(base_max - already_published - pending_need_responses, Decimal("0.000"))
    )


def get_publishable_products(producer):
    product_ids = set()

    stocks = (
        Stock.objects
        .select_related("product")
        .filter(producer=producer, product__is_active=True)
        .order_by("product__name")
    )

    for stock in stocks:
        if get_max_publishable_quantity(stock) > 0:
            product_ids.add(stock.product_id)

    for forecast in get_marketplace_eligible_forecasts(producer):
        product_ids.add(forecast.product_id)

    return Product.objects.filter(id__in=list(product_ids), is_active=True).order_by("name")


def get_market_price_trends_for_product_sources(producer, *, product_ids=None):
    """
    Tendências atuais do mercado para comparar no publish:
    - anúncios ativos
    - quantidade disponível > 0
    - exclui o próprio produtor
    - agrupado por produto + origem (stock/forecast)
    """
    if not producer:
        return {}

    qs = (
        MarketplaceListing.objects
        .filter(
            status=ListingStatus.ACTIVE,
            quantity_available__gt=0,
            need_id__isnull=True,
        )
        .exclude(producer=producer)
        .filter(_valid_listing_source_filter())
    )

    if product_ids:
        qs = qs.filter(product_id__in=list(product_ids))

    source_annotation = Case(
        When(stock_id__isnull=False, forecast_id__isnull=True, then=Value(LISTING_SOURCE_STOCK)),
        When(stock_id__isnull=True, forecast_id__isnull=False, then=Value(LISTING_SOURCE_FORECAST)),
        output_field=CharField(),
    )

    aggregates = (
        qs.annotate(source_key=source_annotation)
        .values("product_id", "source_key")
        .annotate(
            market_min_price=Min("unit_price"),
            market_max_price=Max("unit_price"),
            market_count=Count("id"),
        )
    )

    trend_map = {}
    for row in aggregates:
        key = (str(row["product_id"]), row["source_key"])
        trend_map[key] = {
            "market_min_price": row["market_min_price"],
            "market_max_price": row["market_max_price"],
            "market_count": row["market_count"] or 0,
        }

    return trend_map


def get_publishable_products_summary(producer, *, trend_map=None):
    rows = []
    if not producer:
        return rows

    trend_map = trend_map or {}

    stocks = (
        Stock.objects.select_related("product", "product__category")
        .filter(producer=producer, product__is_active=True)
        .order_by("product__name")
    )

    for stock in stocks:
        publishable_quantity = get_max_publishable_quantity(stock)
        if publishable_quantity <= 0:
            continue

        current_quantity = quantize_qty(stock.current_quantity or 0)
        reserved_quantity = quantize_qty(stock.reserved_quantity or 0)
        trend_key = (str(stock.product_id), LISTING_SOURCE_STOCK)
        trend = trend_map.get(trend_key, {})

        rows.append({
            "source": LISTING_SOURCE_STOCK,
            "product": stock.product,
            "product_id": str(stock.product_id),
            "category_name": stock.product.category.name if stock.product.category else "Sem categoria",
            "current_quantity": current_quantity,
            "reserved_quantity": reserved_quantity,
            "publishable_quantity": publishable_quantity,
            "period_start": None,
            "period_end": None,
            "forecast_id": None,
            "market_min_price": trend.get("market_min_price"),
            "market_max_price": trend.get("market_max_price"),
            "market_count": trend.get("market_count", 0),
        })

    for forecast in get_marketplace_eligible_forecasts(producer):
        trend_key = (str(forecast.product_id), LISTING_SOURCE_FORECAST)
        trend = trend_map.get(trend_key, {})
        rows.append({
            "source": LISTING_SOURCE_FORECAST,
            "product": forecast.product,
            "product_id": str(forecast.product_id),
            "category_name": forecast.product.category.name if forecast.product.category else "Sem categoria",
            "current_quantity": quantize_qty(forecast.forecast_quantity or 0),
            "reserved_quantity": quantize_qty(forecast.reserved_quantity or 0),
            "publishable_quantity": get_forecast_available_quantity(forecast),
            "period_start": forecast.period_start,
            "period_end": forecast.period_end,
            "forecast_id": forecast.id,
            "market_min_price": trend.get("market_min_price"),
            "market_max_price": trend.get("market_max_price"),
            "market_count": trend.get("market_count", 0),
        })

    return rows


def resolve_listing_source(*, producer, product, listing_source, forecast_id=None):
    listing_source = (listing_source or LISTING_SOURCE_STOCK).strip().lower()
    if listing_source == LISTING_SOURCE_STOCK:
        stock = get_stock_for_product(producer, product)
        _validate_listing_source_xor(stock=stock, forecast=None)
        return stock, None

    if listing_source == LISTING_SOURCE_FORECAST:
        if not forecast_id:
            raise MarketplaceServiceError("Selecione a previsão de produção para pré-venda.")
        try:
            forecast = ProductionForecast.objects.select_related("product").get(
                id=forecast_id,
                producer=producer,
                product=product,
            )
        except ProductionForecast.DoesNotExist:
            raise MarketplaceServiceError("Previsão de produção inválida para este produto.")

        if not forecast.is_marketplace_enabled:
            raise MarketplaceServiceError("Esta previsão não está ativa para marketplace.")
        if get_forecast_available_quantity(forecast) <= 0:
            raise MarketplaceServiceError("Esta previsão não tem quantidade disponível para pré-venda.")

        _validate_listing_source_xor(stock=None, forecast=forecast)
        return None, forecast

    raise MarketplaceServiceError("Origem da oferta inválida.")


@transaction.atomic
def create_listing(
    *,
    producer,
    product,
    quantity,
    unit_price,
    delivery_mode,
    delivery_radius_km=None,
    delivery_fee=None,
    show_location_on_map=True,
    notes=None,
    photo_path=None,
    status=ListingStatus.ACTIVE,
    expires_at=None,
    listing_source=LISTING_SOURCE_STOCK,
    forecast=None,
    need=None,
    acting_user=None,
):
    stock = None
    selected_forecast = None
    if listing_source == LISTING_SOURCE_STOCK:
        stock = (
            Stock.objects.select_for_update()
            .filter(producer=producer, product=product)
            .first()
        )
        max_publishable = get_max_publishable_quantity(stock)
    elif listing_source == LISTING_SOURCE_FORECAST:
        if not forecast:
            raise MarketplaceServiceError("Selecione uma previsão de produção válida.")
        selected_forecast = (
            ProductionForecast.objects.select_for_update()
            .filter(id=forecast.id, producer=producer, product=product)
            .first()
        )
        if not selected_forecast:
            raise MarketplaceServiceError("A previsão selecionada não pertence a este produto/produtor.")
        _validate_listing_source_xor(stock=None, forecast=selected_forecast)
        if not selected_forecast.is_marketplace_enabled:
            raise MarketplaceServiceError("Esta previsão não está ativa para marketplace.")
        max_publishable = get_forecast_available_quantity(selected_forecast)
    else:
        raise MarketplaceServiceError("Origem da oferta inválida.")

    quantity = Decimal(str(quantity))
    unit_price = Decimal(str(unit_price))
    now = timezone.now()

    if quantity <= 0:
        raise MarketplaceServiceError("A quantidade tem de ser superior a zero.")

    if unit_price <= 0:
        raise MarketplaceServiceError("O preço tem de ser superior a zero.")

    if status == ListingStatus.ACTIVE and expires_at and expires_at <= now:
        raise MarketplaceServiceError("Para manter ativo, a data de expiração deve ser no futuro.")

    if max_publishable <= 0:
        if listing_source == LISTING_SOURCE_FORECAST:
            raise MarketplaceServiceError("Esta previsão não tem quantidade disponível para pré-venda.")
        raise MarketplaceServiceError("Este produto não tem excedente disponível para publicar.")

    if quantity > max_publishable:
        raise MarketplaceServiceError(
            f"A quantidade excede o máximo publicável ({max_publishable} {product.unit})."
        )

    if need:
        if need.status not in {NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED}:
            raise MarketplaceServiceError(
                "A necessidade já não está aberta para receber respostas."
            )
        if need.product_id != product.id:
            raise MarketplaceServiceError(
                "O produto da resposta não corresponde ao produto da necessidade."
            )
        if need.producer_id == producer.id:
            raise MarketplaceServiceError(
                "Não pode responder à sua própria necessidade."
            )

    if delivery_mode == DeliveryMode.PICKUP:
        delivery_radius_km = None
        delivery_fee = None

    if status == ListingStatus.EXPIRED and not expires_at:
        expires_at = now

    listing = MarketplaceListing.objects.create(
        producer=producer,
        product=product,
        stock=stock,
        forecast=selected_forecast,
        need=need,
        quantity_total=quantity,
        quantity_available=quantity,
        quantity_reserved=Decimal("0.000"),
        unit_price=unit_price,
        delivery_mode=delivery_mode,
        delivery_radius_km=delivery_radius_km,
        delivery_fee=delivery_fee,
        show_location_on_map=bool(show_location_on_map),
        notes=notes or None,
        photo_path=photo_path or None,
        status=status or ListingStatus.ACTIVE,
        expires_at=expires_at,
        published_at=now,
    )
    action = "NEED_RESPONSE_CREATED" if need else "LISTING_CREATED"
    notes = (
        "Proposta privada criada em resposta a uma procura."
        if need
        else "Anúncio publicado no marketplace."
    )
    log_audit_event(
        actor=acting_user,
        action=action,
        entity_type="marketplace_listings",
        entity_id=listing.id,
        notes=notes,
        new_values=_listing_audit_values(listing),
    )
    return listing


@transaction.atomic
def update_listing(
    *,
    listing,
    quantity_total,
    unit_price,
    delivery_mode,
    delivery_radius_km=None,
    delivery_fee=None,
    show_location_on_map=True,
    notes=None,
    status=ListingStatus.ACTIVE,
    expires_at=None,
    photo_path=None,
    acting_user=None,
):
    quantity_total = Decimal(str(quantity_total))
    unit_price = Decimal(str(unit_price))
    reserved_quantity = Decimal(str(listing.quantity_reserved or 0))
    now = timezone.now()

    if getattr(listing, "status", None) in MARKETPLACE_FINAL_STATUSES:
        raise MarketplaceServiceError(
            "Este anúncio já está reservado ou fechado e não pode ser editado."
        )

    old_values = _listing_audit_values(listing)
    has_stock_source = bool(listing.stock_id)
    has_forecast_source = bool(listing.forecast_id)
    if has_stock_source == has_forecast_source:
        raise MarketplaceServiceError(
            "Anúncio com origem inválida. Ajuste os dados da listing para usar stock atual ou previsão futura."
        )

    if has_stock_source:
        locked_stock = (
            Stock.objects.select_for_update()
            .filter(id=listing.stock_id)
            .first()
        )
        if not locked_stock:
            raise MarketplaceServiceError("O stock associado ao anúncio não existe.")
        listing.stock = locked_stock
        source_available = get_max_publishable_quantity(locked_stock, exclude_listing_id=listing.id)
    else:
        locked_forecast = (
            ProductionForecast.objects.select_for_update()
            .filter(id=listing.forecast_id)
            .first()
        )
        if not locked_forecast:
            raise MarketplaceServiceError("A previsão associada ao anúncio não existe.")
        listing.forecast = locked_forecast
        source_available = get_forecast_available_quantity(
            locked_forecast,
            exclude_listing_id=listing.id,
        )

    if quantity_total <= 0:
        raise MarketplaceServiceError("A quantidade listada deve ser superior a zero.")

    if quantity_total < reserved_quantity:
        raise MarketplaceServiceError(
            f"A quantidade listada não pode ser inferior à reservada ({reserved_quantity})."
        )

    if unit_price <= 0:
        raise MarketplaceServiceError("O preço tem de ser superior a zero.")

    max_allowed_total = source_available + reserved_quantity
    if quantity_total > max_allowed_total:
        raise MarketplaceServiceError(
            (
                "A quantidade listada excede o máximo disponível para esta origem "
                f"({quantize_qty(max_allowed_total)} {listing.product.unit})."
            )
        )

    if status == ListingStatus.ACTIVE and expires_at and expires_at <= now:
        raise MarketplaceServiceError("Para manter ativo, a data de expiração deve ser no futuro.")

    if delivery_mode == DeliveryMode.PICKUP:
        delivery_radius_km = None
        delivery_fee = None

    quantity_available = quantity_total - reserved_quantity

    if status == ListingStatus.EXPIRED and not expires_at:
        expires_at = now

    listing.quantity_total = quantity_total
    listing.quantity_available = quantity_available
    listing.unit_price = unit_price
    listing.delivery_mode = delivery_mode
    listing.delivery_radius_km = delivery_radius_km
    listing.delivery_fee = delivery_fee
    listing.show_location_on_map = bool(show_location_on_map)
    listing.notes = notes or None
    listing.status = status
    listing.expires_at = expires_at
    if photo_path is not None:
        listing.photo_path = photo_path
    listing.updated_at = now
    listing.save(
        update_fields=[
            "quantity_total",
            "quantity_available",
            "unit_price",
            "delivery_mode",
            "delivery_radius_km",
            "delivery_fee",
            "show_location_on_map",
            "notes",
            "status",
            "expires_at",
            "photo_path",
            "updated_at",
        ]
    )
    log_audit_event(
        actor=acting_user,
        action="NEED_RESPONSE_UPDATED" if listing.need_id else "LISTING_UPDATED",
        entity_type="marketplace_listings",
        entity_id=listing.id,
        notes=(
            "Condições da proposta privada atualizadas."
            if listing.need_id
            else "Condições do anúncio atualizadas."
        ),
        old_values=old_values,
        new_values=_listing_audit_values(listing),
    )
    return listing


@transaction.atomic
def reactivate_listing(*, listing, acting_user=None):
    """Reactivate a disabled offer only if its source still covers its quantity."""
    try:
        listing = (
            MarketplaceListing.objects.select_for_update(of=("self",))
            .select_related("stock", "forecast", "product")
            .get(id=listing.id)
        )
    except MarketplaceListing.DoesNotExist:
        raise MarketplaceServiceError("Este anúncio já não existe.")

    if listing.need_id:
        raise MarketplaceServiceError("As propostas a necessidades são geridas no fluxo de necessidades.")
    if listing.status in MARKETPLACE_FINAL_STATUSES:
        raise MarketplaceServiceError("Este anúncio já está reservado ou fechado e não pode ser ativado novamente.")
    if is_listing_retired_in_marketplace(listing):
        raise MarketplaceServiceError("Este anúncio foi removido e não pode ser ativado novamente.")

    available_quantity = quantize_qty(listing.quantity_available or 0)
    reserved_quantity = quantize_qty(listing.quantity_reserved or 0)
    if reserved_quantity > 0:
        raise MarketplaceServiceError("Este anúncio está com quantidade reservada e não pode ser ativado agora.")
    if available_quantity <= 0:
        raise MarketplaceServiceError("Este anúncio não pode ser ativado sem quantidade disponível.")

    if listing.stock_id:
        stock = Stock.objects.select_for_update().filter(id=listing.stock_id).first()
        if not stock:
            raise MarketplaceServiceError("O stock associado ao anúncio não existe.")
        listing.stock = stock
        max_publishable = get_max_publishable_quantity(stock, exclude_listing_id=listing.id)
    elif listing.forecast_id:
        forecast = ProductionForecast.objects.select_for_update().filter(id=listing.forecast_id).first()
        if not forecast:
            raise MarketplaceServiceError("A previsão associada ao anúncio não existe.")
        listing.forecast = forecast
        max_publishable = get_forecast_available_quantity(forecast, exclude_listing_id=listing.id)
    else:
        raise MarketplaceServiceError("Este anúncio não tem uma origem válida.")

    if available_quantity > max_publishable:
        raise MarketplaceServiceError(
            "Não pode ativar este anúncio: a quantidade ultrapassa o máximo "
            f"publicável atual ({max_publishable} {listing.product.unit})."
        )

    old_values = _listing_audit_values(listing)
    listing.status = ListingStatus.ACTIVE
    if listing.expires_at and listing.expires_at <= timezone.now():
        listing.expires_at = None
    listing.updated_at = timezone.now()
    listing.save(update_fields=["status", "expires_at", "updated_at"])
    log_audit_event(
        actor=acting_user,
        action="LISTING_STATUS_CHANGED",
        entity_type="marketplace_listings",
        entity_id=listing.id,
        notes="Anúncio reativado pelo produtor após validação da quantidade publicável.",
        old_values=old_values,
        new_values=_listing_audit_values(listing),
    )
    return listing

