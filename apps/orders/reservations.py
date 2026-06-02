"""Order domain services: reservations."""

from apps.common.audit import log_audit_event
from apps.inventory.models import ProducerProduct, ProductionForecast, Stock, StockMovement, StockMovementType
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.needs.models import NeedResponseStatus
from apps.orders.models import DeliveryMethod, OrderItem
from decimal import Decimal
from django.db.models import Sum
from django.utils import timezone
from apps.orders.constants import RESERVED_ORDER_ITEM_STATUSES
from apps.orders.exceptions import OrderServiceError
from apps.orders.utils import _audit_qty, quantize_qty


def _log_listing_status_if_changed(listing, *, previous_status, acting_user, notes):
    if previous_status == listing.status:
        return
    log_audit_event(
        actor=acting_user,
        action="LISTING_STATUS_CHANGED",
        entity_type="marketplace_listings",
        entity_id=listing.id,
        notes=notes,
        old_values={"status": previous_status},
        new_values={
            "listing_id": str(listing.id),
            "product_id": str(listing.product_id),
            "need_id": str(listing.need_id) if listing.need_id else None,
            "status": listing.status,
            "quantity_available": _audit_qty(listing.quantity_available),
            "quantity_reserved": _audit_qty(listing.quantity_reserved),
        },
    )


def _listing_source_kind(listing):
    has_stock_source, has_forecast_source = _validate_listing_source_xor(listing)
    if has_stock_source:
        return "stock"
    if has_forecast_source:
        return "forecast"
    raise OrderServiceError("Não foi possível determinar a origem da listing.")


def _map_delivery_method_from_listing(listing):
    if listing.delivery_mode == "PICKUP":
        return DeliveryMethod.PICKUP
    if listing.delivery_mode == "DELIVERY":
        return DeliveryMethod.DELIVERY
    if listing.delivery_mode == "BOTH":
        return DeliveryMethod.MIXED
    return None


def _validate_listing_source_xor(listing):
    has_stock_source = bool(getattr(listing, "stock_id", None))
    has_forecast_source = bool(getattr(listing, "forecast_id", None))
    if has_stock_source == has_forecast_source:
        raise OrderServiceError(
            "Anúncio com origem inválida (stock/previsão). Contacte o administrador."
        )
    return has_stock_source, has_forecast_source


def _is_persisted_model_instance(instance):
    state = getattr(instance, "_state", None)
    return bool(getattr(instance, "pk", None)) and state is not None and not state.adding


def _lock_listing_for_order(listing):
    if not _is_persisted_model_instance(listing):
        return listing

    return (
        MarketplaceListing.objects
        # Lock only marketplace_listings. Optional relations such as stock,
        # forecast and need are LEFT JOINs; PostgreSQL rejects FOR UPDATE on
        # the nullable side of an outer join.
        .select_for_update(of=("self",))
        .select_related("product", "producer", "stock", "forecast", "need", "need__producer")
        .get(id=listing.id)
    )


def _is_listing_expired(listing, now=None):
    expires_at = getattr(listing, "expires_at", None)
    return bool(expires_at and expires_at <= (now or timezone.now()))


def _validate_listing_can_be_ordered(*, listing, buyer_producer, quantity):
    if listing.producer_id == buyer_producer.id:
        raise OrderServiceError("Não pode criar uma encomenda a partir do seu próprio anúncio.")

    if listing.status != ListingStatus.ACTIVE:
        raise OrderServiceError("O anúncio já não está ativo.")
    if _is_listing_expired(listing):
        raise OrderServiceError("O anúncio já expirou e não pode ser comprado.")

    if listing.need_id and listing.need_response_status == NeedResponseStatus.REJECTED:
        raise OrderServiceError("Esta oferta foi rejeitada e já não pode ser comprada.")

    if listing.need_id and listing.need_response_status != NeedResponseStatus.PENDING:
        raise OrderServiceError("Esta oferta já não está pendente e não pode ser comprada.")

    if listing.need_id and getattr(getattr(listing, "need", None), "producer_id", None) != buyer_producer.id:
        raise OrderServiceError("Esta oferta é dirigida ao produtor da necessidade e não está disponível para esta conta.")

    quantity = quantize_qty(quantity)
    if quantity <= 0:
        raise OrderServiceError("A quantidade tem de ser superior a zero.")

    available_quantity = quantize_qty(Decimal(str(listing.quantity_available or 0)))
    if quantity > available_quantity:
        raise OrderServiceError(
            f"A quantidade pedida excede a disponível ({available_quantity} {listing.product.unit})."
        )


def _update_stock_reserved(stock, quantity, acting_user):
    if not stock:
        return

    quantity = quantize_qty(quantity)
    previous_reserved = quantize_qty(Decimal(str(stock.reserved_quantity or 0)))
    new_reserved = quantize_qty(previous_reserved + quantity)
    current_quantity = quantize_qty(Decimal(str(stock.current_quantity or 0)))

    if new_reserved > current_quantity:
        product = getattr(stock, "product", None)
        product_name = getattr(product, "name", "produto") or "produto"
        product_unit = getattr(product, "unit", "") or ""
        free_capacity = quantize_qty(max(current_quantity - previous_reserved, Decimal("0.000")))
        raise OrderServiceError(
            f"O stock de {product_name} já não chega para reservar esta encomenda "
            f"(disponível: {free_capacity} {product_unit}). "
            "Atualize a página e tente novamente."
        )

    stock.reserved_quantity = new_reserved

    update_fields = ["reserved_quantity"]

    if hasattr(stock, "updated_by"):
        stock.updated_by = acting_user
        update_fields.append("updated_by")

    if hasattr(stock, "last_updated_at"):
        stock.last_updated_at = timezone.now()
        update_fields.append("last_updated_at")

    if hasattr(stock, "updated_at"):
        stock.updated_at = timezone.now()
        update_fields.append("updated_at")

    stock.save(update_fields=update_fields)
    log_audit_event(
        actor=acting_user,
        action="STOCK_RESERVATION_CHANGED",
        entity_type="stocks",
        entity_id=stock.id,
        notes="Quantidade reservada para uma encomenda.",
        old_values={"reserved_quantity": str(previous_reserved)},
        new_values={
            "stock_id": str(stock.id),
            "product_id": str(stock.product_id),
            "reserved_quantity": _audit_qty(stock.reserved_quantity),
            "quantity_delta": _audit_qty(quantity),
        },
    )


def _update_forecast_reserved(forecast, quantity, acting_user=None):
    if not forecast:
        return

    quantity = quantize_qty(quantity)
    previous_reserved = quantize_qty(Decimal(str(forecast.reserved_quantity or 0)))
    forecast.reserved_quantity = quantize_qty(
        Decimal(str(forecast.reserved_quantity or 0)) + quantity
    )
    forecast.updated_at = timezone.now()
    forecast.save(update_fields=["reserved_quantity", "updated_at"])
    log_audit_event(
        actor=acting_user,
        action="FORECAST_RESERVATION_CHANGED",
        entity_type="production_forecasts",
        entity_id=forecast.id,
        notes="Quantidade de produção prevista reservada para uma encomenda.",
        old_values={"reserved_quantity": str(previous_reserved)},
        new_values={
            "forecast_id": str(forecast.id),
            "product_id": str(forecast.product_id),
            "reserved_quantity": _audit_qty(forecast.reserved_quantity),
            "quantity_delta": _audit_qty(quantity),
        },
    )


def _consume_stock_reservation(stock, quantity, acting_user, *, order=None):
    if not stock:
        return

    quantity = quantize_qty(quantity)

    current_quantity = Decimal(str(stock.current_quantity or 0))
    reserved_quantity = Decimal(str(stock.reserved_quantity or 0))

    stock.current_quantity = quantize_qty(max(current_quantity - quantity, Decimal("0.000")))
    stock.reserved_quantity = quantize_qty(max(reserved_quantity - quantity, Decimal("0.000")))

    update_fields = ["current_quantity", "reserved_quantity"]

    if hasattr(stock, "updated_by"):
        stock.updated_by = acting_user
        update_fields.append("updated_by")

    if hasattr(stock, "last_updated_at"):
        stock.last_updated_at = timezone.now()
        update_fields.append("last_updated_at")

    if hasattr(stock, "updated_at"):
        stock.updated_at = timezone.now()
        update_fields.append("updated_at")

    stock.save(update_fields=update_fields)
    log_audit_event(
        actor=acting_user,
        action="STOCK_RESERVATION_CHANGED",
        entity_type="stocks",
        entity_id=stock.id,
        notes="Reserva consumida após conclusão da encomenda.",
        old_values={"current_quantity": _audit_qty(current_quantity), "reserved_quantity": _audit_qty(reserved_quantity)},
        new_values={
            "stock_id": str(stock.id),
            "product_id": str(stock.product_id),
            "current_quantity": _audit_qty(stock.current_quantity),
            "reserved_quantity": _audit_qty(stock.reserved_quantity),
        },
    )

    if order is not None:
        movement = StockMovement.objects.create(
            stock=stock,
            movement_type=StockMovementType.ORDER_OUT,
            quantity_delta=-quantity,
            reference_type="ORDER",
            reference_id=order.id,
            notes=f"Saída por conclusão da encomenda #{order.order_number}.",
            performed_by=acting_user,
        )
        log_audit_event(
            actor=acting_user,
            action="STOCK_MOVEMENT_CREATED",
            entity_type="stock_movements",
            entity_id=movement.id,
            notes=movement.notes,
            new_values={
                "stock_id": str(stock.id),
                "product_id": str(stock.product_id),
                "movement_type": movement.movement_type,
                "quantity_delta": _audit_qty(movement.quantity_delta),
                "order_id": str(order.id),
            },
        )

    _reconcile_listings_against_stock_capacity(stock, acting_user=acting_user)


def _reconcile_listings_against_stock_capacity(stock, *, acting_user=None):
    """
    Após o stock baixar (entrega concluída), verifica se outros anúncios
    ativos do mesmo stock excedem agora a capacidade. Se sim, reduz
    proporcionalmente para não deixar o sistema em estado inválido.
    """
    if not stock:
        return

    from apps.inventory.services import (
        get_listings_blocking_stock_decrease,
        reduce_listings_to_fit_stock,
    )

    new_quantity = Decimal(str(stock.current_quantity or 0))
    blocking = get_listings_blocking_stock_decrease(stock, new_quantity)
    if blocking["deficit"] <= Decimal("0.000"):
        return

    reduce_listings_to_fit_stock(
        stock=stock,
        new_quantity=new_quantity,
        mode="proportional",
        acting_user=acting_user,
    )


def _consume_forecast_reservation(forecast, quantity, acting_user=None):
    if not forecast:
        return

    quantity = quantize_qty(quantity)
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    forecast.reserved_quantity = quantize_qty(max(reserved_quantity - quantity, Decimal("0.000")))
    forecast.updated_at = timezone.now()
    forecast.save(update_fields=["reserved_quantity", "updated_at"])
    log_audit_event(
        actor=acting_user,
        action="FORECAST_RESERVATION_CHANGED",
        entity_type="production_forecasts",
        entity_id=forecast.id,
        notes="Reserva de previsão consumida após conclusão da encomenda.",
        old_values={"reserved_quantity": _audit_qty(reserved_quantity)},
        new_values={
            "forecast_id": str(forecast.id),
            "product_id": str(forecast.product_id),
            "reserved_quantity": _audit_qty(forecast.reserved_quantity),
        },
    )


def _release_stock_reservation(stock, quantity, acting_user):
    if not stock:
        return

    quantity = quantize_qty(quantity)
    reserved_quantity = Decimal(str(stock.reserved_quantity or 0))
    stock.reserved_quantity = quantize_qty(max(reserved_quantity - quantity, Decimal("0.000")))

    update_fields = ["reserved_quantity"]

    if hasattr(stock, "updated_by"):
        stock.updated_by = acting_user
        update_fields.append("updated_by")

    if hasattr(stock, "last_updated_at"):
        stock.last_updated_at = timezone.now()
        update_fields.append("last_updated_at")

    if hasattr(stock, "updated_at"):
        stock.updated_at = timezone.now()
        update_fields.append("updated_at")

    stock.save(update_fields=update_fields)
    log_audit_event(
        actor=acting_user,
        action="STOCK_RESERVATION_CHANGED",
        entity_type="stocks",
        entity_id=stock.id,
        notes="Reserva libertada após cancelamento ou reconciliação da encomenda.",
        old_values={"reserved_quantity": _audit_qty(reserved_quantity)},
        new_values={
            "stock_id": str(stock.id),
            "product_id": str(stock.product_id),
            "reserved_quantity": _audit_qty(stock.reserved_quantity),
        },
    )


def _expected_reserved_quantity_for_listing(listing_id):
    total = (
        OrderItem.objects
        .filter(
            listing_id=listing_id,
            item_status__in=RESERVED_ORDER_ITEM_STATUSES,
        )
        .aggregate(total=Sum("quantity"))
        .get("total")
        or Decimal("0.000")
    )
    return quantize_qty(total)


def _reconcile_listing_reservation(listing_id, acting_user, *, strict=True):
    listing = (
        MarketplaceListing.objects
        .select_for_update()
        .get(id=listing_id)
    )
    has_stock_source, has_forecast_source = _validate_listing_source_xor(listing)
    previous_status = listing.status

    expected_reserved = _expected_reserved_quantity_for_listing(listing.id)
    current_reserved = quantize_qty(Decimal(str(listing.quantity_reserved or 0)))
    if expected_reserved == current_reserved:
        return listing

    current_available = quantize_qty(Decimal(str(listing.quantity_available or 0)))
    source_delta = Decimal("0.000")

    if expected_reserved > current_reserved:
        reserve_delta = quantize_qty(expected_reserved - current_reserved)
        if reserve_delta > current_available:
            if strict:
                raise OrderServiceError(
                    (
                        "Não existe quantidade suficiente no anúncio para reservar esta encomenda. "
                        "Atualize o anúncio ou reverta a operação."
                    )
                )
            reserve_delta = quantize_qty(current_available)
            if reserve_delta <= Decimal("0.000"):
                return listing
        listing.quantity_available = quantize_qty(current_available - reserve_delta)
        listing.quantity_reserved = quantize_qty(current_reserved + reserve_delta)
        source_delta = reserve_delta
    else:
        release_delta = quantize_qty(current_reserved - expected_reserved)
        listing.quantity_available = quantize_qty(current_available + release_delta)
        listing.quantity_reserved = quantize_qty(max(current_reserved - release_delta, Decimal("0.000")))
        source_delta = -release_delta

    update_fields = ["quantity_available", "quantity_reserved", "updated_at"]
    if (
        listing.status not in {ListingStatus.CANCELLED, ListingStatus.EXPIRED}
        and listing.quantity_available <= 0
        and listing.quantity_reserved > 0
    ):
        listing.status = ListingStatus.RESERVED
        update_fields.append("status")
    elif (
        listing.status not in {ListingStatus.CANCELLED, ListingStatus.EXPIRED}
        and listing.quantity_available <= 0
        and listing.quantity_reserved <= 0
    ):
        listing.status = ListingStatus.CLOSED
        update_fields.append("status")
    elif listing.status in {ListingStatus.RESERVED, ListingStatus.CLOSED} and listing.quantity_available > 0:
        listing.status = ListingStatus.ACTIVE
        update_fields.append("status")

    listing.updated_at = timezone.now()
    listing.save(update_fields=list(dict.fromkeys(update_fields)))
    _log_listing_status_if_changed(
        listing,
        previous_status=previous_status,
        acting_user=acting_user,
        notes="Estado do anúncio alterado ao reconciliar reservas de encomendas.",
    )

    if source_delta > 0:
        if has_stock_source:
            stock = Stock.objects.select_for_update().get(id=listing.stock_id)
            _update_stock_reserved(stock, source_delta, acting_user)
        elif has_forecast_source:
            forecast = ProductionForecast.objects.select_for_update().get(id=listing.forecast_id)
            forecast_saleable = quantize_qty(
                Decimal(str(forecast.forecast_quantity or 0))
                - Decimal(str(forecast.reserved_quantity or 0))
            )
            if source_delta > forecast_saleable:
                raise OrderServiceError(
                    (
                        "A quantidade comprometida excede a previsão disponível para pré-venda "
                        f"({forecast_saleable} {listing.product.unit})."
                    )
                )
            _update_forecast_reserved(forecast, source_delta, acting_user)
    elif source_delta < 0:
        source_release = quantize_qty(abs(source_delta))
        if has_stock_source:
            stock = Stock.objects.select_for_update().get(id=listing.stock_id)
            _release_stock_reservation(stock, source_release, acting_user)
        elif has_forecast_source:
            forecast = ProductionForecast.objects.select_for_update().get(id=listing.forecast_id)
            _release_forecast_reservation(forecast, source_release, acting_user)

    return listing


def _reserve_listing_quantity(listing_id, quantity, acting_user):
    listing = (
        MarketplaceListing.objects
        .select_for_update()
        .get(id=listing_id)
    )
    has_stock_source, has_forecast_source = _validate_listing_source_xor(listing)
    previous_status = listing.status

    if listing.status != ListingStatus.ACTIVE:
        raise OrderServiceError("O anúncio já não está ativo.")
    if _is_listing_expired(listing):
        raise OrderServiceError("O anúncio já expirou e não pode ser comprado.")

    quantity = quantize_qty(quantity)
    available_quantity = quantize_qty(Decimal(str(listing.quantity_available or 0)))

    if quantity <= 0:
        raise OrderServiceError("A quantidade tem de ser superior a zero.")

    if quantity > available_quantity:
        raise OrderServiceError(
            f"A quantidade pedida excede a disponível ({available_quantity} {listing.product.unit})."
        )

    listing.quantity_available = quantize_qty(available_quantity - quantity)
    listing.quantity_reserved = quantize_qty(Decimal(str(listing.quantity_reserved or 0)) + quantity)

    update_fields = ["quantity_available", "quantity_reserved", "updated_at"]

    if listing.quantity_available <= 0 and listing.quantity_reserved > 0:
        listing.status = ListingStatus.RESERVED
        update_fields.append("status")
    elif listing.quantity_available > 0:
        listing.status = ListingStatus.ACTIVE
        update_fields.append("status")

    listing.updated_at = timezone.now()
    listing.save(update_fields=update_fields)
    _log_listing_status_if_changed(
        listing,
        previous_status=previous_status,
        acting_user=acting_user,
        notes="Estado do anúncio alterado após reservar quantidade para encomenda.",
    )

    if has_stock_source:
        stock = Stock.objects.select_for_update().get(id=listing.stock_id)
        _update_stock_reserved(stock, quantity, acting_user)
    elif has_forecast_source:
        forecast = ProductionForecast.objects.select_for_update().get(id=listing.forecast_id)
        forecast_saleable = quantize_qty(
            Decimal(str(forecast.forecast_quantity or 0))
            - Decimal(str(forecast.reserved_quantity or 0))
        )
        if quantity > forecast_saleable:
            raise OrderServiceError(
                (
                    "A quantidade pedida excede a previsão disponível para pré-venda "
                    f"({forecast_saleable} {listing.product.unit})."
                )
            )
        _update_forecast_reserved(forecast, quantity, acting_user)

    return listing


def _release_listing_reservation(listing_id, quantity, acting_user):
    listing = (
        MarketplaceListing.objects
        .select_for_update()
        .get(id=listing_id)
    )
    has_stock_source, has_forecast_source = _validate_listing_source_xor(listing)
    previous_status = listing.status

    quantity = quantize_qty(quantity)
    reserved_quantity = Decimal(str(listing.quantity_reserved or 0))
    available_quantity = Decimal(str(listing.quantity_available or 0))

    listing.quantity_reserved = quantize_qty(max(reserved_quantity - quantity, Decimal("0.000")))
    listing.quantity_available = quantize_qty(available_quantity + quantity)

    if listing.status in {ListingStatus.RESERVED, ListingStatus.CLOSED} and listing.quantity_available > 0:
        listing.status = ListingStatus.ACTIVE

    listing.updated_at = timezone.now()
    listing.save(update_fields=["quantity_reserved", "quantity_available", "status", "updated_at"])
    _log_listing_status_if_changed(
        listing,
        previous_status=previous_status,
        acting_user=acting_user,
        notes="Estado do anúncio alterado após libertar reserva de encomenda.",
    )

    if has_stock_source:
        stock = Stock.objects.select_for_update().get(id=listing.stock_id)
        _release_stock_reservation(stock, quantity, acting_user)
    elif has_forecast_source:
        forecast = ProductionForecast.objects.select_for_update().get(id=listing.forecast_id)
        _release_forecast_reservation(forecast, quantity, acting_user)

    return listing


def _consume_listing_reservation(listing_id, quantity, acting_user, *, order=None):
    listing = (
        MarketplaceListing.objects
        .select_for_update()
        .get(id=listing_id)
    )
    has_stock_source, has_forecast_source = _validate_listing_source_xor(listing)
    previous_status = listing.status

    quantity = quantize_qty(quantity)
    reserved_quantity = Decimal(str(listing.quantity_reserved or 0))

    listing.quantity_reserved = quantize_qty(max(reserved_quantity - quantity, Decimal("0.000")))

    if listing.quantity_available <= 0 and listing.quantity_reserved <= 0:
        listing.status = ListingStatus.CLOSED
    elif listing.quantity_available <= 0 and listing.quantity_reserved > 0:
        listing.status = ListingStatus.RESERVED
    elif listing.status in {ListingStatus.CLOSED, ListingStatus.RESERVED} and listing.quantity_available > 0:
        listing.status = ListingStatus.ACTIVE

    listing.updated_at = timezone.now()
    listing.save(update_fields=["quantity_reserved", "status", "updated_at"])
    _log_listing_status_if_changed(
        listing,
        previous_status=previous_status,
        acting_user=acting_user,
        notes="Estado do anúncio alterado após conclusão da encomenda.",
    )

    if has_stock_source:
        stock = Stock.objects.select_for_update().get(id=listing.stock_id)
        _consume_stock_reservation(stock, quantity, acting_user, order=order)
    elif has_forecast_source:
        forecast = ProductionForecast.objects.select_for_update().get(id=listing.forecast_id)
        _consume_forecast_reservation(forecast, quantity, acting_user)

    return listing


def _ensure_buyer_product_link(buyer_producer, product):
    producer_product, created = ProducerProduct.objects.get_or_create(
        producer=buyer_producer,
        product=product,
        defaults={"is_active": True},
    )

    if not created and not producer_product.is_active:
        producer_product.is_active = True
        producer_product.updated_at = timezone.now()
        producer_product.save(update_fields=["is_active", "updated_at"])

    return producer_product


def _ensure_buyer_stock(buyer_producer, product, acting_user):
    now = timezone.now()
    defaults = {
        "current_quantity": quantize_qty(Decimal("0")),
        "reserved_quantity": quantize_qty(Decimal("0")),
        "safety_stock": quantize_qty(Decimal("0")),
        "last_updated_at": now,
    }

    if hasattr(Stock, "updated_by"):
        defaults["updated_by"] = acting_user

    stock, created = (
        Stock.objects
        .select_for_update()
        .get_or_create(
            producer=buyer_producer,
            product=product,
            defaults=defaults,
        )
    )
    if created:
        log_audit_event(
            actor=acting_user,
            action="STOCK_CREATED",
            entity_type="stocks",
            entity_id=stock.id,
            notes="Stock criado automaticamente ao receber uma encomenda.",
            new_values={
                "producer_id": str(stock.producer_id),
                "product_id": str(stock.product_id),
                "current_quantity": _audit_qty(stock.current_quantity),
                "reserved_quantity": _audit_qty(stock.reserved_quantity),
            },
        )

    changed_fields = []
    if stock.current_quantity is None:
        stock.current_quantity = quantize_qty(Decimal("0"))
        changed_fields.append("current_quantity")
    if stock.reserved_quantity is None:
        stock.reserved_quantity = quantize_qty(Decimal("0"))
        changed_fields.append("reserved_quantity")
    if stock.safety_stock is None:
        stock.safety_stock = quantize_qty(Decimal("0"))
        changed_fields.append("safety_stock")
    if getattr(stock, "last_updated_at", None) is None:
        stock.last_updated_at = now
        changed_fields.append("last_updated_at")

    if changed_fields:
        if hasattr(stock, "updated_at"):
            stock.updated_at = now
            changed_fields.append("updated_at")
        stock.save(update_fields=list(dict.fromkeys(changed_fields)))

    return stock


def _register_buyer_order_inbound(*, buyer_producer, order, product, quantity, acting_user):
    _ensure_buyer_product_link(buyer_producer, product)
    stock = _ensure_buyer_stock(buyer_producer, product, acting_user)

    qty = quantize_qty(quantity)
    previous_quantity = quantize_qty(Decimal(str(stock.current_quantity or 0)))
    stock.current_quantity = quantize_qty(Decimal(str(stock.current_quantity or 0)) + qty)

    update_fields = ["current_quantity"]
    if hasattr(stock, "updated_by"):
        stock.updated_by = acting_user
        update_fields.append("updated_by")
    if hasattr(stock, "last_updated_at"):
        stock.last_updated_at = timezone.now()
        update_fields.append("last_updated_at")
    if hasattr(stock, "updated_at"):
        stock.updated_at = timezone.now()
        update_fields.append("updated_at")
    stock.save(update_fields=update_fields)
    log_audit_event(
        actor=acting_user,
        action="STOCK_UPDATED",
        entity_type="stocks",
        entity_id=stock.id,
        notes=f"Entrada de stock pela receção da encomenda #{order.order_number}.",
        old_values={"current_quantity": str(previous_quantity)},
        new_values={
            "stock_id": str(stock.id),
            "product_id": str(stock.product_id),
            "current_quantity": _audit_qty(stock.current_quantity),
        },
    )

    movement = StockMovement.objects.create(
        stock=stock,
        movement_type=StockMovementType.ORDER_IN,
        quantity_delta=qty,
        reference_type="ORDER",
        reference_id=order.id,
        notes=f"Entrada por receção da encomenda #{order.order_number}.",
        performed_by=acting_user,
    )
    log_audit_event(
        actor=acting_user,
        action="STOCK_MOVEMENT_CREATED",
        entity_type="stock_movements",
        entity_id=movement.id,
        notes=movement.notes,
        new_values={
            "stock_id": str(stock.id),
            "product_id": str(stock.product_id),
            "movement_type": movement.movement_type,
            "quantity_delta": _audit_qty(movement.quantity_delta),
            "order_id": str(order.id),
        },
    )


def _release_forecast_reservation(forecast, quantity, acting_user=None):
    if not forecast:
        return

    quantity = quantize_qty(quantity)
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    forecast.reserved_quantity = quantize_qty(max(reserved_quantity - quantity, Decimal("0.000")))
    forecast.updated_at = timezone.now()
    forecast.save(update_fields=["reserved_quantity", "updated_at"])
    log_audit_event(
        actor=acting_user,
        action="FORECAST_RESERVATION_CHANGED",
        entity_type="production_forecasts",
        entity_id=forecast.id,
        notes="Reserva de produção futura libertada.",
        old_values={"reserved_quantity": _audit_qty(reserved_quantity)},
        new_values={
            "forecast_id": str(forecast.id),
            "product_id": str(forecast.product_id),
            "reserved_quantity": _audit_qty(forecast.reserved_quantity),
        },
    )
