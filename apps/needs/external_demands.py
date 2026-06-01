from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.catalog.models import Product
from apps.common.audit import log_audit_event
from apps.inventory.models import ProductionForecast, Stock, StockMovement, StockMovementType
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.needs.audit import (
    audit_quantity as _audit_quantity,
    external_demand_audit_values as _external_demand_audit_values,
    need_audit_values as _need_audit_values,
    need_marketplace_audit_values as _need_marketplace_audit_values,
)
from apps.needs.constants import (
    CUSTOMER_DEMAND_NEED_NOTES,
    EXTERNAL_DEMAND_ACTIVE_STATUSES,
    EXTERNAL_DEMAND_EDITABLE_STATUSES,
)
from apps.needs.coverage import recalculate_need_status
from apps.needs.models import (
    ExternalCustomerDemand,
    ExternalCustomerDemandSourceSystem,
    ExternalCustomerDemandStatus,
    Need,
    NeedResponseStatus,
    NeedSourceSystem,
    NeedStatus,
)
from apps.needs.utils import (
    as_local_date as _as_local_date,
    clean_optional_text as _clean_optional_text,
    is_uuid_like as _is_uuid_like,
    normalize_external_demands_search_query,
    normalize_needed_by_date as _normalize_needed_by_date,
    quantize_need_quantity as _quantize_need_quantity,
)


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
