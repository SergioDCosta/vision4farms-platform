"""Order domain services: lifecycle."""

from apps.common.audit import log_audit_event
from apps.marketplace.models import MarketplaceListing
from apps.needs.services import recalculate_needs_for_order, sync_external_customer_demand_state_for_product, sync_need_response_status_for_listing
from apps.orders.models import DeliveryMethod, Order, OrderGroup, OrderItem, OrderItemStatus, OrderSourceType, OrderStatus, OrderStatusHistory, PaymentStatus
from apps.recommendations.models import RecommendationStatus
from collections import defaultdict
from decimal import Decimal
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from apps.orders.exceptions import OrderServiceError
from apps.orders.notifications import _notify_order_completed_to_seller, _notify_order_purchase_created, _notify_order_status_changed_to_buyer, _sync_alerts_for_producers
from apps.orders.reservations import _consume_listing_reservation, _listing_source_kind, _lock_listing_for_order, _map_delivery_method_from_listing, _reconcile_listing_reservation, _register_buyer_order_inbound, _validate_listing_can_be_ordered, _validate_listing_source_xor
from apps.orders.statuses import _create_status_history, _log_order_status_change, _recalculate_order_status, _set_order_status
from apps.orders.utils import _audit_qty, _order_audit_values, quantize_money, quantize_qty


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


def _sync_need_response_statuses_for_listing_ids(listing_ids):
    listing_ids = [listing_id for listing_id in listing_ids if listing_id]
    if not listing_ids:
        return

    listings = MarketplaceListing.objects.filter(
        id__in=listing_ids,
        need_id__isnull=False,
    )
    for listing in listings:
        sync_need_response_status_for_listing(listing)


def _sync_external_demands_for_product_change(producer, product, acting_user):
    try:
        sync_external_customer_demand_state_for_product(
            producer=producer,
            product=product,
            acting_user=acting_user,
        )
    except Exception:
        return


@transaction.atomic
def create_order_from_listing(*, buyer_producer, listing, quantity, acting_user, buyer_notes=None, need=None):
    listing = _lock_listing_for_order(listing)
    quantity = quantize_qty(quantity)
    _validate_listing_can_be_ordered(
        listing=listing,
        buyer_producer=buyer_producer,
        quantity=quantity,
    )
    if listing.need_id and OrderItem.objects.filter(listing_id=listing.id, need_id=listing.need_id).exists():
        raise OrderServiceError("Esta oferta já originou uma encomenda e não pode ser comprada novamente.")
    _validate_listing_source_xor(listing)

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
    if listing.need_id:
        _sync_need_response_statuses_for_listing_ids([listing.id])

    _create_status_history(
        order=order,
        status=OrderStatus.PENDING,
        changed_by=acting_user,
        notes=(
            "Pedido criado ao aceitar uma oferta privada para uma necessidade."
            if need or listing.need_id
            else "Pedido criado a partir de um anúncio do marketplace."
        ),
    )
    log_audit_event(
        actor=acting_user,
        action="ORDER_CREATED",
        entity_type="orders",
        entity_id=order.id,
        notes=(
            "Encomenda criada ao aceitar uma proposta privada para uma procura."
            if need or listing.need_id
            else "Encomenda criada a partir de anúncio do marketplace."
        ),
        new_values=_order_audit_values(order) | {
            "listing_id": str(listing.id),
            "need_id": str(listing.need_id) if listing.need_id else None,
            "seller_producer_id": str(listing.producer_id),
            "product_id": str(listing.product_id),
            "product_name": getattr(listing.product, "name", None),
            "quantity": _audit_qty(quantity),
        },
    )
    _notify_order_purchase_created(
        order=order,
        buyer_producer=buyer_producer,
        seller_producer=listing.producer,
        acting_user=acting_user,
    )

    recalculate_needs_for_order(order, acting_user=acting_user)
    _sync_alerts_for_producers(buyer_producer, listing.producer, acting_user=acting_user)

    return order_group, order


@transaction.atomic
def create_order_from_recommendation(*, buyer_producer, recommendation, acting_user):
    recommendation = (
        recommendation.__class__.objects
        .select_for_update(of=("self",))
        .select_related("product", "producer", "need")
        .get(id=recommendation.id)
    )

    if recommendation.producer_id != buyer_producer.id:
        raise OrderServiceError("Esta recomendação não pertence ao produtor atual.")

    if recommendation.status == RecommendationStatus.ACCEPTED:
        raise OrderServiceError("Esta recomendação já foi aceite.")

    if recommendation.status in {RecommendationStatus.IGNORED, RecommendationStatus.EXPIRED}:
        raise OrderServiceError("Esta recomendação já não pode ser aceite.")

    selected_items = list(
        recommendation.items.filter(is_selected=True).select_related(
            "listing",
            "product",
            "seller_producer",
        )
    )

    if not selected_items:
        raise OrderServiceError("A recomendação não tem itens selecionados.")

    required_by_listing = defaultdict(lambda: Decimal("0.000"))
    for rec_item in selected_items:
        if not rec_item.listing_id:
            raise OrderServiceError("A recomendação contém um item sem anúncio associado.")
        required_by_listing[rec_item.listing_id] = quantize_qty(
            required_by_listing[rec_item.listing_id] + quantize_qty(rec_item.suggested_quantity)
        )

    locked_listings = {
        listing.id: listing
        for listing in (
            MarketplaceListing.objects
            .select_for_update(of=("self",))
            .select_related("product", "producer", "stock", "forecast", "need", "need__producer")
            .filter(id__in=required_by_listing.keys())
        )
    }

    if len(locked_listings) != len(required_by_listing):
        raise OrderServiceError("A recomendação contém um anúncio indisponível.")

    for listing_id, required_quantity in required_by_listing.items():
        _validate_listing_can_be_ordered(
            listing=locked_listings[listing_id],
            buyer_producer=buyer_producer,
            quantity=required_quantity,
        )

    grouped_items = defaultdict(list)
    for rec_item in selected_items:
        listing = locked_listings.get(rec_item.listing_id)
        if not listing:
            raise OrderServiceError("A recomendação contém um item sem anúncio associado.")
        if rec_item.seller_producer_id != listing.producer_id or rec_item.product_id != listing.product_id:
            raise OrderServiceError("A recomendação contém dados desatualizados do anúncio.")

        rec_item.listing = listing
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
            _reconcile_listing_reservation(listing_id, acting_user, strict=True)

        _create_status_history(
            order=order,
            status=OrderStatus.PENDING,
            changed_by=acting_user,
            notes="Pedido criado a partir de uma recomendação aceite.",
        )
        log_audit_event(
            actor=acting_user,
            action="ORDER_CREATED",
            entity_type="orders",
            entity_id=order.id,
            notes="Encomenda criada a partir de uma recomendação aceite.",
            new_values=_order_audit_values(order) | {
                "recommendation_id": str(recommendation.id),
                "need_id": str(recommendation.need_id) if recommendation.need_id else None,
                "seller_producer_id": str(bucket_items[0].seller_producer_id) if bucket_items else None,
                "product_ids": sorted({str(item.product_id) for item in bucket_items}),
                "listing_ids": sorted({str(item.listing_id) for item in bucket_items if item.listing_id}),
            },
        )
        seller_for_order = bucket_items[0].seller_producer if bucket_items else None
        if seller_for_order:
            _notify_order_purchase_created(
                order=order,
                buyer_producer=buyer_producer,
                seller_producer=seller_for_order,
                acting_user=acting_user,
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
    previous_status = order.status

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
            _consume_listing_reservation(item.listing_id, item.quantity, acting_user, order=order)

        _register_buyer_order_inbound(
            buyer_producer=buyer_producer,
            order=order,
            product=item.product,
            quantity=item.quantity,
            acting_user=acting_user,
        )
        _sync_external_demands_for_product_change(
            buyer_producer,
            item.product,
            acting_user,
        )

    _set_order_status(order, OrderStatus.COMPLETED)

    _create_status_history(
        order=order,
        status=OrderStatus.COMPLETED,
        changed_by=acting_user,
        notes="Receção confirmada pelo comprador.",
    )
    _log_order_status_change(
        order,
        previous_status=previous_status,
        acting_user=acting_user,
        notes="Receção confirmada pelo comprador.",
    )
    log_audit_event(
        actor=acting_user,
        action="ORDER_RECEIPT_CONFIRMED",
        entity_type="orders",
        entity_id=order.id,
        notes="Receção da encomenda confirmada pelo comprador.",
        old_values={"status": previous_status},
        new_values=_order_audit_values(order),
    )

    seller_producers = []
    seen_seller_ids = set()
    for item in active_items:
        seller = item.seller_producer
        seller_id = getattr(seller, "id", None)
        if not seller or seller_id in seen_seller_ids:
            continue
        seen_seller_ids.add(seller_id)
        seller_producers.append(seller)
        _notify_order_completed_to_seller(
            order=order,
            buyer_producer=buyer_producer,
            seller_producer=seller,
            acting_user=acting_user,
        )

    _sync_need_response_statuses_for_listing_ids([item.listing_id for item in active_items])
    recalculate_needs_for_order(order, acting_user=acting_user)
    _sync_alerts_for_producers(buyer_producer, *seller_producers, acting_user=acting_user)

    return order


@transaction.atomic
def buyer_cancel_order(*, order, buyer_producer, acting_user, notes=None):
    order = Order.objects.select_for_update().get(id=order.id)
    previous_status = order.status

    if order.buyer_producer_id != buyer_producer.id:
        raise OrderServiceError("Esta encomenda não pertence ao comprador atual.")

    if order.status in {OrderStatus.DELIVERING, OrderStatus.COMPLETED}:
        raise OrderServiceError("A encomenda já está em entrega ou concluída e não pode ser cancelada pelo comprador.")
    if order.status == OrderStatus.CANCELLED:
        raise OrderServiceError("Esta encomenda já foi cancelada.")
    if order.status not in {OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.IN_PROGRESS}:
        raise OrderServiceError("A encomenda já não pode ser cancelada pelo comprador.")

    active_items = list(
        OrderItem.objects
        .select_related("listing", "seller_producer")
        .filter(order_id=order.id)
        .exclude(item_status=OrderItemStatus.CANCELLED)
    )
    if not active_items:
        raise OrderServiceError("Não existem itens ativos para cancelar nesta encomenda.")
    if any(item.item_status in {OrderItemStatus.IN_DELIVERY, OrderItemStatus.COMPLETED} for item in active_items):
        raise OrderServiceError("Existem itens já em entrega ou concluídos; o comprador já não pode cancelar esta encomenda.")

    touched_listing_ids = set()
    sellers = {}
    for item in active_items:
        item.item_status = OrderItemStatus.CANCELLED
        item.updated_at = timezone.now()
        item.save(update_fields=["item_status", "updated_at"])
        if item.listing_id:
            touched_listing_ids.add(item.listing_id)
        seller = getattr(item, "seller_producer", None)
        if seller:
            sellers[getattr(seller, "id", id(seller))] = seller

    for listing_id in touched_listing_ids:
        _reconcile_listing_reservation(listing_id, acting_user)
    _sync_need_response_statuses_for_listing_ids(touched_listing_ids)

    _recalculate_order_status(order)
    cancellation_notes = notes or "Encomenda cancelada pelo comprador."
    _create_status_history(
        order=order,
        status=OrderStatus.CANCELLED,
        changed_by=acting_user,
        notes=cancellation_notes,
    )
    recalculate_needs_for_order(order, acting_user=acting_user)
    _sync_alerts_for_producers(order.buyer_producer, *sellers.values(), acting_user=acting_user)
    _log_order_status_change(
        order,
        previous_status=previous_status,
        acting_user=acting_user,
        notes=cancellation_notes,
        cancelled=True,
    )
    return order


@transaction.atomic
def seller_update_order_status(*, order, seller_producer, new_status, acting_user, notes=None):
    order = Order.objects.select_for_update().get(id=order.id)
    previous_status = order.status

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
        _sync_need_response_statuses_for_listing_ids(touched_listing_ids)

        _recalculate_order_status(order, preferred_status=OrderStatus.CONFIRMED)

        _create_status_history(
            order=order,
            status=OrderStatus.CONFIRMED,
            changed_by=acting_user,
            notes=notes or "Pedido aceite pelo vendedor.",
        )
        _notify_order_status_changed_to_buyer(
            order=order,
            buyer_producer=order.buyer_producer,
            seller_producer=seller_producer,
            status=OrderStatus.CONFIRMED,
            acting_user=acting_user,
        )
        recalculate_needs_for_order(order, acting_user=acting_user)
        _sync_alerts_for_producers(order.buyer_producer, seller_producer, acting_user=acting_user)
        _log_order_status_change(
            order,
            previous_status=previous_status,
            acting_user=acting_user,
            notes=notes or "Pedido aceite pelo vendedor.",
        )
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
        _notify_order_status_changed_to_buyer(
            order=order,
            buyer_producer=order.buyer_producer,
            seller_producer=seller_producer,
            status=OrderStatus.IN_PROGRESS,
            acting_user=acting_user,
        )
        recalculate_needs_for_order(order, acting_user=acting_user)
        _sync_alerts_for_producers(order.buyer_producer, seller_producer, acting_user=acting_user)
        _log_order_status_change(
            order,
            previous_status=previous_status,
            acting_user=acting_user,
            notes=notes or "Pedido marcado em preparação.",
        )
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
        _notify_order_status_changed_to_buyer(
            order=order,
            buyer_producer=order.buyer_producer,
            seller_producer=seller_producer,
            status=OrderStatus.DELIVERING,
            acting_user=acting_user,
        )
        recalculate_needs_for_order(order, acting_user=acting_user)
        _sync_alerts_for_producers(order.buyer_producer, seller_producer, acting_user=acting_user)
        _log_order_status_change(
            order,
            previous_status=previous_status,
            acting_user=acting_user,
            notes=notes or "Pedido marcado em entrega.",
        )
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
        _sync_need_response_statuses_for_listing_ids(touched_listing_ids)

        _recalculate_order_status(order)

        _create_status_history(
            order=order,
            status=OrderStatus.CANCELLED,
            changed_by=acting_user,
            notes=notes or "Pedido cancelado pelo vendedor.",
        )
        _notify_order_status_changed_to_buyer(
            order=order,
            buyer_producer=order.buyer_producer,
            seller_producer=seller_producer,
            status=OrderStatus.CANCELLED,
            acting_user=acting_user,
        )
        recalculate_needs_for_order(order, acting_user=acting_user)
        _sync_alerts_for_producers(order.buyer_producer, seller_producer, acting_user=acting_user)
        _log_order_status_change(
            order,
            previous_status=previous_status,
            acting_user=acting_user,
            notes=notes or "Pedido cancelado pelo vendedor.",
            cancelled=True,
        )
        return order

    return order
