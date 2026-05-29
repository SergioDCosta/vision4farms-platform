from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from urllib.parse import urlencode
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Product
from apps.common.audit import log_audit_event
from apps.inventory.models import ProductionForecast, Stock, StockMovement, StockMovementType
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.needs.models import (
    ExternalCustomerDemand,
    ExternalCustomerDemandSourceSystem,
    ExternalCustomerDemandStatus,
    Need,
    NeedResponseStatus,
    NeedSourceSystem,
    NeedStatus,
)
from apps.orders.models import OrderItem, OrderItemStatus, OrderStatus


ACTIVE_NEED_STATUSES = [NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED]
EDITABLE_NEED_STATUSES = [NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED, NeedStatus.COVERED]
PLANNED_NEED_ORDER_STATUSES = [
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.DELIVERING,
]
PUBLIC_OFFERED_ORDER_STATUSES = [
    OrderStatus.PENDING,
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.DELIVERING,
]
NEEDS_SEARCH_QUERY_MAX_LENGTH = 120
NEED_NOTES_MAX_LENGTH = 1200
NEED_RESPONSE_NOTES_MAX_LENGTH = 1200
EXTERNAL_DEMAND_SEARCH_QUERY_MAX_LENGTH = 120
EXTERNAL_DEMAND_NOTES_MAX_LENGTH = 1200
EXTERNAL_DEMAND_ACTIVE_STATUSES = [
    ExternalCustomerDemandStatus.OPEN,
    ExternalCustomerDemandStatus.PARTIALLY_COVERED,
    ExternalCustomerDemandStatus.COVERED,
]
EXTERNAL_DEMAND_EDITABLE_STATUSES = [
    ExternalCustomerDemandStatus.OPEN,
    ExternalCustomerDemandStatus.PARTIALLY_COVERED,
    ExternalCustomerDemandStatus.COVERED,
]
CUSTOMER_DEMAND_NEED_NOTES = (
    "Necessidade gerada automaticamente a partir de pedidos externos de clientes. "
    "A quantidade reflete o maior défice temporal entre pedidos acumulados e "
    "stock/previsão disponível até às datas de entrega."
)


def _audit_quantity(value):
    return str(_quantize_need_quantity(value))


def _external_demand_audit_values(demand):
    return {
        "producer_id": str(demand.producer_id),
        "product_id": str(demand.product_id),
        "product_name": getattr(getattr(demand, "product", None), "name", None),
        "requested_quantity": _audit_quantity(demand.requested_quantity),
        "requested_delivery_date": str(demand.requested_delivery_date),
        "status": demand.status,
        "source_system": demand.source_system,
        "client_name": demand.client_name,
        "generated_need_id": str(demand.generated_need_id) if demand.generated_need_id else None,
    }


def _need_audit_values(need, *, plan=None):
    values = {
        "producer_id": str(need.producer_id),
        "product_id": str(need.product_id),
        "product_name": getattr(getattr(need, "product", None), "name", None),
        "required_quantity": _audit_quantity(need.required_quantity),
        "needed_by_date": str(need.needed_by_date) if need.needed_by_date else None,
        "status": need.status,
        "source_system": need.source_system,
        "is_marketplace_published": bool(getattr(need, "is_marketplace_published", False)),
        "published_at": (
            str(need.published_at) if getattr(need, "published_at", None) else None
        ),
    }
    if plan is not None:
        values["max_deficit"] = _audit_quantity(plan.get("max_deficit"))
        values["first_deficit_date"] = (
            str(plan.get("first_deficit_date")) if plan.get("first_deficit_date") else None
        )
    return values


def _need_marketplace_audit_values(need):
    return {
        "need_id": str(need.id),
        "product_id": str(need.product_id),
        "source_system": need.source_system,
        "required_quantity": _audit_quantity(need.required_quantity),
        "needed_by_date": str(need.needed_by_date) if need.needed_by_date else None,
        "is_marketplace_published": bool(getattr(need, "is_marketplace_published", False)),
        "published_at": (
            str(need.published_at) if getattr(need, "published_at", None) else None
        ),
    }


class DuplicateActiveNeedError(ValidationError):
    def __init__(self, existing_need):
        self.existing_need = existing_need
        super().__init__("Já existe uma necessidade ativa para este produto. Edite essa necessidade em vez de criar outra.")


@dataclass(frozen=True)
class NeedResponse:
    listing: MarketplaceListing
    id: object
    need_id: object
    producer_label: str
    need_owner_label: str
    product_name: str
    product_unit: str
    offered_quantity: Decimal
    available_quantity: Decimal
    ordered_quantity: Decimal
    quantity_available: Decimal
    unit_price: Decimal
    source_key: str
    source_label: str
    status: str
    status_label: str
    response_status: str
    response_status_label: str
    response_badge_class: str
    response_message: str
    can_buy: bool
    can_reject: bool
    notes: str
    detail_url: str
    reject_url: str
    edit_url: str
    is_editable: bool
    cta_label: str = "Ver oferta e comprar"


@dataclass(frozen=True)
class NeedResponseSummary:
    listing_id: object
    status: str
    status_label: str
    badge_class: str
    message: str
    detail_url: str
    is_active: bool
    edit_url: str = ""
    can_edit: bool = False
    can_send_new_proposal: bool = False


def _quantize_need_quantity(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))


def normalize_needs_search_query(value):
    return (value or "").strip()[:NEEDS_SEARCH_QUERY_MAX_LENGTH]


def normalize_external_demands_search_query(value):
    return (value or "").strip()[:EXTERNAL_DEMAND_SEARCH_QUERY_MAX_LENGTH]


def _clean_optional_text(value):
    value = (value or "").strip()
    return value or None


def _is_uuid_like(value):
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def get_external_customer_demand_for_producer(*, producer, demand_id):
    if not producer or not demand_id or not _is_uuid_like(demand_id):
        return None

    return (
        ExternalCustomerDemand.objects
        .select_related("producer", "product", "product__category", "generated_need")
        .filter(id=demand_id, producer=producer)
        .first()
    )


def list_external_customer_demands(
    *,
    producer,
    q="",
    status="",
    product_id="",
    category_id="",
    active_only=False,
):
    if not producer:
        return ExternalCustomerDemand.objects.none()

    qs = (
        ExternalCustomerDemand.objects
        .select_related("product", "product__category", "generated_need", "created_by", "updated_by")
        .filter(producer=producer)
        .order_by("requested_delivery_date", "-created_at")
    )

    q = normalize_external_demands_search_query(q)
    if q:
        qs = qs.filter(
            Q(client_name__icontains=q)
            | Q(client_contact__icontains=q)
            | Q(client_reference__icontains=q)
            | Q(product__name__icontains=q)
            | Q(notes__icontains=q)
        )

    valid_statuses = {choice.value for choice in ExternalCustomerDemandStatus}
    if status in valid_statuses:
        qs = qs.filter(status=status)

    if product_id and _is_uuid_like(product_id):
        qs = qs.filter(product_id=product_id)
    elif product_id:
        qs = qs.none()

    if category_id and _is_uuid_like(category_id):
        qs = qs.filter(product__category_id=category_id)
    elif category_id:
        qs = qs.none()

    if active_only:
        qs = qs.filter(status__in=EXTERNAL_DEMAND_ACTIVE_STATUSES)

    return qs


def _as_local_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _forecast_available_date(forecast):
    period_end_date = _as_local_date(getattr(forecast, "period_end", None))
    if period_end_date:
        return period_end_date
    return _as_local_date(getattr(forecast, "period_start", None))


def _forecast_active_listings_quantity(forecast, *, exclude_listing_id=None):
    if not forecast:
        return Decimal("0.000")
    from django.db.models import F
    qs = MarketplaceListing.objects.filter(
        forecast=forecast,
        status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
        need_id__isnull=True,
    )
    if exclude_listing_id:
        qs = qs.exclude(id=exclude_listing_id)
    result = qs.aggregate(total=Sum(F("quantity_available") + F("quantity_reserved")))
    return _quantize_need_quantity(result["total"] or Decimal("0.000"))


def _forecast_available_quantity(forecast, *, exclude_listing_id=None):
    forecast_quantity = _quantize_need_quantity(getattr(forecast, "forecast_quantity", 0))
    reserved_quantity = _quantize_need_quantity(getattr(forecast, "reserved_quantity", 0))
    marketplace_committed = _forecast_active_listings_quantity(
        forecast,
        exclude_listing_id=exclude_listing_id,
    )
    return _quantize_need_quantity(
        max(forecast_quantity - reserved_quantity - marketplace_committed, Decimal("0.000"))
    )


def _stock_active_listings_quantity(stock, *, exclude_listing_id=None):
    """Soma quantity_available + quantity_reserved dos listings ACTIVE/RESERVED deste stock.
    Exclui listings ligados a necessidades (esses já contabilizam outra procura)."""
    if not stock:
        return Decimal("0.000")
    from django.db.models import F
    qs = MarketplaceListing.objects.filter(
        stock=stock,
        status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
        need_id__isnull=True,
    )
    if exclude_listing_id:
        qs = qs.exclude(id=exclude_listing_id)
    result = qs.aggregate(total=Sum(F("quantity_available") + F("quantity_reserved")))
    return _quantize_need_quantity(result["total"] or Decimal("0.000"))


def _stock_pending_need_responses_quantity(stock, *, exclude_listing_id=None):
    """Quantidade oferecida em propostas privadas pendentes sem encomenda associada."""
    if not stock:
        return Decimal("0.000")
    qs = MarketplaceListing.objects.filter(
        stock=stock,
        status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
        need_id__isnull=False,
        need_response_status=NeedResponseStatus.PENDING,
        order_items__isnull=True,
    )
    if exclude_listing_id:
        qs = qs.exclude(id=exclude_listing_id)
    result = qs.aggregate(total=Sum("quantity_available"))
    return _quantize_need_quantity(result["total"] or Decimal("0.000"))


def _stock_available_quantity(stock, *, exclude_listing_id=None):
    if not stock:
        return Decimal("0.000")
    current_quantity = _quantize_need_quantity(getattr(stock, "current_quantity", 0))
    reserved_quantity = _quantize_need_quantity(getattr(stock, "reserved_quantity", 0))
    # Subtrai anúncios públicos e propostas privadas pendentes: ambos comprometem
    # stock que não pode voltar a ser prometido nem contar para pedidos externos.
    marketplace_committed = _stock_active_listings_quantity(stock, exclude_listing_id=exclude_listing_id)
    pending_need_responses = _stock_pending_need_responses_quantity(
        stock,
        exclude_listing_id=exclude_listing_id,
    )
    net = current_quantity - reserved_quantity - marketplace_committed - pending_need_responses
    return _quantize_need_quantity(max(net, Decimal("0.000")))


def calculate_external_demand_plan(*, producer, product, exclude_listing_id=None):
    active_demands = list(
        ExternalCustomerDemand.objects
        .select_related("product")
        .filter(
            producer=producer,
            product=product,
            status__in=EXTERNAL_DEMAND_ACTIVE_STATUSES,
        )
        .order_by("requested_delivery_date", "created_at")
    )
    stock = Stock.objects.filter(producer=producer, product=product).first()
    available_stock_now = _stock_available_quantity(stock, exclude_listing_id=exclude_listing_id)

    forecasts = []
    for forecast in (
        ProductionForecast.objects
        .filter(producer=producer, product=product)
        .only("id", "forecast_quantity", "reserved_quantity", "period_start", "period_end")
    ):
        available_date = _forecast_available_date(forecast)
        available_quantity = _forecast_available_quantity(
            forecast,
            exclude_listing_id=exclude_listing_id,
        )
        if available_date and available_quantity > Decimal("0.000"):
            forecasts.append({
                "available_date": available_date,
                "available_quantity": available_quantity,
            })

    demand_by_date = {}
    for demand in active_demands:
        delivery_date = demand.requested_delivery_date
        demand_by_date[delivery_date] = _quantize_need_quantity(
            demand_by_date.get(delivery_date, Decimal("0.000"))
            + _quantize_need_quantity(demand.requested_quantity)
        )

    rows = []
    total_external_demand = Decimal("0.000")
    max_deficit = Decimal("0.000")
    first_deficit_date = None
    total_forecast_relevant = Decimal("0.000")

    for delivery_date in sorted(demand_by_date):
        total_external_demand = _quantize_need_quantity(
            total_external_demand + demand_by_date[delivery_date]
        )
        forecast_until_date = Decimal("0.000")
        for forecast in forecasts:
            if forecast["available_date"] <= delivery_date:
                forecast_until_date = _quantize_need_quantity(
                    forecast_until_date + forecast["available_quantity"]
                )
        total_forecast_relevant = max(total_forecast_relevant, forecast_until_date)
        capacity_until_date = _quantize_need_quantity(available_stock_now + forecast_until_date)
        remaining_capacity_until_date = _quantize_need_quantity(
            max(capacity_until_date - total_external_demand, Decimal("0.000"))
        )
        deficit_until_date = _quantize_need_quantity(
            max(total_external_demand - capacity_until_date, Decimal("0.000"))
        )
        if deficit_until_date > max_deficit:
            max_deficit = deficit_until_date
        if deficit_until_date > Decimal("0.000") and first_deficit_date is None:
            first_deficit_date = delivery_date

        rows.append({
            "delivery_date": delivery_date,
            "demand_until_date": total_external_demand,
            "forecast_until_date": forecast_until_date,
            "capacity_until_date": capacity_until_date,
            "remaining_capacity_until_date": remaining_capacity_until_date,
            "deficit_until_date": deficit_until_date,
        })

    generated_need = _get_customer_demand_need_for_product(producer=producer, product=product)

    return {
        "product": product,
        "total_external_demand": total_external_demand,
        "available_stock_now": available_stock_now,
        "total_forecast_relevant": total_forecast_relevant,
        "max_deficit": max_deficit,
        "first_deficit_date": first_deficit_date,
        "rows": rows,
        "has_deficit": max_deficit > Decimal("0.000"),
        "generated_need": generated_need,
        "generated_need_status": getattr(generated_need, "status", None),
    }


def _customer_demand_need_external_id(*, producer, product):
    return f"customer_demands:{producer.id}:{product.id}"


def _get_customer_demand_need_for_product(*, producer, product):
    if not producer or not product:
        return None
    return (
        Need.objects
        .select_related("producer", "product", "product__category")
        .filter(
            producer=producer,
            product=product,
            source_system=NeedSourceSystem.CUSTOMER_DEMAND,
        )
        .exclude(status__in=[NeedStatus.IGNORED, NeedStatus.CANCELLED])
        .order_by("-updated_at", "-created_at")
        .first()
    )


def _lock_need_for_customer_demand_sync(*, producer, product):
    return (
        Need.objects
        .select_for_update()
        .filter(
            producer=producer,
            product=product,
            source_system=NeedSourceSystem.CUSTOMER_DEMAND,
        )
        .exclude(status__in=[NeedStatus.IGNORED, NeedStatus.CANCELLED])
        .order_by("-updated_at", "-created_at")
        .first()
    )


def _set_external_demands_generated_need(*, producer, product, need):
    active_qs = ExternalCustomerDemand.objects.filter(
        producer=producer,
        product=product,
        status__in=EXTERNAL_DEMAND_ACTIVE_STATUSES,
    )
    inactive_qs = ExternalCustomerDemand.objects.filter(
        producer=producer,
        product=product,
    ).exclude(status__in=EXTERNAL_DEMAND_ACTIVE_STATUSES)

    active_qs.update(generated_need=need, updated_at=timezone.now())
    inactive_qs.filter(generated_need=need).update(generated_need=None, updated_at=timezone.now())


@transaction.atomic
def sync_safety_stock_from_external_demands(*, producer, product, acting_user=None):
    if not producer or not product:
        return None

    total = (
        ExternalCustomerDemand.objects
        .filter(
            producer=producer,
            product=product,
            status__in=EXTERNAL_DEMAND_ACTIVE_STATUSES,
        )
        .aggregate(total=Sum("requested_quantity"))
        .get("total")
        or Decimal("0.000")
    )
    total = _quantize_need_quantity(total)

    now = timezone.now()
    defaults = {
        "current_quantity": Decimal("0.000"),
        "reserved_quantity": Decimal("0.000"),
        "safety_stock": total,
        "last_updated_at": now,
    }
    if hasattr(Stock, "updated_by"):
        defaults["updated_by"] = acting_user

    stock, created = (
        Stock.objects
        .select_for_update()
        .get_or_create(
            producer=producer,
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
            notes="Stock criado automaticamente para refletir compromissos externos.",
            new_values={
                "producer_id": str(producer.id),
                "product_id": str(product.id),
                "product_name": product.name,
                "current_quantity": _audit_quantity(stock.current_quantity),
                "reserved_quantity": _audit_quantity(stock.reserved_quantity),
                "safety_stock": _audit_quantity(stock.safety_stock),
            },
        )
        return stock

    if _quantize_need_quantity(stock.safety_stock) == total:
        return stock

    previous_total = _audit_quantity(stock.safety_stock)
    stock.safety_stock = total
    update_fields = ["safety_stock"]
    if hasattr(stock, "updated_by"):
        stock.updated_by = acting_user
        update_fields.append("updated_by")
    if hasattr(stock, "last_updated_at"):
        stock.last_updated_at = now
        update_fields.append("last_updated_at")
    if hasattr(stock, "updated_at"):
        stock.updated_at = now
        update_fields.append("updated_at")
    stock.save(update_fields=list(dict.fromkeys(update_fields)))
    log_audit_event(
        actor=acting_user,
        action="STOCK_UPDATED",
        entity_type="stocks",
        entity_id=stock.id,
        notes="Compromissos externos sincronizados a partir dos pedidos de clientes.",
        old_values={"safety_stock": previous_total},
        new_values={
            "producer_id": str(producer.id),
            "product_id": str(product.id),
            "product_name": product.name,
            "safety_stock": _audit_quantity(stock.safety_stock),
        },
    )
    return stock


@transaction.atomic
def sync_need_from_external_demands(*, producer, product, acting_user=None):
    if not producer or not product:
        return None, calculate_external_demand_plan(producer=producer, product=product), False

    plan = calculate_external_demand_plan(producer=producer, product=product)
    max_deficit = _quantize_need_quantity(plan.get("max_deficit"))
    first_deficit_date = plan.get("first_deficit_date")
    existing_need = _lock_need_for_customer_demand_sync(producer=producer, product=product)
    changed = False
    previous_need_values = _need_audit_values(existing_need, plan=plan) if existing_need else None

    if max_deficit > Decimal("0.000"):
        needed_by_date = _normalize_needed_by_date(first_deficit_date)
        external_id = _customer_demand_need_external_id(producer=producer, product=product)

        if existing_need:
            update_fields = []
            publication_values_before = None
            public_terms_changed = (
                bool(getattr(existing_need, "is_marketplace_published", False))
                and (
                    _quantize_need_quantity(existing_need.required_quantity) != max_deficit
                    or existing_need.needed_by_date != needed_by_date
                )
            )
            if public_terms_changed:
                publication_values_before = _need_marketplace_audit_values(existing_need)
                existing_need.is_marketplace_published = False
                update_fields.append("is_marketplace_published")
            if _quantize_need_quantity(existing_need.required_quantity) != max_deficit:
                existing_need.required_quantity = max_deficit
                update_fields.append("required_quantity")
            if existing_need.needed_by_date != needed_by_date:
                existing_need.needed_by_date = needed_by_date
                update_fields.append("needed_by_date")
            if existing_need.source_system != NeedSourceSystem.CUSTOMER_DEMAND:
                existing_need.source_system = NeedSourceSystem.CUSTOMER_DEMAND
                update_fields.append("source_system")
            if existing_need.external_id != external_id:
                existing_need.external_id = external_id
                update_fields.append("external_id")
            if (existing_need.notes or "") != CUSTOMER_DEMAND_NEED_NOTES:
                existing_need.notes = CUSTOMER_DEMAND_NEED_NOTES
                update_fields.append("notes")
            if existing_need.status == NeedStatus.COVERED:
                existing_need.status = NeedStatus.OPEN
                update_fields.append("status")

            if update_fields:
                if hasattr(existing_need, "updated_at"):
                    existing_need.updated_at = timezone.now()
                    update_fields.append("updated_at")
                existing_need.save(update_fields=list(dict.fromkeys(update_fields)))
                changed = True
            if publication_values_before:
                log_audit_event(
                    actor=acting_user,
                    action="NEED_MARKETPLACE_UNPUBLISHED_AFTER_RECALCULATION",
                    entity_type="needs",
                    entity_id=existing_need.id,
                    notes="Procura retirada do marketplace porque o défice ou a data crítica foram recalculados.",
                    old_values=publication_values_before,
                    new_values=_need_marketplace_audit_values(existing_need),
                )
            need = existing_need
        else:
            need = Need.objects.create(
                producer=producer,
                product=product,
                required_quantity=max_deficit,
                needed_by_date=needed_by_date,
                source_system=NeedSourceSystem.CUSTOMER_DEMAND,
                external_id=external_id,
                notes=CUSTOMER_DEMAND_NEED_NOTES,
                status=NeedStatus.OPEN,
                is_marketplace_published=False,
                published_at=None,
            )
            changed = True
            log_audit_event(
                actor=acting_user,
                action="CUSTOMER_DEMAND_NEED_CREATED",
                entity_type="needs",
                entity_id=need.id,
                notes="Procura agregada criada automaticamente por défice em pedidos externos.",
                new_values=_need_audit_values(need, plan=plan),
            )

        need, _, status_changed = recalculate_need_status(need, acting_user=acting_user)
        if existing_need and changed:
            log_audit_event(
                actor=acting_user,
                action="CUSTOMER_DEMAND_NEED_UPDATED",
                entity_type="needs",
                entity_id=need.id,
                notes="Procura agregada recalculada a partir dos pedidos externos.",
                old_values=previous_need_values,
                new_values=_need_audit_values(need, plan=plan),
            )
        _set_external_demands_generated_need(producer=producer, product=product, need=need)
        return need, plan, bool(changed or status_changed)

    if existing_need:
        update_fields = []
        publication_values_before = None
        if existing_need.source_system != NeedSourceSystem.CUSTOMER_DEMAND:
            existing_need.source_system = NeedSourceSystem.CUSTOMER_DEMAND
            update_fields.append("source_system")
        external_id = _customer_demand_need_external_id(producer=producer, product=product)
        if existing_need.external_id != external_id:
            existing_need.external_id = external_id
            update_fields.append("external_id")
        if existing_need.status != NeedStatus.COVERED:
            existing_need.status = NeedStatus.COVERED
            update_fields.append("status")
        if getattr(existing_need, "is_marketplace_published", False):
            publication_values_before = _need_marketplace_audit_values(existing_need)
            existing_need.is_marketplace_published = False
            update_fields.append("is_marketplace_published")
        if (existing_need.notes or "") != CUSTOMER_DEMAND_NEED_NOTES:
            existing_need.notes = CUSTOMER_DEMAND_NEED_NOTES
            update_fields.append("notes")
        if update_fields:
            if hasattr(existing_need, "updated_at"):
                existing_need.updated_at = timezone.now()
                update_fields.append("updated_at")
            existing_need.save(update_fields=list(dict.fromkeys(update_fields)))
            changed = True
        if changed:
            action = (
                "CUSTOMER_DEMAND_NEED_COVERED"
                if previous_need_values and previous_need_values["status"] != NeedStatus.COVERED
                else "CUSTOMER_DEMAND_NEED_UPDATED"
            )
            log_audit_event(
                actor=acting_user,
                action=action,
                entity_type="needs",
                entity_id=existing_need.id,
                notes="Procura automática coberta após recalcular stock e previsão disponíveis.",
                old_values=previous_need_values,
                new_values=_need_audit_values(existing_need, plan=plan),
            )
        if publication_values_before:
            log_audit_event(
                actor=acting_user,
                action="NEED_MARKETPLACE_UNPUBLISHED_AFTER_RECALCULATION",
                entity_type="needs",
                entity_id=existing_need.id,
                notes="Procura retirada do marketplace porque os pedidos passaram a estar cobertos.",
                old_values=publication_values_before,
                new_values=_need_marketplace_audit_values(existing_need),
            )
        _set_external_demands_generated_need(producer=producer, product=product, need=existing_need)
        return existing_need, plan, changed

    _set_external_demands_generated_need(producer=producer, product=product, need=None)
    return None, plan, False


@transaction.atomic
def evaluate_external_demand_conflict_with_listings(*, producer, product):
    """
    Avalia se um pedido externo recém-criado/atualizado deixa anúncios
    ativos do mesmo produto sem cobertura temporal.

    Devolve None quando não há conflito; caso contrário um dict com:
      - max_deficit, first_deficit_date, temporal_sellable_quantity
      - published_quantity (Σ quantity_available + quantity_reserved dos
        listings ACTIVE/RESERVED para este produto e produtor)
      - affected_listings_count
    """
    from apps.inventory.services import calculate_inventory_commitment_state
    from apps.marketplace.models import MarketplaceListing, ListingStatus
    from django.db.models import F, Sum

    commitment = calculate_inventory_commitment_state(producer, product)
    max_deficit = commitment.get("max_deficit") or Decimal("0.000")
    temporal_sellable = commitment.get("temporal_sellable_quantity") or Decimal("0.000")

    if max_deficit <= Decimal("0.000") and temporal_sellable >= Decimal("0.000"):
        return None

    listings_qs = MarketplaceListing.objects.filter(
        producer=producer,
        product=product,
        status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
        need_id__isnull=True,
    )
    aggregated = listings_qs.aggregate(
        total=Sum(F("quantity_available") + F("quantity_reserved")),
        count=Count("id"),
    )
    published_quantity = aggregated.get("total") or Decimal("0.000")
    affected_count = aggregated.get("count") or 0

    if published_quantity <= Decimal("0.000") or affected_count == 0:
        return None

    return {
        "max_deficit": max_deficit,
        "first_deficit_date": commitment.get("first_deficit_date"),
        "temporal_sellable_quantity": temporal_sellable,
        "published_quantity": published_quantity,
        "affected_listings_count": affected_count,
    }


def sync_external_customer_demand_state_for_product(*, producer, product, acting_user=None):
    stock = sync_safety_stock_from_external_demands(
        producer=producer,
        product=product,
        acting_user=acting_user,
    )
    need, plan, changed = sync_need_from_external_demands(
        producer=producer,
        product=product,
        acting_user=acting_user,
    )
    return {
        "stock": stock,
        "need": need,
        "plan": plan,
        "changed": changed,
    }


def build_external_demand_plans(*, producer, product_id=""):
    if not producer:
        return []

    product_ids_qs = (
        ExternalCustomerDemand.objects
        .filter(producer=producer, status__in=EXTERNAL_DEMAND_ACTIVE_STATUSES)
        .values_list("product_id", flat=True)
        .distinct()
    )
    if product_id and _is_uuid_like(product_id):
        product_ids_qs = product_ids_qs.filter(product_id=product_id)
    elif product_id:
        return []

    products = (
        Product.objects
        .filter(id__in=list(product_ids_qs), is_active=True)
        .select_related("category")
        .order_by("name")
    )
    plans = [calculate_external_demand_plan(producer=producer, product=product) for product in products]
    return sorted(
        plans,
        key=lambda plan: (
            plan["first_deficit_date"] is None,
            plan["first_deficit_date"] or date.max,
            -plan["max_deficit"],
            plan["product"].name.lower(),
        ),
    )


def get_external_customer_demand_summary(*, producer, demand_plans=None):
    if not producer:
        return {
            "open_count": 0,
            "product_count": 0,
            "total_quantity": Decimal("0.000"),
            "max_deficit": Decimal("0.000"),
            "first_deficit_date": None,
        }

    rows = list(
        ExternalCustomerDemand.objects
        .filter(producer=producer, status__in=EXTERNAL_DEMAND_ACTIVE_STATUSES)
        .values_list("product_id", "requested_quantity")
    )
    total_quantity = Decimal("0.000")
    product_ids = set()
    for product_id, quantity in rows:
        product_ids.add(product_id)
        total_quantity = _quantize_need_quantity(total_quantity + _quantize_need_quantity(quantity))

    demand_plans = demand_plans if demand_plans is not None else build_external_demand_plans(producer=producer)
    max_deficit = Decimal("0.000")
    first_deficit_date = None
    for plan in demand_plans:
        if plan["max_deficit"] > max_deficit:
            max_deficit = plan["max_deficit"]
        current_date = plan.get("first_deficit_date")
        if current_date and (first_deficit_date is None or current_date < first_deficit_date):
            first_deficit_date = current_date

    return {
        "open_count": len(rows),
        "product_count": len(product_ids),
        "total_quantity": total_quantity,
        "max_deficit": _quantize_need_quantity(max_deficit),
        "first_deficit_date": first_deficit_date,
    }


def _validate_external_customer_demand_payload(*, product, client_name, requested_quantity, requested_delivery_date):
    if not product:
        raise ValidationError("Produto inválido para o pedido externo.")
    if not (client_name or "").strip():
        raise ValidationError("Indique o nome do cliente.")

    quantity = _quantize_need_quantity(requested_quantity)
    if quantity <= Decimal("0.000"):
        raise ValidationError("A quantidade pedida deve ser superior a zero.")

    if not requested_delivery_date:
        raise ValidationError("Indique a data pretendida de entrega.")

    return quantity


@transaction.atomic
def create_external_customer_demand(
    *,
    producer,
    product,
    client_name,
    requested_quantity,
    requested_delivery_date,
    client_contact=None,
    client_reference=None,
    notes=None,
    created_by=None,
    source_system=ExternalCustomerDemandSourceSystem.MANUAL,
    external_id=None,
):
    quantity = _validate_external_customer_demand_payload(
        product=product,
        client_name=client_name,
        requested_quantity=requested_quantity,
        requested_delivery_date=requested_delivery_date,
    )

    demand = ExternalCustomerDemand.objects.create(
        producer=producer,
        product=product,
        client_name=(client_name or "").strip(),
        client_contact=_clean_optional_text(client_contact),
        client_reference=_clean_optional_text(client_reference),
        requested_quantity=quantity,
        requested_delivery_date=requested_delivery_date,
        status=ExternalCustomerDemandStatus.OPEN,
        notes=_clean_optional_text(notes),
        source_system=source_system or ExternalCustomerDemandSourceSystem.MANUAL,
        external_id=_clean_optional_text(external_id),
        created_by=created_by,
        updated_by=created_by,
    )
    log_audit_event(
        actor=created_by,
        action="EXTERNAL_DEMAND_CREATED",
        entity_type="external_customer_demands",
        entity_id=demand.id,
        notes="Pedido externo de cliente registado.",
        new_values=_external_demand_audit_values(demand),
    )
    sync_external_customer_demand_state_for_product(
        producer=producer,
        product=product,
        acting_user=created_by,
    )
    return demand


@transaction.atomic
def update_external_customer_demand(
    *,
    demand,
    producer,
    product,
    client_name,
    requested_quantity,
    requested_delivery_date,
    client_contact=None,
    client_reference=None,
    notes=None,
    updated_by=None,
):
    if not demand:
        raise ValidationError("Pedido externo inválido.")

    locked_demand = (
        ExternalCustomerDemand.objects
        .select_for_update()
        .select_related("product", "producer")
        .get(id=demand.id)
    )
    if locked_demand.producer_id != producer.id:
        raise ValidationError("Não pode editar este pedido externo.")
    if locked_demand.status not in EXTERNAL_DEMAND_EDITABLE_STATUSES:
        raise ValidationError("Este pedido externo já não pode ser editado.")

    previous_values = _external_demand_audit_values(locked_demand)
    quantity = _validate_external_customer_demand_payload(
        product=product,
        client_name=client_name,
        requested_quantity=requested_quantity,
        requested_delivery_date=requested_delivery_date,
    )

    previous_product = locked_demand.product
    locked_demand.product = product
    locked_demand.client_name = (client_name or "").strip()
    locked_demand.client_contact = _clean_optional_text(client_contact)
    locked_demand.client_reference = _clean_optional_text(client_reference)
    locked_demand.requested_quantity = quantity
    locked_demand.requested_delivery_date = requested_delivery_date
    locked_demand.notes = _clean_optional_text(notes)
    locked_demand.updated_by = updated_by
    locked_demand.updated_at = timezone.now()
    locked_demand.save(
        update_fields=[
            "product",
            "client_name",
            "client_contact",
            "client_reference",
            "requested_quantity",
            "requested_delivery_date",
            "notes",
            "updated_by",
            "updated_at",
        ]
    )
    log_audit_event(
        actor=updated_by,
        action="EXTERNAL_DEMAND_UPDATED",
        entity_type="external_customer_demands",
        entity_id=locked_demand.id,
        notes="Pedido externo de cliente atualizado.",
        old_values=previous_values,
        new_values=_external_demand_audit_values(locked_demand),
    )
    sync_external_customer_demand_state_for_product(
        producer=producer,
        product=product,
        acting_user=updated_by,
    )
    if previous_product and previous_product.id != product.id:
        sync_external_customer_demand_state_for_product(
            producer=producer,
            product=previous_product,
            acting_user=updated_by,
        )
    return locked_demand


@transaction.atomic
def cancel_external_customer_demand(*, demand, producer, updated_by=None):
    if not demand:
        raise ValidationError("Pedido externo inválido.")

    locked_demand = (
        ExternalCustomerDemand.objects
        .select_for_update()
        .get(id=demand.id)
    )
    if locked_demand.producer_id != producer.id:
        raise ValidationError("Não pode cancelar este pedido externo.")
    if locked_demand.status == ExternalCustomerDemandStatus.CANCELLED:
        return locked_demand, False
    if locked_demand.status == ExternalCustomerDemandStatus.FULFILLED:
        raise ValidationError("Este pedido externo já está cumprido e não pode ser cancelado.")

    previous_values = _external_demand_audit_values(locked_demand)
    now = timezone.now()
    locked_demand.status = ExternalCustomerDemandStatus.CANCELLED
    locked_demand.cancelled_at = now
    locked_demand.updated_by = updated_by
    locked_demand.updated_at = now
    locked_demand.save(update_fields=["status", "cancelled_at", "updated_by", "updated_at"])
    log_audit_event(
        actor=updated_by,
        action="EXTERNAL_DEMAND_CANCELLED",
        entity_type="external_customer_demands",
        entity_id=locked_demand.id,
        notes="Pedido externo de cliente cancelado.",
        old_values=previous_values,
        new_values=_external_demand_audit_values(locked_demand),
    )
    sync_external_customer_demand_state_for_product(
        producer=producer,
        product=locked_demand.product,
        acting_user=updated_by,
    )
    return locked_demand, True


@transaction.atomic
def mark_external_customer_demand_fulfilled(*, demand, producer, updated_by=None):
    if not demand:
        raise ValidationError("Pedido externo inválido.")

    locked_demand = (
        ExternalCustomerDemand.objects
        .select_for_update()
        .select_related("product")
        .get(id=demand.id)
    )
    if locked_demand.producer_id != producer.id:
        raise ValidationError("Não pode concluir este pedido externo.")
    if locked_demand.status == ExternalCustomerDemandStatus.FULFILLED:
        return locked_demand, False
    if locked_demand.status == ExternalCustomerDemandStatus.CANCELLED:
        raise ValidationError("Um pedido cancelado não pode ser marcado como cumprido.")
    if locked_demand.status not in EXTERNAL_DEMAND_EDITABLE_STATUSES:
        raise ValidationError("Este pedido externo já não pode ser marcado como cumprido.")

    previous_values = _external_demand_audit_values(locked_demand)
    existing_movement = (
        StockMovement.objects
        .filter(
            movement_type=StockMovementType.ORDER_OUT,
            reference_type="EXTERNAL_DEMAND",
            reference_id=locked_demand.id,
        )
        .first()
    )
    movement = existing_movement
    old_stock_quantity = None
    new_stock_quantity = None

    if existing_movement is None:
        stock = (
            Stock.objects
            .select_for_update()
            .filter(producer=producer, product=locked_demand.product)
            .first()
        )
        available_quantity = _quantize_need_quantity(
            (
                Decimal(str(getattr(stock, "current_quantity", 0) or 0))
                - Decimal(str(getattr(stock, "reserved_quantity", 0) or 0))
            )
            if stock
            else Decimal("0.000")
        )
        requested_quantity = _quantize_need_quantity(locked_demand.requested_quantity)
        if available_quantity < requested_quantity:
            raise ValidationError(
                "Não existe stock atual suficiente para concluir este pedido. "
                "Assuma produção prevista em stock ou atualize o stock antes de marcar como cumprido."
            )

        old_stock_quantity = _quantize_need_quantity(stock.current_quantity)
        stock.current_quantity = _quantize_need_quantity(old_stock_quantity - requested_quantity)
        new_stock_quantity = stock.current_quantity
        stock.updated_by = updated_by
        stock.last_updated_at = timezone.now()
        update_fields = ["current_quantity", "updated_by", "last_updated_at"]
        if hasattr(stock, "updated_at"):
            stock.updated_at = timezone.now()
            update_fields.append("updated_at")
        stock.save(update_fields=update_fields)
        log_audit_event(
            actor=updated_by,
            action="STOCK_UPDATED",
            entity_type="stocks",
            entity_id=stock.id,
            notes="Saída de stock por entrega de pedido externo a cliente.",
            old_values={"current_quantity": _audit_quantity(old_stock_quantity)},
            new_values={
                "stock_id": str(stock.id),
                "product_id": str(locked_demand.product_id),
                "current_quantity": _audit_quantity(new_stock_quantity),
                "demand_id": str(locked_demand.id),
            },
        )

        movement = StockMovement.objects.create(
            stock=stock,
            movement_type=StockMovementType.ORDER_OUT,
            quantity_delta=-requested_quantity,
            reference_type="EXTERNAL_DEMAND",
            reference_id=locked_demand.id,
            notes=(
                f"Saída por entrega do pedido externo de {locked_demand.client_name}: "
                f"{requested_quantity} {locked_demand.product.unit} de {locked_demand.product.name}."
            ),
            performed_by=updated_by,
        )
        log_audit_event(
            actor=updated_by,
            action="STOCK_MOVEMENT_CREATED",
            entity_type="stock_movements",
            entity_id=movement.id,
            notes=movement.notes,
            new_values={
                "movement_id": str(movement.id),
                "demand_id": str(locked_demand.id),
                "stock_id": str(stock.id),
                "product_id": str(locked_demand.product_id),
                "quantity_delta": _audit_quantity(-requested_quantity),
                "movement_type": StockMovementType.ORDER_OUT,
            },
        )

        from apps.inventory.services import (
            get_listings_blocking_stock_decrease,
            reduce_listings_to_fit_stock,
        )

        blocking = get_listings_blocking_stock_decrease(stock, stock.current_quantity)
        if blocking["deficit"] > Decimal("0.000"):
            reduce_listings_to_fit_stock(
                stock=stock,
                new_quantity=stock.current_quantity,
                mode="proportional",
                acting_user=updated_by,
            )

    now = timezone.now()
    locked_demand.status = ExternalCustomerDemandStatus.FULFILLED
    locked_demand.fulfilled_at = now
    locked_demand.updated_by = updated_by
    locked_demand.updated_at = now
    locked_demand.save(update_fields=["status", "fulfilled_at", "updated_by", "updated_at"])
    log_audit_event(
        actor=updated_by,
        action="EXTERNAL_DEMAND_FULFILLED",
        entity_type="external_customer_demands",
        entity_id=locked_demand.id,
        notes="Pedido externo marcado manualmente como cumprido.",
        old_values=previous_values,
        new_values=_external_demand_audit_values(locked_demand) | {
            "movement_id": str(movement.id) if movement else None,
            "old_stock_quantity": (
                _audit_quantity(old_stock_quantity) if old_stock_quantity is not None else None
            ),
            "new_stock_quantity": (
                _audit_quantity(new_stock_quantity) if new_stock_quantity is not None else None
            ),
            "fulfilled_at": str(locked_demand.fulfilled_at),
        },
    )
    sync_external_customer_demand_state_for_product(
        producer=producer,
        product=locked_demand.product,
        acting_user=updated_by,
    )
    return locked_demand, True


def is_listing_effectively_expired(listing, *, now=None):
    expires_at = getattr(listing, "expires_at", None)
    if not expires_at or not isinstance(expires_at, datetime):
        return False
    now = now or timezone.now()
    return expires_at <= now


def _normalize_needed_by_date(value):
    if not value:
        return None

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max)
    else:
        raw_value = str(value).strip()
        if not raw_value:
            return None
        try:
            if "T" in raw_value:
                parsed = datetime.strptime(raw_value, "%Y-%m-%dT%H:%M")
            else:
                parsed = datetime.combine(datetime.strptime(raw_value, "%Y-%m-%d").date(), time.max)
        except ValueError:
            raise ValidationError("Data limite inválida para a necessidade.")

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def get_need_minimum_edit_quantity(coverage):
    return _quantize_need_quantity(
        max(
            coverage.get("planned_qty") or Decimal("0.000"),
            coverage.get("completed_qty") or Decimal("0.000"),
        )
    )


def get_need_edit_help_text(need, coverage):
    minimum_quantity = get_need_minimum_edit_quantity(coverage)
    product = getattr(need, "product", None)
    unit = getattr(product, "unit", "kg")
    if minimum_quantity > 0:
        return f"Esta necessidade já tem encomendas associadas. A quantidade mínima permitida é {minimum_quantity} {unit}."
    return "Pode ajustar a quantidade, a data limite e as observações. O produto mantém-se fixo."


def _producer_marketplace_display_name(producer):
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
        if getattr(user, "email", None):
            return user.email
    return "Produtor"


def calculate_need_coverage(need):
    required_quantity = _quantize_need_quantity(need.required_quantity)
    planned_qty = Decimal("0.000")
    completed_qty = Decimal("0.000")

    items = (
        OrderItem.objects
        .filter(need_id=need.id)
        .select_related("order")
    )

    for item in items:
        item_status = item.item_status
        if item_status == OrderItemStatus.CANCELLED:
            continue

        quantity = _quantize_need_quantity(item.quantity)

        if item_status == OrderItemStatus.COMPLETED:
            planned_qty += quantity
            completed_qty += quantity
            continue

        if item_status == OrderItemStatus.IN_DELIVERY:
            planned_qty += quantity
            continue

        if item_status == OrderItemStatus.CONFIRMED:
            order_status = getattr(getattr(item, "order", None), "status", None)
            if order_status in PLANNED_NEED_ORDER_STATUSES:
                planned_qty += quantity

    planned_qty = _quantize_need_quantity(planned_qty)
    completed_qty = _quantize_need_quantity(completed_qty)
    remaining_to_plan = _quantize_need_quantity(max(required_quantity - planned_qty, Decimal("0.000")))
    remaining_to_receive = _quantize_need_quantity(max(required_quantity - completed_qty, Decimal("0.000")))

    return {
        "required_quantity": required_quantity,
        "planned_qty": planned_qty,
        "completed_qty": completed_qty,
        "remaining_to_plan": remaining_to_plan,
        "remaining_to_receive": remaining_to_receive,
    }


def _resolve_need_status(need, coverage):
    if need.status == NeedStatus.IGNORED:
        return NeedStatus.IGNORED

    if getattr(need, "source_system", None) == NeedSourceSystem.CUSTOMER_DEMAND:
        plan = calculate_external_demand_plan(producer=need.producer, product=need.product)
        if _quantize_need_quantity(plan.get("max_deficit")) <= Decimal("0.000"):
            return NeedStatus.COVERED

    if coverage["completed_qty"] >= coverage["required_quantity"]:
        return NeedStatus.COVERED

    if coverage["planned_qty"] > 0:
        return NeedStatus.PARTIALLY_COVERED

    return NeedStatus.OPEN


@transaction.atomic
def update_need(
    *,
    need,
    producer,
    required_quantity,
    needed_by_date=None,
    notes=None,
):
    if not need:
        raise ValidationError("Necessidade inválida.")

    locked_need = (
        Need.objects
        .select_for_update()
        .select_related("product", "producer")
        .get(id=need.id)
    )
    if locked_need.producer_id != producer.id:
        raise ValidationError("Não pode editar esta necessidade.")
    if getattr(locked_need, "source_system", None) == NeedSourceSystem.CUSTOMER_DEMAND:
        raise ValidationError(
            "Esta procura é gerada automaticamente a partir dos pedidos de clientes. "
            "Para alterar a quantidade ou a data, edite os pedidos de origem."
        )
    if locked_need.status not in EDITABLE_NEED_STATUSES:
        raise ValidationError("Esta necessidade já não pode ser editada.")

    quantity = _quantize_need_quantity(required_quantity)
    if quantity <= Decimal("0.000"):
        raise ValidationError("A quantidade necessária deve ser superior a zero.")

    coverage = calculate_need_coverage(locked_need)
    minimum_quantity = get_need_minimum_edit_quantity(coverage)
    if quantity < minimum_quantity:
        raise ValidationError(
            f"A quantidade mínima permitida é {minimum_quantity} {locked_need.product.unit}, "
            "porque já existem encomendas associadas."
        )

    normalized_deadline = _normalize_needed_by_date(needed_by_date)
    normalized_notes = (notes or "").strip() or None

    changed = (
        locked_need.required_quantity != quantity
        or locked_need.needed_by_date != normalized_deadline
        or (locked_need.notes or None) != normalized_notes
    )

    if changed:
        locked_need.required_quantity = quantity
        locked_need.needed_by_date = normalized_deadline
        locked_need.notes = normalized_notes
        if hasattr(locked_need, "updated_at"):
            locked_need.updated_at = timezone.now()
            locked_need.save(update_fields=["required_quantity", "needed_by_date", "notes", "updated_at"])
        else:
            locked_need.save(update_fields=["required_quantity", "needed_by_date", "notes"])

    updated_need, updated_coverage, status_changed = recalculate_need_status(locked_need)
    return updated_need, updated_coverage, bool(changed or status_changed)


@transaction.atomic
def recalculate_need_status(need, *, acting_user=None):
    need = Need.objects.select_for_update().get(id=need.id)
    if need.status == NeedStatus.IGNORED:
        return need, calculate_need_coverage(need), False

    coverage = calculate_need_coverage(need)
    next_status = _resolve_need_status(need, coverage)
    status_changed = False

    publication_values_before = None
    update_fields = []
    if (
        getattr(need, "source_system", None) == NeedSourceSystem.CUSTOMER_DEMAND
        and next_status == NeedStatus.COVERED
        and getattr(need, "is_marketplace_published", False)
    ):
        publication_values_before = _need_marketplace_audit_values(need)
        need.is_marketplace_published = False
        update_fields.append("is_marketplace_published")

    if need.status != next_status:
        need.status = next_status
        update_fields.append("status")
        status_changed = True

    if update_fields:
        if hasattr(need, "updated_at"):
            need.updated_at = timezone.now()
            update_fields.append("updated_at")
        need.save(update_fields=list(dict.fromkeys(update_fields)))

    if publication_values_before:
        log_audit_event(
            actor=acting_user,
            action="NEED_MARKETPLACE_UNPUBLISHED_AFTER_RECALCULATION",
            entity_type="needs",
            entity_id=need.id,
            notes="Procura retirada do marketplace porque ficou coberta.",
            old_values=publication_values_before,
            new_values=_need_marketplace_audit_values(need),
        )

    return need, coverage, status_changed


@transaction.atomic
def recalculate_needs_for_order(order, *, acting_user=None):
    need_ids = list(
        OrderItem.objects
        .filter(order_id=order.id, need_id__isnull=False)
        .values_list("need_id", flat=True)
        .distinct()
    )
    if not need_ids:
        return []

    needs = list(
        Need.objects
        .select_for_update()
        .filter(id__in=need_ids)
    )

    results = []
    for need in needs:
        _, coverage, changed = recalculate_need_status(
            need,
            acting_user=acting_user,
        )
        results.append({
            "need": need,
            "coverage": coverage,
            "changed": changed,
        })
        log_audit_event(
            actor=acting_user,
            action="NEED_COVERAGE_CHANGED",
            entity_type="needs",
            entity_id=need.id,
            notes=f"Cobertura recalculada após alteração da encomenda #{getattr(order, 'order_number', order.id)}.",
            new_values={
                "order_id": str(order.id),
                "status": need.status,
                "required_quantity": _audit_quantity(coverage["required_quantity"]),
                "planned_quantity": _audit_quantity(coverage["planned_qty"]),
                "completed_quantity": _audit_quantity(coverage["completed_qty"]),
            },
        )

    return results


def get_need_for_producer(*, producer, need_id):
    return Need.objects.filter(id=need_id, producer=producer).select_related(
        "product",
        "product__category",
        "producer",
        "producer__user",
    ).first()


@transaction.atomic
def create_need(
    *,
    producer,
    product,
    required_quantity,
    needed_by_date=None,
    source_system=NeedSourceSystem.MANUAL,
    external_id=None,
    notes=None,
    acting_user=None,
):
    quantity = _quantize_need_quantity(required_quantity)
    if quantity <= Decimal("0.000"):
        raise ValidationError("A quantidade necessária deve ser superior a zero.")

    existing_need = (
        Need.objects
        .select_for_update()
        .filter(
            producer=producer,
            product=product,
            status__in=ACTIVE_NEED_STATUSES,
        )
        .order_by("-updated_at", "-created_at")
        .first()
    )
    if existing_need:
        raise DuplicateActiveNeedError(existing_need)

    is_marketplace_published = source_system != NeedSourceSystem.CUSTOMER_DEMAND
    need = Need.objects.create(
        producer=producer,
        product=product,
        required_quantity=quantity,
        needed_by_date=needed_by_date,
        source_system=source_system,
        external_id=external_id,
        notes=(notes or "").strip() or None,
        status=NeedStatus.OPEN,
        is_marketplace_published=is_marketplace_published,
        published_at=timezone.now() if is_marketplace_published else None,
    )

    need, coverage, _ = recalculate_need_status(need, acting_user=acting_user)
    if is_marketplace_published:
        log_audit_event(
            actor=acting_user,
            action="NEED_MARKETPLACE_PUBLISHED",
            entity_type="needs",
            entity_id=need.id,
            notes="Procura publicada no marketplace ao ser criada explicitamente.",
            new_values=_need_marketplace_audit_values(need),
        )
    return need, coverage


@transaction.atomic
def create_or_update_need(**kwargs):
    need, coverage = create_need(**kwargs)
    return need, coverage, True


@transaction.atomic
def ignore_need(*, need, producer):
    if not need or need.producer_id != producer.id:
        raise ValidationError("Necessidade inválida para este produtor.")

    if need.status == NeedStatus.IGNORED:
        return False

    need.status = NeedStatus.IGNORED
    if hasattr(need, "updated_at"):
        need.updated_at = timezone.now()
        need.save(update_fields=["status", "updated_at"])
    else:
        need.save(update_fields=["status"])
    return True


@transaction.atomic
def publish_need_to_marketplace(*, need, producer, acting_user=None):
    if not need:
        raise ValidationError("Procura inválida.")

    locked_need = (
        Need.objects
        .select_for_update()
        .select_related("product", "producer")
        .get(id=need.id)
    )
    if locked_need.producer_id != producer.id:
        raise ValidationError("Não pode publicar esta procura.")
    if locked_need.status not in ACTIVE_NEED_STATUSES:
        raise ValidationError("Apenas procuras abertas ou parcialmente cobertas podem ser publicadas.")

    coverage = calculate_need_coverage(locked_need)
    if coverage["remaining_to_plan"] <= Decimal("0.000"):
        raise ValidationError("Esta procura já não tem quantidade por cobrir.")

    if locked_need.source_system == NeedSourceSystem.CUSTOMER_DEMAND:
        plan = calculate_external_demand_plan(
            producer=locked_need.producer,
            product=locked_need.product,
        )
        if _quantize_need_quantity(plan.get("max_deficit")) <= Decimal("0.000"):
            raise ValidationError("Os pedidos de clientes já estão cobertos; não existe défice para publicar.")

    if getattr(locked_need, "is_marketplace_published", False):
        return locked_need, False

    locked_need.is_marketplace_published = True
    locked_need.published_at = timezone.now()
    update_fields = ["is_marketplace_published", "published_at"]
    if hasattr(locked_need, "updated_at"):
        locked_need.updated_at = timezone.now()
        update_fields.append("updated_at")
    locked_need.save(update_fields=update_fields)
    log_audit_event(
        actor=acting_user,
        action="NEED_MARKETPLACE_PUBLISHED",
        entity_type="needs",
        entity_id=locked_need.id,
        notes="Procura publicada no marketplace pelo produtor.",
        new_values=_need_marketplace_audit_values(locked_need),
    )
    return locked_need, True


@transaction.atomic
def withdraw_need_from_marketplace(*, need, producer, acting_user=None):
    if not need:
        raise ValidationError("Procura inválida.")

    locked_need = (
        Need.objects
        .select_for_update()
        .select_related("product", "producer")
        .get(id=need.id)
    )
    if locked_need.producer_id != producer.id:
        raise ValidationError("Não pode retirar esta procura.")
    if not getattr(locked_need, "is_marketplace_published", False):
        return locked_need, False

    previous_values = _need_marketplace_audit_values(locked_need)
    locked_need.is_marketplace_published = False
    update_fields = ["is_marketplace_published"]
    if hasattr(locked_need, "updated_at"):
        locked_need.updated_at = timezone.now()
        update_fields.append("updated_at")
    locked_need.save(update_fields=update_fields)
    log_audit_event(
        actor=acting_user,
        action="NEED_MARKETPLACE_WITHDRAWN",
        entity_type="needs",
        entity_id=locked_need.id,
        notes="Procura retirada do marketplace pelo produtor.",
        old_values=previous_values,
        new_values=_need_marketplace_audit_values(locked_need),
    )
    return locked_need, True


def _build_need_row(need):
    coverage = calculate_need_coverage(need)
    required_quantity = coverage["required_quantity"]
    completed_qty = coverage["completed_qty"]
    minimum_edit_quantity = get_need_minimum_edit_quantity(coverage)
    progress_percent = Decimal("0")
    if required_quantity > 0:
        progress_percent = (completed_qty / required_quantity) * Decimal("100")

    return {
        "need": need,
        "status": need.status,
        "status_label": need.get_status_display(),
        "producer_label": _producer_marketplace_display_name(need.producer),
        "required_quantity": required_quantity,
        "planned_qty": coverage["planned_qty"],
        "completed_qty": completed_qty,
        "remaining_to_plan": coverage["remaining_to_plan"],
        "remaining_to_receive": coverage["remaining_to_receive"],
        "planned_excess_qty": _quantize_need_quantity(
            max(coverage["planned_qty"] - required_quantity, Decimal("0.000"))
        ),
        "progress_percent": max(Decimal("0"), min(progress_percent, Decimal("100"))),
        "can_edit": need.status in EDITABLE_NEED_STATUSES,
        "minimum_edit_quantity": minimum_edit_quantity,
        "edit_help_text": get_need_edit_help_text(need, coverage),
    }


def _add_offered_quantity(offered_quantities, need_id, quantity):
    need_key = str(need_id)
    offered_quantities[need_key] = _quantize_need_quantity(
        offered_quantities.get(need_key, Decimal("0.000"))
        + _quantize_need_quantity(quantity)
    )


def get_public_offered_quantities_by_need(*, need_ids, viewer_producer=None):
    if not need_ids:
        return {}

    now = timezone.now()
    offered_quantities = {}
    pending_listings = (
        MarketplaceListing.objects
        .filter(
            need_id__in=need_ids,
            status=ListingStatus.ACTIVE,
            need_response_status=NeedResponseStatus.PENDING,
            quantity_available__gt=0,
            order_items__isnull=True,
        )
        .exclude(expires_at__lte=now)
        .only("need_id", "producer_id", "quantity_available")
    )
    if viewer_producer:
        pending_listings = pending_listings.exclude(producer=viewer_producer)

    for listing in pending_listings:
        _add_offered_quantity(
            offered_quantities,
            listing.need_id,
            listing.quantity_available,
        )

    active_order_items = (
        OrderItem.objects
        .filter(
            need_id__in=need_ids,
            order__status__in=PUBLIC_OFFERED_ORDER_STATUSES,
        )
        .exclude(item_status__in=[OrderItemStatus.CANCELLED, OrderItemStatus.COMPLETED])
        .only("need_id", "seller_producer_id", "quantity")
    )
    if viewer_producer:
        active_order_items = active_order_items.exclude(seller_producer=viewer_producer)

    for item in active_order_items:
        _add_offered_quantity(
            offered_quantities,
            item.need_id,
            item.quantity,
        )

    return offered_quantities


def list_marketplace_public_needs(*, viewer_producer=None, q="", category_id=""):
    qs = (
        Need.objects
        .select_related("producer", "producer__user", "product", "product__category")
        .filter(
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            is_marketplace_published=True,
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    if viewer_producer:
        qs = qs.exclude(producer=viewer_producer)

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(producer__display_name__icontains=q)
            | Q(producer__company_name__icontains=q)
            | Q(producer__user__first_name__icontains=q)
            | Q(producer__user__last_name__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    rows = []
    for need in qs:
        row = _build_need_row(need)
        if row["remaining_to_plan"] > 0:
            row["public_status_label"] = "Aberta"
            row["public_status"] = NeedStatus.OPEN
            row["public_quantity"] = row["remaining_to_plan"]
            row["public_offered_quantity"] = Decimal("0.000")
            need_product_id = getattr(need, "product_id", None) or getattr(getattr(need, "product", None), "id", "")
            response_query = urlencode({"product": str(need_product_id)})
            row["response_url"] = (
                f"{reverse('marketplace:need_respond', args=[need.id])}?{response_query}"
            )
            rows.append(row)

    if viewer_producer and rows:
        offered_quantities = get_public_offered_quantities_by_need(
            need_ids=[row["need"].id for row in rows],
            viewer_producer=viewer_producer,
        )
        summaries = get_need_response_summaries_for_responder(
            responder_producer=viewer_producer,
            need_ids=[row["need"].id for row in rows],
        )
        for row in rows:
            row["public_offered_quantity"] = offered_quantities.get(
                str(row["need"].id),
                Decimal("0.000"),
            )
            row["viewer_response_summary"] = summaries.get(str(row["need"].id))

    return rows


def list_marketplace_my_needs(*, producer, q="", category_id=""):
    qs = (
        Need.objects
        .select_related("producer", "producer__user", "product", "product__category")
        .filter(
            producer=producer,
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED, NeedStatus.COVERED],
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    return [_build_need_row(need) for need in qs]


def list_marketplace_my_published_needs(*, producer, q="", category_id=""):
    if not producer:
        return []
    qs = (
        Need.objects
        .select_related("producer", "producer__user", "product", "product__category")
        .filter(
            producer=producer,
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            is_marketplace_published=True,
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(Q(product__name__icontains=q) | Q(notes__icontains=q))

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    rows = []
    for need in qs:
        row = _build_need_row(need)
        row["public_quantity"] = row["remaining_to_plan"]
        rows.append(row)

    if rows:
        response_counts = get_need_response_counts_for_owner(
            owner_producer=producer,
            need_ids=[row["need"].id for row in rows],
        )
        for row in rows:
            row["response_count"] = response_counts.get(str(row["need"].id), 0)

    return rows


def get_need_response_counts_for_owner(*, owner_producer, need_ids):
    if not owner_producer or not need_ids:
        return {}

    rows = (
        MarketplaceListing.objects
        .filter(
            need_id__in=need_ids,
            need__producer=owner_producer,
        )
        .values("need_id")
        .annotate(total=Count("id"))
    )
    return {str(row["need_id"]): row["total"] for row in rows}


def get_need_candidate_products(producer):
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


def get_critical_stock_product_ids(producer, *, product_ids=None):
    if not producer:
        return set()

    qs = Stock.objects.filter(producer=producer)
    if product_ids:
        qs = qs.filter(product_id__in=product_ids)

    critical_product_ids = set()
    from apps.inventory.services import calculate_inventory_commitment_state

    for stock in qs.select_related("product").only(
        "product_id",
        "product__id",
        "product__name",
        "current_quantity",
        "reserved_quantity",
        "safety_stock",
    ):
        commitment_state = calculate_inventory_commitment_state(
            producer,
            stock.product,
            stock=stock,
        )
        if commitment_state.get("state_key") == "critical":
            critical_product_ids.add(str(stock.product_id))
    return critical_product_ids


def _get_need_response_listings_for_owner(*, owner_producer, q="", category_id="", need_id=""):
    qs = (
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
        .filter(
            need_id__isnull=False,
            need__producer=owner_producer,
        )
        .order_by("-published_at", "-created_at")
    )

    if need_id:
        qs = qs.filter(need_id=need_id)

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(producer__display_name__icontains=q)
            | Q(producer__company_name__icontains=q)
            | Q(producer__user__first_name__icontains=q)
            | Q(producer__user__last_name__icontains=q)
            | Q(notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    return qs


def _get_need_response_listing_queryset():
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
            "need__product",
        )
        .filter(need_id__isnull=False)
    )


def get_need_response_listing_for_viewer(*, viewer_producer, listing_id):
    if not viewer_producer:
        return None

    return (
        _get_need_response_listing_queryset()
        .filter(id=listing_id)
        .filter(Q(need__producer=viewer_producer) | Q(producer=viewer_producer))
        .first()
    )


def get_active_need_response_for_responder(*, responder_producer, need):
    if not responder_producer or not need:
        return None

    listings = list(
        _get_need_response_listing_queryset()
        .filter(producer=responder_producer, need=need)
        .order_by("-published_at", "-created_at")
    )
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    for listing in listings:
        state = _derive_need_response_state(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
        )
        if state["is_active"]:
            return listing
    return None


def get_editable_need_response_for_responder(*, responder_producer, listing_id):
    if not responder_producer or not listing_id:
        return None

    listing = (
        _get_need_response_listing_queryset()
        .filter(id=listing_id, producer=responder_producer)
        .exclude(need__producer=responder_producer)
        .first()
    )
    if not listing:
        return None

    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids([listing.id])
    state = _derive_need_response_state(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
    )
    if (
        state["is_active"]
        and state["status"] == NeedResponseStatus.PENDING
        and listing.status == ListingStatus.ACTIVE
    ):
        return listing
    return None


def _get_need_response_listing_for_update(listing_id):
    return (
        MarketplaceListing.objects
        # Lock only marketplace_listings. stock, forecast and need are null=True
        # (LEFT JOINs); PostgreSQL rejects FOR UPDATE on the nullable side of an
        # outer join, so of=("self",) is required here.
        .select_for_update(of=("self",))
        .select_related(
            "producer",
            "product",
            "stock",
            "forecast",
            "need",
            "need__producer",
        )
        .filter(need_id__isnull=False)
        .get(id=listing_id)
    )


def _listing_source_label(listing):
    has_stock_source = bool(getattr(listing, "stock_id", None))
    has_forecast_source = bool(getattr(listing, "forecast_id", None))
    if has_forecast_source and not has_stock_source:
        return "forecast", "Pré-venda"
    return "stock", "Disponível agora"


def get_need_response_order_snapshot(listing_ids):
    if not listing_ids:
        return {}

    rows = (
        OrderItem.objects
        .filter(
            listing_id__in=listing_ids,
            need_id__isnull=False,
        )
        .values_list("listing_id", "item_status", "quantity")
    )
    snapshots = {}
    for listing_id, item_status, quantity in rows:
        snapshot = snapshots.setdefault(
            listing_id,
            {
                "item_statuses": [],
                "active_or_completed_quantity": Decimal("0.000"),
                "cancelled_quantity": Decimal("0.000"),
                "completed_quantity": Decimal("0.000"),
                "ordered_quantity": Decimal("0.000"),
                "status": None,
            },
        )
        snapshot["item_statuses"].append(item_status)
        quantity = _quantize_need_quantity(quantity)
        if item_status == OrderItemStatus.CANCELLED:
            snapshot["cancelled_quantity"] = _quantize_need_quantity(snapshot["cancelled_quantity"] + quantity)
        else:
            snapshot["active_or_completed_quantity"] = _quantize_need_quantity(
                snapshot["active_or_completed_quantity"] + quantity
            )
        if item_status == OrderItemStatus.COMPLETED:
            snapshot["completed_quantity"] = _quantize_need_quantity(snapshot["completed_quantity"] + quantity)

    for snapshot in snapshots.values():
        statuses = snapshot["item_statuses"]
        if any(status == OrderItemStatus.COMPLETED for status in statuses):
            snapshot["status"] = NeedResponseStatus.COMPLETED
            snapshot["ordered_quantity"] = snapshot["completed_quantity"]
        elif any(status != OrderItemStatus.CANCELLED for status in statuses):
            snapshot["status"] = NeedResponseStatus.ACCEPTED
            snapshot["ordered_quantity"] = snapshot["active_or_completed_quantity"]
        elif statuses:
            snapshot["status"] = NeedResponseStatus.CANCELLED
            snapshot["ordered_quantity"] = snapshot["cancelled_quantity"]

    return snapshots


def _get_need_response_order_state_listing_ids(listing_ids):
    snapshots = get_need_response_order_snapshot(listing_ids)
    accepted_listing_ids = set()
    cancelled_listing_ids = set()
    completed_listing_ids = set()
    for listing_id, snapshot in snapshots.items():
        if snapshot["status"] == NeedResponseStatus.CANCELLED:
            cancelled_listing_ids.add(listing_id)
        elif snapshot["status"] == NeedResponseStatus.COMPLETED:
            completed_listing_ids.add(listing_id)
        elif snapshot["status"] == NeedResponseStatus.ACCEPTED:
            accepted_listing_ids.add(listing_id)
    return accepted_listing_ids, cancelled_listing_ids, completed_listing_ids


def _get_accepted_need_response_listing_ids(listing_ids):
    accepted_listing_ids, _, completed_listing_ids = _get_need_response_order_state_listing_ids(listing_ids)
    return accepted_listing_ids | completed_listing_ids


def _derive_need_response_state(
    listing,
    *,
    accepted_listing_ids=None,
    cancelled_listing_ids=None,
    completed_listing_ids=None,
    order_snapshots=None,
):
    accepted_listing_ids = accepted_listing_ids or set()
    cancelled_listing_ids = cancelled_listing_ids or set()
    completed_listing_ids = completed_listing_ids or set()
    order_snapshots = order_snapshots or {}
    order_snapshot = order_snapshots.get(listing.id) or {}
    response_status = getattr(listing, "need_response_status", NeedResponseStatus.PENDING)

    if response_status != NeedResponseStatus.REJECTED and order_snapshot.get("status"):
        response_status = order_snapshot["status"]

    if response_status == NeedResponseStatus.COMPLETED or listing.id in completed_listing_ids:
        return {
            "status": NeedResponseStatus.COMPLETED,
            "label": NeedResponseStatus.COMPLETED.label,
            "badge_class": "ok",
            "message": "A encomenda criada a partir desta oferta foi concluída.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.REJECTED:
        return {
            "status": "REJECTED",
            "label": NeedResponseStatus.REJECTED.label,
            "badge_class": "danger",
            "message": "Esta oferta foi rejeitada pelo produtor da necessidade.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.ACCEPTED or listing.id in accepted_listing_ids:
        return {
            "status": NeedResponseStatus.ACCEPTED,
            "label": NeedResponseStatus.ACCEPTED.label,
            "badge_class": "ok",
            "message": "Esta oferta já foi aceite e originou uma encomenda.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.CANCELLED or listing.id in cancelled_listing_ids:
        return {
            "status": NeedResponseStatus.CANCELLED,
            "label": NeedResponseStatus.CANCELLED.label,
            "badge_class": "danger",
            "message": "A encomenda criada a partir desta oferta foi cancelada.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if (
        response_status == NeedResponseStatus.EXPIRED
        or listing.status == ListingStatus.EXPIRED
        or is_listing_effectively_expired(listing)
    ):
        return {
            "status": NeedResponseStatus.EXPIRED,
            "label": NeedResponseStatus.EXPIRED.label,
            "badge_class": "muted",
            "message": "Esta oferta expirou.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.WITHDRAWN or listing.status == ListingStatus.CANCELLED:
        return {
            "status": NeedResponseStatus.WITHDRAWN,
            "label": NeedResponseStatus.WITHDRAWN.label,
            "badge_class": "muted",
            "message": "Esta oferta foi retirada.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if listing.status == ListingStatus.ACTIVE:
        return {
            "status": NeedResponseStatus.PENDING,
            "label": NeedResponseStatus.PENDING.label,
            "badge_class": "warn",
            "message": "Esta oferta aguarda decisão do produtor da necessidade.",
            "is_active": True,
            "can_buy": Decimal(str(listing.quantity_available or 0)) > Decimal("0"),
            "can_reject": True,
        }

    return {
        "status": listing.status,
        "label": listing.get_status_display(),
        "badge_class": "info",
        "message": "Esta oferta já não está no estado inicial.",
        "is_active": False,
        "can_buy": False,
        "can_reject": False,
    }


def _build_need_response(
    listing,
    *,
    accepted_listing_ids=None,
    cancelled_listing_ids=None,
    completed_listing_ids=None,
    order_snapshots=None,
):
    order_snapshots = order_snapshots or {}
    order_snapshot = order_snapshots.get(listing.id) or {}
    source_key, source_label = _listing_source_label(listing)
    state = _derive_need_response_state(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
        order_snapshots=order_snapshots,
    )
    is_editable = bool(
        state["is_active"]
        and state["status"] == NeedResponseStatus.PENDING
        and listing.status == ListingStatus.ACTIVE
    )
    return NeedResponse(
        listing=listing,
        id=listing.id,
        need_id=listing.need_id,
        producer_label=_producer_marketplace_display_name(listing.producer),
        need_owner_label=_producer_marketplace_display_name(getattr(getattr(listing, "need", None), "producer", None)),
        product_name=listing.product.name,
        product_unit=listing.product.unit,
        offered_quantity=_quantize_need_quantity(
            getattr(listing, "quantity_total", getattr(listing, "quantity_available", Decimal("0.000")))
        ),
        available_quantity=_quantize_need_quantity(listing.quantity_available),
        ordered_quantity=_quantize_need_quantity(order_snapshot.get("ordered_quantity") or Decimal("0.000")),
        quantity_available=listing.quantity_available,
        unit_price=listing.unit_price,
        source_key=source_key,
        source_label=source_label,
        status=listing.status,
        status_label=listing.get_status_display(),
        response_status=state["status"],
        response_status_label=state["label"],
        response_badge_class=state["badge_class"],
        response_message=state["message"],
        can_buy=state["can_buy"],
        can_reject=state["can_reject"],
        notes=listing.notes or "",
        detail_url=reverse("marketplace:proposal_detail", args=[listing.id]),
        reject_url=reverse("marketplace:proposal_reject", args=[listing.id]),
        edit_url=reverse("marketplace:proposal_edit", args=[listing.id]) if is_editable else "",
        is_editable=is_editable,
    )


def list_need_responses_for_owner(*, owner_producer, q="", category_id="", need_id=""):
    listings = list(
        _get_need_response_listings_for_owner(
            owner_producer=owner_producer,
            q=q,
            category_id=category_id,
            need_id=need_id,
        )
    )
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    order_snapshots = get_need_response_order_snapshot([listing.id for listing in listings])
    return [
        _build_need_response(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
            order_snapshots=order_snapshots,
        )
        for listing in listings
    ]


def build_need_response_for_listing(listing):
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids([listing.id])
    order_snapshots = get_need_response_order_snapshot([listing.id])
    return _build_need_response(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
        order_snapshots=order_snapshots,
    )


def list_need_responses_for_responder(*, responder_producer, q="", category_id=""):
    if not responder_producer:
        return []

    qs = (
        _get_need_response_listing_queryset()
        .filter(producer=responder_producer)
        .exclude(need__producer=responder_producer)
        .order_by("-published_at", "-created_at")
    )

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(need__producer__display_name__icontains=q)
            | Q(need__producer__company_name__icontains=q)
            | Q(need__producer__user__first_name__icontains=q)
            | Q(need__producer__user__last_name__icontains=q)
            | Q(notes__icontains=q)
            | Q(need__notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    listings = list(qs)
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    order_snapshots = get_need_response_order_snapshot([listing.id for listing in listings])
    return [
        _build_need_response(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
            order_snapshots=order_snapshots,
        )
        for listing in listings
    ]


def get_need_response_summaries_for_responder(*, responder_producer, need_ids):
    if not responder_producer or not need_ids:
        return {}

    listings = list(
        _get_need_response_listing_queryset()
        .filter(producer=responder_producer, need_id__in=need_ids)
        .order_by("need_id", "-published_at", "-created_at")
    )
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    order_snapshots = get_need_response_order_snapshot([listing.id for listing in listings])

    summaries = {}
    for listing in listings:
        need_key = str(listing.need_id)
        if need_key in summaries:
            continue
        state = _derive_need_response_state(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
            order_snapshots=order_snapshots,
        )
        can_edit = bool(
            state["is_active"]
            and state["status"] == NeedResponseStatus.PENDING
            and listing.status == ListingStatus.ACTIVE
        )
        summaries[need_key] = NeedResponseSummary(
            listing_id=listing.id,
            status=state["status"],
            status_label=state["label"],
            badge_class=state["badge_class"],
            message=state["message"],
            detail_url=reverse("marketplace:proposal_detail", args=[listing.id]),
            is_active=state["is_active"],
            edit_url=reverse("marketplace:proposal_edit", args=[listing.id]) if can_edit else "",
            can_edit=can_edit,
            can_send_new_proposal=state["status"] in {"REJECTED", "CANCELLED", "EXPIRED", "WITHDRAWN", "COMPLETED"},
        )

    return summaries


def _resolve_persisted_need_response_status(listing, order_snapshot):
    current_status = getattr(listing, "need_response_status", NeedResponseStatus.PENDING)
    if current_status == NeedResponseStatus.REJECTED:
        return NeedResponseStatus.REJECTED

    if order_snapshot and order_snapshot.get("status"):
        return order_snapshot["status"]

    if listing.status == ListingStatus.EXPIRED:
        return NeedResponseStatus.EXPIRED
    if listing.status == ListingStatus.CANCELLED:
        return NeedResponseStatus.WITHDRAWN
    return NeedResponseStatus.PENDING


def sync_need_response_status_for_listing(listing):
    if not listing or not getattr(listing, "need_id", None):
        return listing

    order_snapshot = get_need_response_order_snapshot([listing.id]).get(listing.id)
    next_status = _resolve_persisted_need_response_status(listing, order_snapshot)
    if listing.need_response_status != next_status:
        listing.need_response_status = next_status
        if hasattr(listing, "updated_at"):
            listing.updated_at = timezone.now()
            listing.save(update_fields=["need_response_status", "updated_at"])
        else:
            listing.save(update_fields=["need_response_status"])
    return listing


@transaction.atomic
def update_need_response(
    *,
    listing,
    responder_producer,
    quantity,
    unit_price,
    delivery_mode,
    delivery_radius_km=None,
    delivery_fee=None,
    notes=None,
    acting_user=None,
):
    from apps.marketplace.services import MarketplaceServiceError, update_listing

    listing = _get_need_response_listing_for_update(listing.id)
    if not responder_producer or listing.producer_id != responder_producer.id:
        raise ValidationError("Não pode editar esta proposta.")
    if not listing.need or listing.need.producer_id == responder_producer.id:
        raise ValidationError("Proposta inválida para edição.")

    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids([listing.id])
    state = _derive_need_response_state(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
    )
    if not (
        state["is_active"]
        and state["status"] == NeedResponseStatus.PENDING
        and listing.status == ListingStatus.ACTIVE
    ):
        raise ValidationError("Esta proposta já não pode ser editada.")

    try:
        updated_listing = update_listing(
            listing=listing,
            quantity_total=quantity,
            unit_price=unit_price,
            delivery_mode=delivery_mode,
            delivery_radius_km=delivery_radius_km,
            delivery_fee=delivery_fee,
            show_location_on_map=True,
            notes=notes,
            status=ListingStatus.ACTIVE,
            expires_at=None,
            photo_path=listing.photo_path,
            acting_user=acting_user,
        )
    except MarketplaceServiceError as exc:
        raise ValidationError(str(exc)) from exc

    if updated_listing.need_response_status != NeedResponseStatus.PENDING:
        updated_listing.need_response_status = NeedResponseStatus.PENDING
        updated_listing.save(update_fields=["need_response_status"])
    return updated_listing


@transaction.atomic
def reject_need_response(*, listing, owner_producer, acting_user=None):
    listing = _get_need_response_listing_for_update(listing.id)

    if not owner_producer or not listing.need or listing.need.producer_id != owner_producer.id:
        raise ValidationError("Não tem permissão para rejeitar esta oferta.")

    if _get_accepted_need_response_listing_ids([listing.id]):
        raise ValidationError("Esta oferta já foi aceite e não pode ser rejeitada.")

    if listing.need_response_status == NeedResponseStatus.REJECTED and listing.status == ListingStatus.CANCELLED:
        return False

    previous_values = {
        "status": listing.status,
        "need_response_status": listing.need_response_status,
    }
    listing.need_response_status = NeedResponseStatus.REJECTED
    listing.status = ListingStatus.CANCELLED
    listing.updated_at = timezone.now()
    listing.save(update_fields=["need_response_status", "status", "updated_at"])
    log_audit_event(
        actor=acting_user,
        action="NEED_RESPONSE_REJECTED",
        entity_type="marketplace_listings",
        entity_id=listing.id,
        notes="Proposta privada rejeitada pelo produtor que publicou a procura.",
        old_values=previous_values,
        new_values={
            "status": listing.status,
            "need_response_status": listing.need_response_status,
            "need_id": str(listing.need_id),
            "product_id": str(listing.product_id),
            "product_name": getattr(getattr(listing, "product", None), "name", None),
            "producer_id": str(listing.producer_id),
        },
    )
    try:
        from apps.alerts.models import AlertSeverity, AlertType
        from apps.alerts.services import create_need_response_event_alert

        create_need_response_event_alert(
            target_producer=listing.producer,
            listing=listing,
            alert_type=AlertType.OFFER_REJECTED,
            title=f"Oferta rejeitada: {listing.product.name}",
            description="O produtor da necessidade rejeitou a sua proposta. Pode enviar uma nova proposta se fizer sentido.",
            action_url=f"/marketplace/propostas/{listing.id}/",
            action_label="Ver proposta",
            acting_user=getattr(owner_producer, "user", None),
            severity=AlertSeverity.INFO,
            requires_action=False,
        )
    except Exception:
        pass
    return True
