from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.common.audit import log_audit_event
from apps.inventory.models import ProductionForecast, Stock
from apps.marketplace.audit import listing_audit_values
from apps.marketplace.availability import (
    _validate_listing_source_xor,
    get_forecast_available_quantity,
    get_max_publishable_quantity,
)
from apps.marketplace.constants import (
    LISTING_SOURCE_FORECAST,
    LISTING_SOURCE_STOCK,
    MARKETPLACE_FINAL_STATUSES,
)
from apps.marketplace.exceptions import MarketplaceServiceError
from apps.marketplace.models import DeliveryMode, ListingStatus, MarketplaceListing
from apps.marketplace.queries import is_listing_retired_in_marketplace
from apps.marketplace.utils import quantize_qty
from apps.needs.models import NeedResponseStatus, NeedStatus


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
        old_values = listing_audit_values(listing)
        listing.status = ListingStatus.EXPIRED
        log_audit_event(
            action="LISTING_EXPIRED",
            entity_type="marketplace_listings",
            entity_id=listing.id,
            notes="Anúncio ou proposta expirou automaticamente após atingir a data limite.",
            old_values=old_values,
            new_values=listing_audit_values(listing),
        )
    return need_responses_expired + listings_expired


def retire_listing(*, listing, acting_user=None):
    old_values = listing_audit_values(listing)
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
        new_values=listing_audit_values(listing),
    )
    return listing


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
        new_values=listing_audit_values(listing),
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

    old_values = listing_audit_values(listing)
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
        new_values=listing_audit_values(listing),
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

    old_values = listing_audit_values(listing)
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
        new_values=listing_audit_values(listing),
    )
    return listing
