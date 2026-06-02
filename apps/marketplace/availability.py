from decimal import Decimal

from django.db.models import Case, CharField, Count, Max, Min, Q, Value, When

from apps.catalog.models import Product
from apps.inventory.models import ProducerProduct, ProductionForecast, Stock
from apps.marketplace.constants import (
    LISTING_SOURCE_FORECAST,
    LISTING_SOURCE_STOCK,
)
from apps.marketplace.exceptions import MarketplaceServiceError
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.marketplace.utils import quantize_qty
from apps.needs.models import NeedResponseStatus


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
