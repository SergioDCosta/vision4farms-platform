from decimal import Decimal

from django.db.models import Q, Min, Max, Count, Case, When, Value, CharField
from django.utils import timezone

from apps.catalog.models import Product, ProductCategory
from apps.inventory.models import ProducerProfile, ProducerProduct, ProductionForecast, Stock
from apps.marketplace.models import MarketplaceListing, ListingStatus, DeliveryMode


QTY_DECIMAL = Decimal("0.001")
LISTING_SOURCE_STOCK = "stock"
LISTING_SOURCE_FORECAST = "forecast"


class MarketplaceServiceError(Exception):
    pass


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

    qs = MarketplaceListing.objects.filter(
        forecast=forecast,
        status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
    )
    if exclude_listing_id:
        qs = qs.exclude(id=exclude_listing_id)

    total = Decimal("0.000")
    for listing in qs.only("quantity_available"):
        total += Decimal(str(listing.quantity_available or 0))

    return quantize_qty(total)


def get_forecast_available_quantity(forecast, *, exclude_listing_id=None):
    forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    published_quantity = _get_open_forecast_published_quantity(
        forecast,
        exclude_listing_id=exclude_listing_id,
    )
    return quantize_qty(
        max(forecast_quantity - reserved_quantity - published_quantity, Decimal("0.000"))
    )


def get_base_listing_queryset():
    return (
        MarketplaceListing.objects
        .select_related("producer", "producer__user", "product", "stock", "forecast")
        .filter(_valid_listing_source_filter())
        .order_by("-published_at", "-created_at")
    )


def expire_due_active_listings():
    now = timezone.now()
    MarketplaceListing.objects.filter(
        status=ListingStatus.ACTIVE,
        expires_at__isnull=False,
        expires_at__lte=now,
    ).update(
        status=ListingStatus.EXPIRED,
        updated_at=now,
    )


def get_public_listings(*, producer=None, q="", category_id=""):
    qs = get_base_listing_queryset().filter(
        status=ListingStatus.ACTIVE,
        quantity_available__gt=0,
        product__is_active=True,
    )

    if producer:
        qs = qs.exclude(producer=producer)

    if q:
        q = q.strip()
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(producer__display_name__icontains=q)
            | Q(producer__company_name__icontains=q)
            | Q(producer__user__first_name__icontains=q)
            | Q(producer__user__last_name__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    return qs


def get_my_listings(*, producer, q="", category_id=""):
    qs = get_base_listing_queryset().filter(producer=producer)

    if q:
        q = q.strip()
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    return qs


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
        return qs.filter(Q(status=ListingStatus.ACTIVE, product__is_active=True) | Q(producer=producer))

    return qs.filter(status=ListingStatus.ACTIVE, product__is_active=True)


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


def get_max_publishable_quantity(stock):
    """
    Regra v1:
    excedente publicável = current_quantity - reserved_quantity - safety_stock
    """
    if not stock:
        return Decimal("0.000")

    current_quantity = Decimal(str(stock.current_quantity or 0))
    reserved_quantity = Decimal(str(stock.reserved_quantity or 0))
    safety_stock = Decimal(str(stock.safety_stock or 0))

    publishable = current_quantity - reserved_quantity - safety_stock
    if publishable < 0:
        publishable = Decimal("0.000")

    return quantize_qty(publishable)


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


def create_listing(
    *,
    producer,
    product,
    quantity,
    unit_price,
    delivery_mode,
    delivery_radius_km=None,
    delivery_fee=None,
    notes=None,
    photo_path=None,
    status=ListingStatus.ACTIVE,
    expires_at=None,
    listing_source=LISTING_SOURCE_STOCK,
    forecast=None,
):
    stock = None
    selected_forecast = None
    if listing_source == LISTING_SOURCE_STOCK:
        stock = get_stock_for_product(producer, product)
        max_publishable = get_max_publishable_quantity(stock)
    elif listing_source == LISTING_SOURCE_FORECAST:
        selected_forecast = forecast
        _validate_listing_source_xor(stock=None, forecast=selected_forecast)
        if not selected_forecast:
            raise MarketplaceServiceError("Selecione uma previsão de produção válida.")
        if selected_forecast.producer_id != producer.id or selected_forecast.product_id != product.id:
            raise MarketplaceServiceError("A previsão selecionada não pertence a este produto/produtor.")
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

    if delivery_mode == DeliveryMode.PICKUP:
        delivery_radius_km = None
        delivery_fee = None

    if status == ListingStatus.EXPIRED and not expires_at:
        expires_at = now

    return MarketplaceListing.objects.create(
        producer=producer,
        product=product,
        stock=stock,
        forecast=selected_forecast,
        quantity_total=quantity,
        quantity_available=quantity,
        quantity_reserved=Decimal("0.000"),
        unit_price=unit_price,
        delivery_mode=delivery_mode,
        delivery_radius_km=delivery_radius_km,
        delivery_fee=delivery_fee,
        notes=notes or None,
        photo_path=photo_path or None,
        status=status or ListingStatus.ACTIVE,
        expires_at=expires_at,
        published_at=now,
    )


def update_listing(
    *,
    listing,
    quantity_total,
    unit_price,
    delivery_mode,
    delivery_radius_km=None,
    delivery_fee=None,
    notes=None,
    status=ListingStatus.ACTIVE,
    expires_at=None,
    photo_path=None,
):
    quantity_total = Decimal(str(quantity_total))
    unit_price = Decimal(str(unit_price))
    reserved_quantity = Decimal(str(listing.quantity_reserved or 0))
    now = timezone.now()

    has_stock_source = bool(listing.stock_id)
    has_forecast_source = bool(listing.forecast_id)
    if has_stock_source == has_forecast_source:
        raise MarketplaceServiceError(
            "Anúncio com origem inválida. Ajuste os dados da listing para usar stock atual ou previsão futura."
        )

    if has_stock_source:
        source_available = get_max_publishable_quantity(listing.stock)
    else:
        if not listing.forecast:
            raise MarketplaceServiceError("A previsão associada ao anúncio não existe.")
        source_available = get_forecast_available_quantity(
            listing.forecast,
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

    max_allowed_total = max(
        source_available + reserved_quantity,
        Decimal(str(listing.quantity_total or 0)),
    )
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
            "notes",
            "status",
            "expires_at",
            "photo_path",
            "updated_at",
        ]
    )
    return listing

