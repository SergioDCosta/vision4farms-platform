from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.db.models import Max, Prefetch, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.inventory.models import (
    ProductionForecast,
    ProducerProfile,
    ProducerProduct,
    Stock,
    StockMovement,
    StockMovementType,
)
from apps.inventory.services import recalculate_needs_for_order
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.orders.models import (
    OrderGroup,
    Order,
    OrderItem,
    OrderStatusHistory,
    OrderSourceType,
    OrderStatus,
    PaymentStatus,
    OrderItemStatus,
    DeliveryMethod,
)
from apps.recommendations.models import RecommendationStatus


QTY_DECIMAL = Decimal("0.001")
MONEY_DECIMAL = Decimal("0.01")
RESERVED_ORDER_ITEM_STATUSES = (
    OrderItemStatus.PENDING,
    OrderItemStatus.CONFIRMED,
    OrderItemStatus.IN_DELIVERY,
)


class OrderServiceError(Exception):
    pass


def quantize_qty(value):
    return Decimal(str(value)).quantize(QTY_DECIMAL)


def quantize_money(value):
    return Decimal(str(value)).quantize(MONEY_DECIMAL, rounding=ROUND_HALF_UP)


def get_current_producer_for_user(user):
    if not user:
        return None
    return ProducerProfile.objects.filter(user=user).first()


def _sync_alerts_for_producers(*producers, acting_user=None):
    try:
        from apps.alerts.services import sync_alerts_for_producer
    except Exception:
        return

    seen_ids = set()
    for producer in producers:
        producer_id = getattr(producer, "id", None)
        if not producer or producer_id in seen_ids:
            continue
        seen_ids.add(producer_id)
        try:
            sync_alerts_for_producer(producer, acting_user=acting_user)
        except Exception:
            continue


def _next_order_number():
    last_number = Order.objects.aggregate(max_number=Max("order_number")).get("max_number") or 1000
    return int(last_number) + 1


def _next_group_number():
    last_number = OrderGroup.objects.aggregate(max_number=Max("group_number")).get("max_number") or 1000
    return int(last_number) + 1


def _create_order_group_with_retry(*, buyer_producer, source_type, max_retries=3):
    for _ in range(max_retries):
        try:
            return OrderGroup.objects.create(
                group_number=_next_group_number(),
                buyer_producer=buyer_producer,
                source_type=source_type,
            )
        except IntegrityError:
            continue
    raise OrderServiceError("Não foi possível gerar o número do grupo de encomendas.")


def _create_order_with_retry(*, max_retries=3, **kwargs):
    for _ in range(max_retries):
        try:
            kwargs["order_number"] = _next_order_number()
            return Order.objects.create(**kwargs)
        except IntegrityError:
            continue
    raise OrderServiceError("Não foi possível gerar o número da encomenda.")


def _listing_source_kind(listing):
    has_stock_source, has_forecast_source = _validate_listing_source_xor(listing)
    if has_stock_source:
        return "stock"
    if has_forecast_source:
        return "forecast"
    raise OrderServiceError("Não foi possível determinar a origem da listing.")


def _collect_order_source_flags(order):
    items = list(getattr(order, "_prefetched_objects_cache", {}).get("items", []) or order.items.all())
    has_stock_source = False
    has_forecast_source = False
    has_unknown_source = False

    for item in items:
        listing = getattr(item, "listing", None)
        if not listing:
            has_unknown_source = True
            continue

        has_stock = bool(getattr(listing, "stock_id", None))
        has_forecast = bool(getattr(listing, "forecast_id", None))
        if has_stock == has_forecast:
            has_unknown_source = True
            continue

        if has_stock:
            has_stock_source = True
        if has_forecast:
            has_forecast_source = True

    return has_stock_source, has_forecast_source, has_unknown_source


def is_order_forecast_only(order):
    has_stock_source, has_forecast_source, has_unknown_source = _collect_order_source_flags(order)
    return bool(has_forecast_source and not has_stock_source and not has_unknown_source)


def get_order_source_label(order):
    has_stock_source, has_forecast_source, _ = _collect_order_source_flags(order)

    if has_forecast_source and not has_stock_source:
        return "Pré-venda"
    if has_stock_source and not has_forecast_source:
        return "Stock atual"
    return "Origem mista"


ORDER_STATUS_LABELS = dict(OrderStatus.choices)
INCOMING_FORECAST_ORDER_STATUSES = (
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.DELIVERING,
)


def compute_order_group_status(order_statuses):
    statuses = [str(status) for status in order_statuses if status]
    if not statuses:
        return OrderStatus.PENDING

    if all(status == OrderStatus.COMPLETED for status in statuses):
        return OrderStatus.COMPLETED

    if all(status == OrderStatus.CANCELLED for status in statuses):
        return OrderStatus.CANCELLED

    if any(status == OrderStatus.DELIVERING for status in statuses):
        return OrderStatus.DELIVERING

    if any(status == OrderStatus.IN_PROGRESS for status in statuses):
        return OrderStatus.IN_PROGRESS

    if any(status == OrderStatus.CONFIRMED for status in statuses):
        return OrderStatus.CONFIRMED

    if any(status == OrderStatus.PENDING for status in statuses):
        return OrderStatus.PENDING

    if any(status == OrderStatus.COMPLETED for status in statuses):
        return OrderStatus.COMPLETED

    if any(status == OrderStatus.CANCELLED for status in statuses):
        return OrderStatus.CANCELLED

    return OrderStatus.PENDING


def get_order_group_status_label(status):
    return ORDER_STATUS_LABELS.get(str(status), str(status))


def _producer_display_name(producer):
    if not producer:
        return "Vendedor"
    display_name = (getattr(producer, "display_name", "") or "").strip()
    if display_name:
        return display_name
    company_name = (getattr(producer, "company_name", "") or "").strip()
    if company_name:
        return company_name
    user = getattr(producer, "user", None)
    if user:
        full_name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
        if full_name:
            return full_name
        email = (getattr(user, "email", "") or "").strip()
        if email:
            return email
    return "Vendedor"


def get_buyer_incoming_forecast_projection(*, buyer_producer):
    """
    Calcula stock previsto do comprador sem persistência:
    - apenas encomendas já comprometidas (CONFIRMED/IN_PROGRESS/DELIVERING)
    - apenas itens ainda ativos (exclui COMPLETED/CANCELLED)
    - apenas itens com prova de origem forecast (listing + forecast_id)
    """
    incoming_items = (
        OrderItem.objects
        .filter(
            order__buyer_producer=buyer_producer,
            order__status__in=INCOMING_FORECAST_ORDER_STATUSES,
            listing__isnull=False,
            listing__forecast_id__isnull=False,
        )
        .exclude(item_status__in=[OrderItemStatus.COMPLETED, OrderItemStatus.CANCELLED])
        .select_related(
            "order",
            "listing",
            "listing__forecast",
            "seller_producer",
            "seller_producer__user",
            "product",
        )
        .order_by("-order__created_at", "-created_at")
    )

    total_incoming = Decimal("0.000")
    by_product = {}
    for item in incoming_items:
        product_id = str(item.product_id)
        listing = item.listing
        forecast = getattr(listing, "forecast", None) if listing else None
        order = item.order

        bucket = by_product.get(product_id)
        if not bucket:
            bucket = {
                "product_id": product_id,
                "product_name": getattr(item.product, "name", None) or "Produto",
                "product_unit": getattr(item.product, "unit", None) or "",
                "incoming_qty": Decimal("0.000"),
                "period_start_min": None,
                "period_end_max": None,
                "items": [],
            }
            by_product[product_id] = bucket

        quantity = quantize_qty(item.quantity or 0)
        bucket["incoming_qty"] = quantize_qty(bucket["incoming_qty"] + quantity)

        period_start = getattr(forecast, "period_start", None)
        period_end = getattr(forecast, "period_end", None)
        if period_start and (not bucket["period_start_min"] or period_start < bucket["period_start_min"]):
            bucket["period_start_min"] = period_start
        if period_end and (not bucket["period_end_max"] or period_end > bucket["period_end_max"]):
            bucket["period_end_max"] = period_end

        bucket["items"].append(
            {
                "order_id": order.id,
                "order_number": order.order_number,
                "order_status": order.status,
                "order_status_label": order.get_status_display(),
                "listing_id": getattr(listing, "id", None),
                "listing_unit_price": quantize_money(item.unit_price or 0),
                "seller_name": _producer_display_name(item.seller_producer),
                "quantity": quantity,
                "subtotal": quantize_money(item.subtotal or 0),
                "delivery_mode": getattr(listing, "delivery_mode", None),
                "delivery_mode_label": (
                    listing.get_delivery_mode_display()
                    if listing and hasattr(listing, "get_delivery_mode_display")
                    else "—"
                ),
                "period_start": period_start,
                "period_end": period_end,
                "committed_at": order.created_at,
            }
        )

        total_incoming = quantize_qty(total_incoming + quantity)

    products = list(by_product.values())

    for entry in products:
        entry["incoming_qty"] = quantize_qty(entry["incoming_qty"])
        fallback_now = timezone.now()
        entry["items"].sort(
            key=lambda row: (
                row.get("committed_at") is not None,
                row.get("committed_at") or fallback_now,
            ),
            reverse=True,
        )

    products.sort(key=lambda item: (-item["incoming_qty"], item["product_name"].lower()))

    return {
        "total_incoming_qty": quantize_qty(total_incoming),
        "product_count": len(products),
        "products": products,
        "by_product": by_product,
    }


def _map_delivery_method_from_listing(listing):
    if listing.delivery_mode == "PICKUP":
        return DeliveryMethod.PICKUP
    if listing.delivery_mode == "DELIVERY":
        return DeliveryMethod.DELIVERY
    if listing.delivery_mode == "BOTH":
        return DeliveryMethod.MIXED
    return None


def _create_status_history(order, status, changed_by=None, notes=None):
    return OrderStatusHistory.objects.create(
        order=order,
        status=status,
        changed_by=changed_by,
        notes=notes or None,
    )


def _validate_listing_source_xor(listing):
    has_stock_source = bool(getattr(listing, "stock_id", None))
    has_forecast_source = bool(getattr(listing, "forecast_id", None))
    if has_stock_source == has_forecast_source:
        raise OrderServiceError(
            "Anúncio com origem inválida (stock/previsão). Contacte o administrador."
        )
    return has_stock_source, has_forecast_source


def _update_stock_reserved(stock, quantity, acting_user):
    if not stock:
        return

    quantity = quantize_qty(quantity)
    stock.reserved_quantity = quantize_qty(Decimal(str(stock.reserved_quantity or 0)) + quantity)

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


def _update_forecast_reserved(forecast, quantity):
    if not forecast:
        return

    quantity = quantize_qty(quantity)
    forecast.reserved_quantity = quantize_qty(
        Decimal(str(forecast.reserved_quantity or 0)) + quantity
    )
    forecast.updated_at = timezone.now()
    forecast.save(update_fields=["reserved_quantity", "updated_at"])


def _consume_stock_reservation(stock, quantity, acting_user):
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


def _consume_forecast_reservation(forecast, quantity):
    if not forecast:
        return

    quantity = quantize_qty(quantity)
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    forecast.reserved_quantity = quantize_qty(max(reserved_quantity - quantity, Decimal("0.000")))
    forecast.updated_at = timezone.now()
    forecast.save(update_fields=["reserved_quantity", "updated_at"])


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
            _update_forecast_reserved(forecast, source_delta)
    elif source_delta < 0:
        source_release = quantize_qty(abs(source_delta))
        if has_stock_source:
            stock = Stock.objects.select_for_update().get(id=listing.stock_id)
            _release_stock_reservation(stock, source_release, acting_user)
        elif has_forecast_source:
            forecast = ProductionForecast.objects.select_for_update().get(id=listing.forecast_id)
            _release_forecast_reservation(forecast, source_release)

    return listing


def _reserve_listing_quantity(listing_id, quantity, acting_user):
    listing = (
        MarketplaceListing.objects
        .select_for_update()
        .get(id=listing_id)
    )
    has_stock_source, has_forecast_source = _validate_listing_source_xor(listing)

    if listing.status != ListingStatus.ACTIVE:
        raise OrderServiceError("O anúncio já não está ativo.")

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
        _update_forecast_reserved(forecast, quantity)

    return listing


def _release_listing_reservation(listing_id, quantity, acting_user):
    listing = (
        MarketplaceListing.objects
        .select_for_update()
        .get(id=listing_id)
    )
    has_stock_source, has_forecast_source = _validate_listing_source_xor(listing)

    quantity = quantize_qty(quantity)
    reserved_quantity = Decimal(str(listing.quantity_reserved or 0))
    available_quantity = Decimal(str(listing.quantity_available or 0))

    listing.quantity_reserved = quantize_qty(max(reserved_quantity - quantity, Decimal("0.000")))
    listing.quantity_available = quantize_qty(available_quantity + quantity)

    if listing.status in {ListingStatus.RESERVED, ListingStatus.CLOSED} and listing.quantity_available > 0:
        listing.status = ListingStatus.ACTIVE

    listing.updated_at = timezone.now()
    listing.save(update_fields=["quantity_reserved", "quantity_available", "status", "updated_at"])

    if has_stock_source:
        stock = Stock.objects.select_for_update().get(id=listing.stock_id)
        _release_stock_reservation(stock, quantity, acting_user)
    elif has_forecast_source:
        forecast = ProductionForecast.objects.select_for_update().get(id=listing.forecast_id)
        _release_forecast_reservation(forecast, quantity)

    return listing


def _consume_listing_reservation(listing_id, quantity, acting_user):
    listing = (
        MarketplaceListing.objects
        .select_for_update()
        .get(id=listing_id)
    )
    has_stock_source, has_forecast_source = _validate_listing_source_xor(listing)

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

    if has_stock_source:
        stock = Stock.objects.select_for_update().get(id=listing.stock_id)
        _consume_stock_reservation(stock, quantity, acting_user)
    elif has_forecast_source:
        forecast = ProductionForecast.objects.select_for_update().get(id=listing.forecast_id)
        _consume_forecast_reservation(forecast, quantity)

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
        "surplus_threshold": quantize_qty(Decimal("0")),
        "last_updated_at": now,
    }

    if hasattr(Stock, "updated_by"):
        defaults["updated_by"] = acting_user

    stock, _ = (
        Stock.objects
        .select_for_update()
        .get_or_create(
            producer=buyer_producer,
            product=product,
            defaults=defaults,
        )
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
    if getattr(stock, "surplus_threshold", None) is None:
        stock.surplus_threshold = quantize_qty(Decimal("0"))
        changed_fields.append("surplus_threshold")
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

    StockMovement.objects.create(
        stock=stock,
        movement_type=StockMovementType.ORDER_IN,
        quantity_delta=qty,
        reference_type="ORDER",
        reference_id=order.id,
        notes=f"Entrada por receção da encomenda #{order.order_number}.",
        performed_by=acting_user,
    )


def _release_forecast_reservation(forecast, quantity):
    if not forecast:
        return

    quantity = quantize_qty(quantity)
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    forecast.reserved_quantity = quantize_qty(max(reserved_quantity - quantity, Decimal("0.000")))
    forecast.updated_at = timezone.now()
    forecast.save(update_fields=["reserved_quantity", "updated_at"])


def _set_order_status(order, status):
    update_fields = ["status", "updated_at"]

    order.status = status
    order.updated_at = timezone.now()

    if status == OrderStatus.CONFIRMED and not order.confirmed_at:
        order.confirmed_at = timezone.now()
        update_fields.append("confirmed_at")

    if status == OrderStatus.COMPLETED and not order.completed_at:
        order.completed_at = timezone.now()
        update_fields.append("completed_at")

    if status == OrderStatus.CANCELLED and not order.cancelled_at:
        order.cancelled_at = timezone.now()
        update_fields.append("cancelled_at")

    order.save(update_fields=update_fields)


def compute_order_status_from_db(order_id, *, preferred_status=None, current_status=None):
    item_statuses = list(
        OrderItem.objects.filter(order_id=order_id).values_list("item_status", flat=True)
    )
    active_statuses = [
        item_status for item_status in item_statuses
        if item_status != OrderItemStatus.CANCELLED
    ]

    if not active_statuses:
        return OrderStatus.CANCELLED

    if all(item_status == OrderItemStatus.COMPLETED for item_status in active_statuses):
        return OrderStatus.COMPLETED

    has_in_delivery = any(item_status == OrderItemStatus.IN_DELIVERY for item_status in active_statuses)
    if has_in_delivery:
        return OrderStatus.DELIVERING

    has_confirmed = any(item_status == OrderItemStatus.CONFIRMED for item_status in active_statuses)

    should_keep_in_progress = (
        (preferred_status == OrderStatus.IN_PROGRESS or current_status == OrderStatus.IN_PROGRESS)
        and has_confirmed
        and not has_in_delivery
    )
    if should_keep_in_progress:
        return OrderStatus.IN_PROGRESS

    if has_confirmed:
        return OrderStatus.CONFIRMED

    return OrderStatus.PENDING


def _recalculate_order_status(order, preferred_status=None):
    resolved_status = compute_order_status_from_db(
        order_id=order.id,
        preferred_status=preferred_status,
        current_status=order.status,
    )
    _set_order_status(order, resolved_status)
    return order


@transaction.atomic
def create_order_from_listing(*, buyer_producer, listing, quantity, acting_user, buyer_notes=None, need=None):
    if listing.producer_id == buyer_producer.id:
        raise OrderServiceError("Não pode criar uma encomenda a partir do seu próprio anúncio.")
    _validate_listing_source_xor(listing)

    quantity = quantize_qty(quantity)

    if quantity <= 0:
        raise OrderServiceError("A quantidade tem de ser superior a zero.")

    available_quantity = quantize_qty(Decimal(str(listing.quantity_available or 0)))
    if quantity > available_quantity:
        raise OrderServiceError(
            f"A quantidade pedida excede a disponível ({available_quantity} {listing.product.unit})."
        )

    unit_price = Decimal(str(listing.unit_price))
    subtotal = quantize_money(quantity * unit_price)

    order_group = _create_order_group_with_retry(
        buyer_producer=buyer_producer,
        source_type=OrderSourceType.MARKETPLACE,
    )

    order = _create_order_with_retry(
        group=order_group,
        buyer_producer=buyer_producer,
        source_type=OrderSourceType.MARKETPLACE,
        status=OrderStatus.PENDING,
        total_amount=subtotal,
        delivery_method=_map_delivery_method_from_listing(listing),
        payment_status=PaymentStatus.PENDING,
        buyer_notes=buyer_notes or None,
    )

    OrderItem.objects.create(
        order=order,
        listing=listing,
        need=need,
        product=listing.product,
        seller_producer=listing.producer,
        quantity=quantity,
        unit_price=unit_price,
        subtotal=subtotal,
        item_status=OrderItemStatus.PENDING,
    )
    _reconcile_listing_reservation(listing.id, acting_user)

    _create_status_history(
        order=order,
        status=OrderStatus.PENDING,
        changed_by=acting_user,
        notes="Pedido criado a partir de um anúncio do marketplace.",
    )

    recalculate_needs_for_order(order, acting_user=acting_user)
    _sync_alerts_for_producers(buyer_producer, listing.producer, acting_user=acting_user)

    return order_group, order


@transaction.atomic
def create_order_from_recommendation(*, buyer_producer, recommendation, acting_user):
    selected_items = list(
        recommendation.items.filter(is_selected=True).select_related(
            "listing",
            "product",
            "seller_producer",
        )
    )

    if not selected_items:
        raise OrderServiceError("A recomendação não tem itens selecionados.")

    grouped_items = defaultdict(list)
    for rec_item in selected_items:
        listing = rec_item.listing
        if not listing:
            raise OrderServiceError("A recomendação contém um item sem anúncio associado.")

        source_kind = _listing_source_kind(listing)
        group_key = (str(rec_item.seller_producer_id), source_kind)
        grouped_items[group_key].append(rec_item)

    order_group = _create_order_group_with_retry(
        buyer_producer=buyer_producer,
        source_type=OrderSourceType.RECOMMENDATION,
    )

    created_orders = []

    for bucket_items in grouped_items.values():
        touched_listing_ids = set()
        order = _create_order_with_retry(
            group=order_group,
            buyer_producer=buyer_producer,
            source_type=OrderSourceType.RECOMMENDATION,
            recommendation=recommendation,
            status=OrderStatus.PENDING,
            total_amount=Decimal("0.00"),
            payment_status=PaymentStatus.PENDING,
            buyer_notes="Pedido criado a partir de uma recomendação.",
        )

        total_amount = Decimal("0.00")
        delivery_method = None

        for rec_item in bucket_items:
            listing = rec_item.listing
            quantity = quantize_qty(rec_item.suggested_quantity)
            unit_price = Decimal(str(rec_item.unit_price))
            subtotal = quantize_money(quantity * unit_price)
            total_amount += subtotal

            OrderItem.objects.create(
                order=order,
                listing=listing,
                need_id=recommendation.need_id,
                product=rec_item.product,
                seller_producer=rec_item.seller_producer,
                quantity=quantity,
                unit_price=unit_price,
                subtotal=subtotal,
                item_status=OrderItemStatus.PENDING,
            )
            if listing and getattr(listing, "id", None):
                touched_listing_ids.add(listing.id)

            mapped_method = _map_delivery_method_from_listing(listing)
            if delivery_method is None:
                delivery_method = mapped_method
            elif delivery_method != mapped_method:
                delivery_method = DeliveryMethod.MIXED

        order.total_amount = quantize_money(total_amount)
        order.delivery_method = delivery_method
        order.updated_at = timezone.now()
        order.save(update_fields=["total_amount", "delivery_method", "updated_at"])

        for listing_id in touched_listing_ids:
            _reconcile_listing_reservation(listing_id, acting_user, strict=False)

        _create_status_history(
            order=order,
            status=OrderStatus.PENDING,
            changed_by=acting_user,
            notes="Pedido criado a partir de uma recomendação aceite.",
        )
        recalculate_needs_for_order(order, acting_user=acting_user)
        created_orders.append(order)

    recommendation.status = RecommendationStatus.ACCEPTED
    recommendation.accepted_at = timezone.now()
    recommendation.updated_at = timezone.now()
    recommendation.save(update_fields=["status", "accepted_at", "updated_at"])
    sellers = [rec_item.seller_producer for rec_item in selected_items]
    _sync_alerts_for_producers(buyer_producer, *sellers, acting_user=acting_user)

    return order_group, created_orders


@transaction.atomic
def confirm_order_receipt(*, order, acting_user):
    order = Order.objects.select_for_update().get(id=order.id)

    if order.status in {OrderStatus.COMPLETED, OrderStatus.CANCELLED}:
        raise OrderServiceError("Esta encomenda já não pode ser concluída.")

    if order.status != OrderStatus.DELIVERING:
        raise OrderServiceError("Só pode confirmar receção quando a encomenda estiver em entrega.")

    active_items = list(
        OrderItem.objects
        .select_related("listing", "product")
        .filter(order_id=order.id)
        .exclude(item_status=OrderItemStatus.CANCELLED)
    )

    if not active_items:
        raise OrderServiceError("Esta encomenda não tem items ativos para concluir.")

    if not all(item.item_status in {OrderItemStatus.IN_DELIVERY, OrderItemStatus.COMPLETED} for item in active_items):
        raise OrderServiceError("Só pode confirmar receção quando a encomenda estiver efetivamente em entrega.")

    buyer_producer = order.buyer_producer

    for item in active_items:
        if item.item_status == OrderItemStatus.COMPLETED:
            continue

        item.item_status = OrderItemStatus.COMPLETED
        item.updated_at = timezone.now()
        item.save(update_fields=["item_status", "updated_at"])

        if item.listing_id:
            _consume_listing_reservation(item.listing_id, item.quantity, acting_user)

        _register_buyer_order_inbound(
            buyer_producer=buyer_producer,
            order=order,
            product=item.product,
            quantity=item.quantity,
            acting_user=acting_user,
        )

    _set_order_status(order, OrderStatus.COMPLETED)

    _create_status_history(
        order=order,
        status=OrderStatus.COMPLETED,
        changed_by=acting_user,
        notes="Receção confirmada pelo comprador.",
    )

    recalculate_needs_for_order(order, acting_user=acting_user)
    seller_producers = [item.seller_producer for item in active_items]
    _sync_alerts_for_producers(buyer_producer, *seller_producers, acting_user=acting_user)

    return order


def _sum_order_items_count(orders):
    total = 0
    for order in orders:
        prefetched_items = getattr(order, "_prefetched_objects_cache", {}).get("items", None)
        if prefetched_items is not None:
            total += len(prefetched_items)
        else:
            total += order.items.count()
    return total


def _sum_total_amount(orders):
    total = Decimal("0.00")
    for order in orders:
        total += Decimal(str(order.total_amount or 0))
    return quantize_money(total)


def _build_group_purchase_entry(group):
    group_orders = list(group.orders.all())
    statuses = [order.status for order in group_orders]
    aggregated_status = compute_order_group_status(statuses)
    return {
        "kind": "group",
        "group": group,
        "orders": group_orders,
        "status": aggregated_status,
        "status_label": get_order_group_status_label(aggregated_status),
        "total_amount": _sum_total_amount(group_orders),
        "item_count": _sum_order_items_count(group_orders),
        "order_count": len(group_orders),
        "created_at": group.created_at,
    }


def _build_legacy_order_purchase_entry(order):
    prefetched_items = getattr(order, "_prefetched_objects_cache", {}).get("items", None)
    item_count = len(prefetched_items) if prefetched_items is not None else order.items.count()
    return {
        "kind": "legacy_order",
        "group": None,
        "orders": [order],
        "order": order,
        "status": order.status,
        "status_label": order.get_status_display(),
        "total_amount": order.total_amount,
        "item_count": item_count,
        "order_count": 1,
        "created_at": order.created_at,
    }


def _format_forecast_period_from_order(order):
    items = list(getattr(order, "_prefetched_objects_cache", {}).get("items", []) or order.items.all())
    period_start_min = None
    period_end_max = None

    for item in items:
        listing = getattr(item, "listing", None)
        forecast = getattr(listing, "forecast", None) if listing else None
        if not forecast:
            continue

        period_start = getattr(forecast, "period_start", None)
        period_end = getattr(forecast, "period_end", None)
        if period_start and (not period_start_min or period_start < period_start_min):
            period_start_min = period_start
        if period_end and (not period_end_max or period_end > period_end_max):
            period_end_max = period_end

    if period_start_min and timezone.is_aware(period_start_min):
        period_start_min = timezone.localtime(period_start_min)
    if period_end_max and timezone.is_aware(period_end_max):
        period_end_max = timezone.localtime(period_end_max)

    if period_start_min and period_end_max:
        return f"{period_start_min.strftime('%d/%m/%Y')} - {period_end_max.strftime('%d/%m/%Y')}"
    if period_start_min:
        return f"A partir de {period_start_min.strftime('%d/%m/%Y')}"
    return "Sem período definido"


def _build_presale_order_entry(*, order, viewer_role):
    prefetched_items = list(getattr(order, "_prefetched_objects_cache", {}).get("items", []) or order.items.all())
    first_item = prefetched_items[0] if prefetched_items else None
    item_count = len(prefetched_items)

    if item_count == 1 and first_item:
        product_label = getattr(getattr(first_item, "product", None), "name", "") or "Produto"
        quantity_label = (
            f"{quantize_qty(first_item.quantity or 0)} "
            f"{getattr(getattr(first_item, 'product', None), 'unit', '')}"
        ).strip()
    else:
        product_label = f"Múltiplos produtos ({item_count})" if item_count > 1 else "Produto"
        quantity_label = "Vários itens"

    if viewer_role == "buyer":
        counterpart = first_item.seller_producer if first_item else None
    else:
        counterpart = order.buyer_producer

    return {
        "order": order,
        "viewer_role": viewer_role,
        "status": order.status,
        "status_label": order.get_status_display(),
        "total_amount": order.total_amount,
        "created_at": order.created_at,
        "product_label": product_label,
        "quantity_label": quantity_label,
        "counterpart_label": _producer_display_name(counterpart),
        "forecast_period_text": _format_forecast_period_from_order(order),
        "is_presale": True,
    }


def get_presale_order_entries_for_producer(*, producer, status=""):
    common_prefetch = [
        "items__product",
        "items__seller_producer__user",
        "items__listing",
        "items__listing__forecast",
    ]

    buyer_qs = (
        Order.objects
        .select_related("buyer_producer__user")
        .prefetch_related(*common_prefetch)
        .filter(buyer_producer=producer)
        .order_by("-created_at")
    )
    seller_qs = (
        Order.objects
        .select_related("buyer_producer__user")
        .prefetch_related(*common_prefetch)
        .filter(items__seller_producer=producer)
        .distinct()
        .order_by("-created_at")
    )

    if status:
        buyer_qs = buyer_qs.filter(status=status)
        seller_qs = seller_qs.filter(status=status)

    buyer_entries = []
    for order in buyer_qs:
        if not is_order_forecast_only(order):
            continue
        buyer_entries.append(_build_presale_order_entry(order=order, viewer_role="buyer"))

    seller_entries = []
    for order in seller_qs:
        if not is_order_forecast_only(order):
            continue
        seller_entries.append(_build_presale_order_entry(order=order, viewer_role="seller"))

    return {
        "buyer_entries": buyer_entries,
        "seller_entries": seller_entries,
    }


def get_buyer_purchase_entries(*, buyer_producer, status=""):
    group_orders_queryset = (
        Order.objects
        .select_related("recommendation", "buyer_producer__user")
        .prefetch_related("items__product", "items__seller_producer__user", "items__listing")
        .order_by("-created_at")
    )
    groups = (
        OrderGroup.objects
        .filter(buyer_producer=buyer_producer)
        .prefetch_related(Prefetch("orders", queryset=group_orders_queryset))
        .order_by("-created_at")
    )

    entries = []

    for group in groups:
        entry = _build_group_purchase_entry(group)
        if entry["order_count"] <= 0:
            continue
        if status and entry["status"] != status:
            continue
        entries.append(entry)

    legacy_orders_queryset = (
        Order.objects
        .select_related("recommendation", "buyer_producer__user")
        .prefetch_related("items__product", "items__seller_producer__user", "items__listing")
        .filter(buyer_producer=buyer_producer, group_id__isnull=True)
        .order_by("-created_at")
    )
    if status:
        legacy_orders_queryset = legacy_orders_queryset.filter(status=status)

    for order in legacy_orders_queryset:
        entries.append(_build_legacy_order_purchase_entry(order))

    entries.sort(key=lambda item: item["created_at"], reverse=True)
    return entries


def get_orders_for_seller(*, seller_producer, status=""):
    qs = (
        Order.objects
        .select_related("recommendation", "buyer_producer__user")
        .prefetch_related("items__product", "items__seller_producer__user")
        .filter(items__seller_producer=seller_producer)
        .distinct()
        .order_by("-created_at")
    )

    if status:
        qs = qs.filter(status=status)

    return qs


def get_order_group_detail_for_buyer(*, buyer_producer, group_id):
    group_queryset = (
        OrderGroup.objects
        .select_related("buyer_producer__user")
        .prefetch_related(
            Prefetch(
                "orders",
                queryset=(
                    Order.objects
                    .select_related("recommendation", "buyer_producer__user")
                    .prefetch_related(
                        "items__product",
                        "items__seller_producer__user",
                        "items__listing",
                        "status_history__changed_by",
                    )
                    .order_by("-created_at")
                ),
            )
        )
        .filter(id=group_id, buyer_producer=buyer_producer)
    )
    return get_object_or_404(group_queryset)


def get_order_detail_for_buyer(*, buyer_producer, order_id):
    return get_object_or_404(
        Order.objects
        .select_related("recommendation", "buyer_producer__user")
        .prefetch_related(
            "items__product",
            "items__seller_producer__user",
            "items__listing",
            "status_history__changed_by",
        ),
        id=order_id,
        buyer_producer=buyer_producer,
    )


def get_order_detail_for_seller(*, seller_producer, order_id):
    queryset = (
        Order.objects
        .select_related("recommendation", "buyer_producer__user")
        .prefetch_related(
            "items__product",
            "items__seller_producer__user",
            "items__listing",
            "status_history__changed_by",
        )
        .filter(
            id=order_id,
            items__seller_producer=seller_producer,
        )
        .distinct()
    )
    return get_object_or_404(queryset)


@transaction.atomic
def seller_update_order_status(*, order, seller_producer, new_status, acting_user, notes=None):
    order = Order.objects.select_for_update().get(id=order.id)

    if new_status not in {
        OrderStatus.CONFIRMED,
        OrderStatus.IN_PROGRESS,
        OrderStatus.DELIVERING,
        OrderStatus.CANCELLED,
    }:
        raise OrderServiceError("Estado inválido para o vendedor.")

    seller_items = list(
        OrderItem.objects
        .select_related("listing")
        .filter(order_id=order.id, seller_producer=seller_producer)
    )
    active_seller_items = [item for item in seller_items if item.item_status != OrderItemStatus.CANCELLED]

    if not active_seller_items:
        raise OrderServiceError("Não existem items ativos desta encomenda para este vendedor.")

    if order.status in {OrderStatus.COMPLETED, OrderStatus.CANCELLED}:
        raise OrderServiceError("Esta encomenda já não pode ser alterada.")

    if new_status == OrderStatus.CONFIRMED:
        reservable_items = [item for item in active_seller_items if item.item_status == OrderItemStatus.PENDING]
        if not reservable_items:
            raise OrderServiceError("Este pedido já foi previamente aceite por este vendedor.")

        touched_listing_ids = set()
        for item in reservable_items:
            item.item_status = OrderItemStatus.CONFIRMED
            item.updated_at = timezone.now()
            item.save(update_fields=["item_status", "updated_at"])
            if item.listing_id:
                touched_listing_ids.add(item.listing_id)

        for listing_id in touched_listing_ids:
            _reconcile_listing_reservation(listing_id, acting_user, strict=False)

        _recalculate_order_status(order, preferred_status=OrderStatus.CONFIRMED)

        _create_status_history(
            order=order,
            status=OrderStatus.CONFIRMED,
            changed_by=acting_user,
            notes=notes or "Pedido aceite pelo vendedor.",
        )
        recalculate_needs_for_order(order, acting_user=acting_user)
        _sync_alerts_for_producers(order.buyer_producer, seller_producer, acting_user=acting_user)
        return order

    if new_status == OrderStatus.IN_PROGRESS:
        seller_has_started = OrderStatusHistory.objects.filter(
            order_id=order.id,
            status=OrderStatus.IN_PROGRESS,
            changed_by=acting_user,
        ).exists()
        if seller_has_started:
            return order

        if (
            order.status not in {OrderStatus.CONFIRMED, OrderStatus.IN_PROGRESS}
            or any(item.item_status == OrderItemStatus.PENDING for item in active_seller_items)
        ):
            raise OrderServiceError("Tem de aceitar o pedido antes de avançar o estado da encomenda.")
        if not any(item.item_status == OrderItemStatus.CONFIRMED for item in active_seller_items):
            raise OrderServiceError("Não existem items confirmados para marcar em preparação.")

        _recalculate_order_status(order, preferred_status=OrderStatus.IN_PROGRESS)

        _create_status_history(
            order=order,
            status=OrderStatus.IN_PROGRESS,
            changed_by=acting_user,
            notes=notes or "Pedido marcado em preparação.",
        )
        recalculate_needs_for_order(order, acting_user=acting_user)
        _sync_alerts_for_producers(order.buyer_producer, seller_producer, acting_user=acting_user)
        return order

    if new_status == OrderStatus.DELIVERING:
        seller_has_started = OrderStatusHistory.objects.filter(
            order_id=order.id,
            status=OrderStatus.IN_PROGRESS,
            changed_by=acting_user,
        ).exists()
        if not seller_has_started and order.status not in {OrderStatus.IN_PROGRESS, OrderStatus.DELIVERING}:
            raise OrderServiceError("Só pode marcar em entrega depois de preparação.")

        if any(item.item_status == OrderItemStatus.PENDING for item in active_seller_items):
            raise OrderServiceError("Tem de aceitar o pedido antes de avançar o estado da encomenda.")

        deliverable_items = [
            item for item in active_seller_items
            if item.item_status == OrderItemStatus.CONFIRMED
        ]
        has_items_in_delivery = any(
            item.item_status == OrderItemStatus.IN_DELIVERY for item in active_seller_items
        )
        if not deliverable_items and not has_items_in_delivery:
            raise OrderServiceError("Não existem items elegíveis para marcar em entrega.")
        if not deliverable_items and has_items_in_delivery:
            return order

        for item in active_seller_items:
            if item.item_status == OrderItemStatus.CONFIRMED:
                item.item_status = OrderItemStatus.IN_DELIVERY
                item.updated_at = timezone.now()
                item.save(update_fields=["item_status", "updated_at"])

        _recalculate_order_status(order, preferred_status=OrderStatus.DELIVERING)

        _create_status_history(
            order=order,
            status=OrderStatus.DELIVERING,
            changed_by=acting_user,
            notes=notes or "Pedido marcado em entrega.",
        )
        recalculate_needs_for_order(order, acting_user=acting_user)
        _sync_alerts_for_producers(order.buyer_producer, seller_producer, acting_user=acting_user)
        return order

    if new_status == OrderStatus.CANCELLED:
        cancelable_items = [item for item in active_seller_items if item.item_status != OrderItemStatus.COMPLETED]
        if not cancelable_items:
            raise OrderServiceError("Os items deste vendedor já foram concluídos e não podem ser cancelados.")

        touched_listing_ids = set()
        for item in cancelable_items:
            item.item_status = OrderItemStatus.CANCELLED
            item.updated_at = timezone.now()
            item.save(update_fields=["item_status", "updated_at"])
            if item.listing_id:
                touched_listing_ids.add(item.listing_id)

        for listing_id in touched_listing_ids:
            _reconcile_listing_reservation(listing_id, acting_user)

        _recalculate_order_status(order)

        _create_status_history(
            order=order,
            status=OrderStatus.CANCELLED,
            changed_by=acting_user,
            notes=notes or "Pedido cancelado pelo vendedor.",
        )
        recalculate_needs_for_order(order, acting_user=acting_user)
        _sync_alerts_for_producers(order.buyer_producer, seller_producer, acting_user=acting_user)
        return order

    return order
