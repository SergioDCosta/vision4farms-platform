from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.common.audit import log_audit_event
from apps.inventory.audit import (
    _audit_qty,
    _forecast_audit_values,
    _log_stock_movement,
    _stock_audit_values,
)
from apps.inventory.constants import ZERO
from apps.inventory.models import (
    ForecastSourceSystem,
    ProductionForecast,
    Stock,
    StockMovement,
    StockMovementType,
)
from apps.marketplace.models import ListingStatus, MarketplaceListing


def _forecast_saleable_quantity(forecast):
    forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    available = forecast_quantity - reserved_quantity
    return max(available, ZERO)


def get_product_forecasts(producer, product_id):
    forecasts = list(
        ProductionForecast.objects
        .filter(producer=producer, product_id=product_id, forecast_quantity__gt=ZERO)
        .order_by("period_start", "period_end", "-created_at")
    )

    if not forecasts:
        return []

    forecast_ids = [forecast.id for forecast in forecasts]
    listings = (
        MarketplaceListing.objects
        .filter(
            producer=producer,
            product_id=product_id,
            forecast_id__in=forecast_ids,
        )
        .order_by("-published_at", "-created_at")
    )

    active_listing_by_forecast = {}
    latest_listing_by_forecast = {}
    open_published_by_forecast = defaultdict(lambda: ZERO)
    for listing in listings:
        if listing.forecast_id not in latest_listing_by_forecast:
            latest_listing_by_forecast[listing.forecast_id] = listing
        if (
            listing.status == ListingStatus.ACTIVE
            and listing.forecast_id not in active_listing_by_forecast
        ):
            active_listing_by_forecast[listing.forecast_id] = listing
        if listing.status in {ListingStatus.ACTIVE, ListingStatus.RESERVED}:
            open_published_by_forecast[listing.forecast_id] += Decimal(
                str(listing.quantity_available or 0)
            )

    today = timezone.localdate()
    rows = []
    for forecast in forecasts:
        forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
        reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
        available_quantity = forecast_quantity - reserved_quantity
        open_published_quantity = Decimal(str(open_published_by_forecast.get(forecast.id, ZERO))).quantize(Decimal("0.001"))
        # "Disponível pré-venda" no card reflete o que ainda está disponível
        # nos anúncios do marketplace associados a esta previsão.
        saleable_quantity = Decimal(str(max(open_published_quantity, ZERO))).quantize(Decimal("0.001"))
        publish_ready_quantity = Decimal(str(max(available_quantity - open_published_quantity, ZERO))).quantize(Decimal("0.001"))
        period_start_local = (
            timezone.localtime(forecast.period_start)
            if getattr(forecast, "period_start", None) and timezone.is_aware(forecast.period_start)
            else getattr(forecast, "period_start", None)
        )
        assimilable_quantity = Decimal(str(max(available_quantity, ZERO))).quantize(Decimal("0.001"))
        can_assimilate_now = bool(
            period_start_local and today >= period_start_local.date() and assimilable_quantity > ZERO
        )

        linked_listing = (
            active_listing_by_forecast.get(forecast.id)
            or latest_listing_by_forecast.get(forecast.id)
        )

        marketplace_status_label = "Inativa"
        marketplace_status_class = "inv-status inv-status--normal"
        if linked_listing:
            if linked_listing.status == ListingStatus.ACTIVE:
                marketplace_status_label = "Ativa"
                marketplace_status_class = "inv-status inv-status--excess"
            elif linked_listing.status == ListingStatus.RESERVED:
                marketplace_status_label = "Reservada"
            elif linked_listing.status == ListingStatus.CANCELLED:
                marketplace_status_label = "Desativada"
            elif linked_listing.status == ListingStatus.EXPIRED:
                marketplace_status_label = "Expirada"
            elif linked_listing.status == ListingStatus.CLOSED:
                marketplace_status_label = "Fechada"
            elif hasattr(linked_listing, "get_status_display"):
                marketplace_status_label = linked_listing.get_status_display()
        elif forecast.is_marketplace_enabled and publish_ready_quantity > ZERO:
            marketplace_status_label = "Pronta para publicar"

        rows.append({
            "forecast": forecast,
            "forecast_quantity": forecast_quantity,
            "reserved_quantity": reserved_quantity,
            "forecast_available": available_quantity,
            "forecast_saleable": saleable_quantity,
            "assimilable_quantity": assimilable_quantity,
            "open_published_quantity": open_published_quantity,
            "linked_listing": linked_listing,
            "marketplace_status_label": marketplace_status_label,
            "marketplace_status_class": marketplace_status_class,
            "can_assimilate_now": can_assimilate_now,
        })

    return rows


def _forecast_periods_overlap(*, start_a, end_a, start_b, end_b):
    if not start_a or not end_a or not start_b or not end_b:
        return True
    return start_a <= end_b and end_a >= start_b


@transaction.atomic
def save_product_forecast(
    *,
    producer,
    product,
    forecast_quantity,
    period_start=None,
    period_end=None,
    is_marketplace_enabled=False,
    user=None,
    forecast_id=None,
):
    quantity = Decimal(str(forecast_quantity or 0))
    if quantity <= ZERO:
        raise ValidationError("A quantidade prevista deve ser superior a zero.")

    if not period_start or not period_end:
        raise ValidationError("Indica o início e o fim do período da previsão.")

    if period_start and period_end and period_end < period_start:
        raise ValidationError("O período final não pode ser anterior ao período inicial.")

    existing_forecasts_qs = (
        ProductionForecast.objects
        .select_for_update()
        .filter(producer=producer, product=product)
    )
    existing_forecasts = list(
        existing_forecasts_qs.order_by("period_start", "period_end", "-created_at")
    )

    created = False
    if forecast_id:
        forecast = next(
            (row for row in existing_forecasts if str(row.id) == str(forecast_id)),
            None,
        )
        if not forecast:
            raise ValidationError("Previsão não encontrada para este produto.")
        old_values = _forecast_audit_values(forecast)
    else:
        forecast = ProductionForecast(
            producer=producer,
            product=product,
            reserved_quantity=ZERO,
            source_system=ForecastSourceSystem.MANUAL,
        )
        created = True
        old_values = None

    other_forecasts = [
        row for row in existing_forecasts
        if not getattr(forecast, "id", None) or row.id != forecast.id
    ]

    for other in other_forecasts:
        if not other.period_start or not other.period_end:
            raise ValidationError(
                "Existe uma previsão antiga sem período completo. Edite/limpe essa previsão antes de criar novas."
            )
        if _forecast_periods_overlap(
            start_a=period_start,
            end_a=period_end,
            start_b=other.period_start,
            end_b=other.period_end,
        ):
            overlap_start = timezone.localtime(other.period_start) if timezone.is_aware(other.period_start) else other.period_start
            overlap_end = timezone.localtime(other.period_end) if timezone.is_aware(other.period_end) else other.period_end
            raise ValidationError(
                (
                    "O intervalo desta previsão sobrepõe-se a uma previsão já existente "
                    f"({overlap_start.strftime('%d/%m/%Y')} - {overlap_end.strftime('%d/%m/%Y')})."
                )
            )

    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    open_published_quantity = ZERO
    if getattr(forecast, "id", None):
        open_published_quantity = Decimal(
            str(
                MarketplaceListing.objects.filter(
                    forecast_id=forecast.id,
                    status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
                ).aggregate(total=Sum("quantity_available"))["total"]
                or ZERO
            )
        )

    minimum_allowed_quantity = reserved_quantity + open_published_quantity
    if quantity < minimum_allowed_quantity:
        raise ValidationError(
            (
                "A quantidade prevista não pode ser inferior à quantidade já comprometida "
                f"({minimum_allowed_quantity})."
            )
        )

    forecast.forecast_quantity = quantity
    forecast.period_start = period_start
    forecast.period_end = period_end
    forecast.is_marketplace_enabled = bool(is_marketplace_enabled)
    if getattr(forecast, "updated_at", None) is not None:
        forecast.updated_at = timezone.now()

    saleable_quantity = max(quantity - reserved_quantity, ZERO)
    if forecast.is_marketplace_enabled and saleable_quantity <= ZERO:
        raise ValidationError(
            "Só pode ativar no marketplace quando existir quantidade disponível para pré-venda."
        )

    if created:
        forecast.save()
    else:
        update_fields = [
            "forecast_quantity",
            "period_start",
            "period_end",
            "is_marketplace_enabled",
            "updated_at",
        ]
        forecast.save(update_fields=update_fields)

    log_audit_event(
        actor=user,
        action="FORECAST_CREATED" if created else "FORECAST_UPDATED",
        entity_type="production_forecasts",
        entity_id=forecast.id,
        notes="Produção futura registada." if created else "Produção futura atualizada.",
        old_values=old_values,
        new_values=_forecast_audit_values(forecast),
    )
    return forecast, created


@transaction.atomic
def delete_product_forecast(*, producer, product, forecast_id, user=None):
    forecast = (
        ProductionForecast.objects
        .select_for_update()
        .filter(
            id=forecast_id,
            producer=producer,
            product=product,
        )
        .first()
    )
    if not forecast:
        raise ValidationError("Previsão não encontrada para este produto.")

    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    if reserved_quantity > ZERO:
        raise ValidationError(
            "Esta previsão não pode ser eliminada porque já tem quantidade reservada em encomendas."
        )

    has_open_listings = MarketplaceListing.objects.filter(
        forecast_id=forecast.id,
        status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
    ).exists()
    if has_open_listings:
        raise ValidationError(
            "Esta previsão não pode ser eliminada enquanto tiver anúncios ativos/reservados associados."
        )

    previous_values = _forecast_audit_values(forecast)
    forecast.delete()
    log_audit_event(
        actor=user,
        action="FORECAST_DELETED",
        entity_type="production_forecasts",
        entity_id=forecast_id,
        notes="Produção futura removida.",
        old_values=previous_values,
    )
    return True


@transaction.atomic
def assimilate_product_forecast_to_stock(*, producer, product, forecast_id, user):
    forecast = (
        ProductionForecast.objects
        .select_for_update()
        .filter(
            id=forecast_id,
            producer=producer,
            product=product,
        )
        .first()
    )
    if not forecast:
        raise ValidationError("Previsão não encontrada para este produto.")

    if not forecast.period_start:
        raise ValidationError("Esta previsão não tem data de início válida para assimilação.")

    period_start_local = (
        timezone.localtime(forecast.period_start)
        if timezone.is_aware(forecast.period_start)
        else forecast.period_start
    )
    today = timezone.localdate()
    if period_start_local.date() > today:
        raise ValidationError(
            "Esta previsão ainda não pode ser assimilada: a data de início ainda não chegou."
        )

    forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    quantity_to_assimilate = Decimal(str(max(forecast_quantity - reserved_quantity, ZERO))).quantize(Decimal("0.001"))
    if quantity_to_assimilate <= ZERO:
        raise ValidationError(
            "Não existe quantidade disponível para assumir no stock atual nesta previsão."
        )

    open_listings = list(
        MarketplaceListing.objects
        .select_for_update()
        .filter(
            forecast_id=forecast.id,
            status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
        )
        .only("id", "quantity_available", "quantity_reserved", "status", "updated_at")
    )

    now = timezone.now()
    for listing in open_listings:
        previous_status = listing.status
        listing.quantity_available = Decimal("0.000")
        if Decimal(str(listing.quantity_reserved or 0)) > ZERO:
            listing.status = ListingStatus.RESERVED
        else:
            listing.status = ListingStatus.CLOSED
        listing.updated_at = now
        listing.save(update_fields=["quantity_available", "status", "updated_at"])
        if previous_status != listing.status:
            log_audit_event(
                actor=user,
                action="LISTING_STATUS_CHANGED",
                entity_type="marketplace_listings",
                entity_id=listing.id,
                notes="Anúncio de pré-venda encerrado ao assumir a produção no stock atual.",
                old_values={"status": previous_status},
                new_values={"status": listing.status, "quantity_available": "0.000"},
            )

    stock = (
        Stock.objects
        .select_for_update()
        .filter(producer=producer, product=product)
        .first()
    )
    if not stock:
        raise ValidationError("Stock não encontrado para este produto.")

    stock_old_values = _stock_audit_values(stock)
    forecast_old_values = _forecast_audit_values(forecast)
    stock.current_quantity = Decimal(str(stock.current_quantity or 0)) + quantity_to_assimilate
    stock.updated_by = user
    stock.last_updated_at = now
    stock.updated_at = now
    stock.save(update_fields=["current_quantity", "updated_by", "last_updated_at", "updated_at"])
    log_audit_event(
        actor=user,
        action="STOCK_UPDATED",
        entity_type="stocks",
        entity_id=stock.id,
        notes="Stock acrescido pela assimilação de produção futura.",
        old_values=stock_old_values,
        new_values=_stock_audit_values(stock),
    )

    period_label_start = period_start_local.strftime("%d/%m/%Y")
    period_end_local = (
        timezone.localtime(forecast.period_end)
        if getattr(forecast, "period_end", None) and timezone.is_aware(forecast.period_end)
        else getattr(forecast, "period_end", None)
    )
    period_label_end = period_end_local.strftime("%d/%m/%Y") if period_end_local else "—"

    movement = StockMovement.objects.create(
        stock=stock,
        movement_type=StockMovementType.IMPORT,
        quantity_delta=quantity_to_assimilate,
        reference_type="FORECAST",
        reference_id=forecast.id,
        notes=(
            "Entrada por produção futura assumida no stock atual "
            f"(período {period_label_start} - {period_label_end})."
        ),
        performed_by=user,
    )
    _log_stock_movement(movement, actor=user)

    forecast.forecast_quantity = Decimal(str(max(forecast_quantity - quantity_to_assimilate, ZERO))).quantize(Decimal("0.001"))
    if forecast.forecast_quantity <= ZERO:
        forecast.is_marketplace_enabled = False
    forecast.updated_at = now
    forecast.save(update_fields=["forecast_quantity", "is_marketplace_enabled", "updated_at"])
    log_audit_event(
        actor=user,
        action="FORECAST_ASSIMILATED",
        entity_type="production_forecasts",
        entity_id=forecast.id,
        notes="Produção futura assumida como entrada no stock atual.",
        old_values=forecast_old_values,
        new_values=_forecast_audit_values(forecast) | {
            "quantity_delta": _audit_qty(quantity_to_assimilate),
            "stock_id": str(stock.id),
        },
    )
    return quantity_to_assimilate
