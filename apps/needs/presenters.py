from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import ProductionForecast
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.marketplace.services import (
    get_forecast_available_quantity,
    get_max_publishable_quantity,
    get_stock_for_product,
)
from apps.needs.forms import ExternalCustomerDemandForm, NeedEditForm
from apps.needs.navigation import build_needs_index_url
from apps.needs.models import (
    ExternalCustomerDemand,
    ExternalCustomerDemandStatus,
    NeedResponseStatus,
    NeedStatus,
)
from apps.needs.services import (
    build_external_demand_plans,
    calculate_external_demand_plan,
    calculate_need_coverage,
    get_critical_stock_product_ids,
    get_external_customer_demand_for_producer,
    get_external_customer_demand_summary,
    get_need_candidate_products,
    get_need_edit_help_text,
    get_need_for_producer,
    get_need_minimum_edit_quantity,
    get_need_response_counts_for_owner,
    list_external_customer_demands,
    list_marketplace_my_needs,
    list_marketplace_public_needs,
    list_need_responses_for_owner,
    normalize_external_demands_search_query,
    normalize_needs_search_query,
)


def build_selected_need_row(need):
    coverage = calculate_need_coverage(need)
    required_quantity = coverage["required_quantity"]
    completed_qty = coverage["completed_qty"]
    progress_percent = Decimal("0")
    if required_quantity > 0:
        progress_percent = (completed_qty / required_quantity) * Decimal("100")
    minimum_edit_quantity = get_need_minimum_edit_quantity(coverage)
    return {
        "need": need,
        "status": need.status,
        "status_label": need.get_status_display(),
        "required_quantity": required_quantity,
        "planned_qty": coverage["planned_qty"],
        "completed_qty": completed_qty,
        "remaining_to_plan": coverage["remaining_to_plan"],
        "remaining_to_receive": coverage["remaining_to_receive"],
        "planned_excess_qty": max(
            coverage["planned_qty"] - required_quantity,
            Decimal("0.000"),
        ),
        "progress_percent": max(Decimal("0"), min(progress_percent, Decimal("100"))),
        "can_edit": need.status in {NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED, NeedStatus.COVERED},
        "minimum_edit_quantity": minimum_edit_quantity,
        "edit_help_text": get_need_edit_help_text(need, coverage),
    }


def build_need_response_inventory_context(producer, product):
    stock = get_stock_for_product(producer, product)
    forecast_summary = build_need_response_forecast_context(producer, product)
    if not stock:
        return {
            "has_stock": False,
            **forecast_summary,
            "stock": None,
            "current_quantity": Decimal("0.000"),
            "reserved_quantity": Decimal("0.000"),
            "available_quantity": Decimal("0.000"),
            "safety_stock": Decimal("0.000"),
            "max_publishable_quantity": Decimal("0.000"),
        }

    current_quantity = Decimal(str(stock.current_quantity or 0))
    reserved_quantity = Decimal(str(stock.reserved_quantity or 0))
    safety_stock = Decimal(str(stock.safety_stock or 0))
    available_quantity = current_quantity - reserved_quantity

    return {
        "has_stock": True,
        **forecast_summary,
        "stock": stock,
        "current_quantity": current_quantity,
        "reserved_quantity": reserved_quantity,
        "available_quantity": available_quantity,
        "safety_stock": safety_stock,
        "max_publishable_quantity": get_max_publishable_quantity(stock),
    }


def build_need_response_forecast_context(producer, product):
    zero = Decimal("0.000")
    if not producer or not product:
        return {
            "has_forecast": False,
            "forecast_count": 0,
            "forecast_quantity": zero,
            "forecast_reserved_quantity": zero,
            "forecast_available_quantity": zero,
            "forecast_presale_available_quantity": zero,
        }

    forecasts = list(
        ProductionForecast.objects
        .filter(producer=producer, product=product, forecast_quantity__gt=zero)
        .only(
            "id",
            "producer_id",
            "product_id",
            "forecast_quantity",
            "reserved_quantity",
            "is_marketplace_enabled",
        )
    )
    forecast_quantity = zero
    reserved_quantity = zero
    available_quantity = zero
    presale_available_quantity = zero

    for forecast in forecasts:
        current_forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
        current_reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
        current_available_quantity = max(
            current_forecast_quantity - current_reserved_quantity,
            zero,
        )

        forecast_quantity += current_forecast_quantity
        reserved_quantity += current_reserved_quantity
        available_quantity += current_available_quantity
        if forecast.is_marketplace_enabled:
            presale_available_quantity += get_forecast_available_quantity(forecast)

    return {
        "has_forecast": available_quantity > zero,
        "forecast_count": len(forecasts),
        "forecast_quantity": forecast_quantity,
        "forecast_reserved_quantity": reserved_quantity,
        "forecast_available_quantity": available_quantity,
        "forecast_presale_available_quantity": presale_available_quantity,
    }


def get_needs_filters(request):
    source = request.POST if request.method == "POST" else request.GET
    q = normalize_needs_search_query(source.get("q"))
    category_id = (source.get("category") or "").strip()
    need_id = (source.get("need") or "").strip()
    requested_product_id = (source.get("product") or source.get("product_id") or "").strip()
    requested_quantity = (source.get("qty") or source.get("required_quantity") or "").strip()
    show_need_form = (source.get("show_need_form") or "").strip().lower() in {"1", "true", "yes", "on"}
    return q, category_id, need_id, requested_product_id, requested_quantity, show_need_form


def add_form_errors_to_messages(request, form):
    for field, errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else ""
        for error in errors:
            messages.error(request, f"{label}: {error}" if label else str(error))


def _validation_error_text(exc):
    messages_list = getattr(exc, "messages", None)
    return messages_list[0] if messages_list else str(exc)


def build_external_demands_url(*, q="", status="", product_id="", show_form=False, edit_id=""):
    params = {}
    if q:
        params["q"] = q
    if status:
        params["status"] = status
    if product_id:
        params["product"] = product_id
    if show_form:
        params["show_form"] = "1"
    if edit_id:
        params["edit"] = edit_id
    query = urlencode(params)
    base_url = reverse("needs:external_demands")
    return f"{base_url}?{query}" if query else base_url


def get_external_demands_filters(request):
    source = request.POST if request.method == "POST" else request.GET
    q = normalize_external_demands_search_query(source.get("q"))
    status = (source.get("status") or "").strip()
    product_id = (source.get("product") or source.get("product_id") or "").strip()
    edit_id = (source.get("edit") or "").strip()
    show_form = (source.get("show_form") or "").strip().lower() in {"1", "true", "yes", "on"}
    return q, status, product_id, edit_id, show_form


def build_external_demands_context(
    producer,
    *,
    q="",
    status="",
    product_id="",
    show_form=False,
    edit_id="",
    create_form=None,
    edit_form=None,
):
    demand_rows = list(
        list_external_customer_demands(
            producer=producer,
            q=q,
            status=status,
            product_id=product_id,
        )
    )
    today = timezone.now().date()
    active_statuses_set = {
        ExternalCustomerDemandStatus.OPEN,
        ExternalCustomerDemandStatus.PARTIALLY_COVERED,
        ExternalCustomerDemandStatus.COVERED,
    }
    products = list(get_need_candidate_products(producer)) if producer else []
    selected_demand = None
    if edit_id:
        selected_demand = get_external_customer_demand_for_producer(
            producer=producer,
            demand_id=edit_id,
        )
        if selected_demand and selected_demand.status in {
            ExternalCustomerDemandStatus.OPEN,
            ExternalCustomerDemandStatus.PARTIALLY_COVERED,
            ExternalCustomerDemandStatus.COVERED,
        }:
            edit_form = edit_form or ExternalCustomerDemandForm(producer=producer, demand=selected_demand)
        else:
            selected_demand = None
            edit_id = ""

    demand_plans = build_external_demand_plans(
        producer=producer,
        product_id=product_id,
    )
    plan_rows_by_product = {
        str(plan["product"].id): {
            row["delivery_date"]: row
            for row in plan.get("rows", [])
        }
        for plan in demand_plans
    }
    for demand in demand_rows:
        days = (demand.requested_delivery_date - today).days
        demand.days_remaining = days
        if demand.status not in active_statuses_set:
            demand.urgency = "inactive"
            demand.coverage_key = "inactive"
        else:
            if days < 0:
                demand.urgency = "overdue"
            elif days <= 7:
                demand.urgency = "soon"
            elif days <= 14:
                demand.urgency = "warning"
            else:
                demand.urgency = "ok"
            plan_row = plan_rows_by_product.get(str(demand.product_id), {}).get(
                demand.requested_delivery_date
            )
            deficit = plan_row["deficit_until_date"] if plan_row else Decimal("0.000")
            remaining = plan_row["remaining_capacity_until_date"] if plan_row else Decimal("0.000")
            demand.stock_diff = -deficit if deficit > Decimal("0.000") else remaining
            demand.stock_deficit = max(-demand.stock_diff, Decimal("0.000"))
            demand.stock_surplus = max(demand.stock_diff, Decimal("0.000"))
            if demand.stock_diff < Decimal("0.000"):
                demand.coverage_key = "deficit"
            elif demand.stock_diff == Decimal("0.000"):
                demand.coverage_key = "no_margin"
            else:
                demand.coverage_key = "covered"
    summary = get_external_customer_demand_summary(
        producer=producer,
        demand_plans=demand_plans,
    )

    return {
        "page_title": "Pedidos de clientes",
        "q": q,
        "selected_status": status,
        "selected_product_id": product_id,
        "show_form": show_form,
        "demand_rows": demand_rows,
        "active_demand_rows": [
            demand for demand in demand_rows if demand.status in active_statuses_set
        ],
        "past_demand_rows": [
            demand for demand in demand_rows if demand.status not in active_statuses_set
        ],
        "products": products,
        "status_choices": ExternalCustomerDemandStatus.choices,
        "active_statuses": {
            ExternalCustomerDemandStatus.OPEN,
            ExternalCustomerDemandStatus.PARTIALLY_COVERED,
            ExternalCustomerDemandStatus.COVERED,
        },
        "summary": summary,
        "demand_plans": demand_plans,
        "create_form": create_form or ExternalCustomerDemandForm(producer=producer),
        "selected_demand": selected_demand,
        "edit_form": edit_form,
        "edit_id": str(selected_demand.id) if selected_demand else "",
        "current_url": build_external_demands_url(
            q=q,
            status=status,
            product_id=product_id,
            show_form=show_form,
            edit_id=str(selected_demand.id) if selected_demand else "",
        ),
    }


def build_needs_index_context(
    producer,
    *,
    q,
    category_id,
    selected_need_id="",
    need_prefill_product_id="",
    need_prefill_quantity="",
    show_need_form=False,
    edit_need_id="",
    need_edit_form=None,
):
    need_public_rows = list_marketplace_public_needs(
        viewer_producer=producer,
        q=q,
        category_id=category_id,
    )
    need_my_rows = list_marketplace_my_needs(
        producer=producer,
        q=q,
        category_id=category_id,
    ) if producer else []
    response_counts = get_need_response_counts_for_owner(
        owner_producer=producer,
        need_ids=[row["need"].id for row in need_my_rows],
    ) if producer else {}
    for row in need_my_rows:
        row["response_count"] = response_counts.get(str(row["need"].id), 0)

    need_products = list(get_need_candidate_products(producer)) if producer else []
    critical_product_ids = get_critical_stock_product_ids(
        producer,
        product_ids=[
            product_id
            for product_id in (getattr(product, "id", None) for product in need_products)
            if product_id
        ],
    ) if producer else set()
    for product in need_products:
        product_id = getattr(product, "id", None)
        product.is_critical_stock = bool(product_id and str(product_id) in critical_product_ids)

    category_map = {}
    for row in [*need_public_rows, *need_my_rows]:
        category = getattr(getattr(row["need"], "product", None), "category", None)
        if category:
            category_map[str(category.id)] = category
    if producer:
        demand_categories = (
            ExternalCustomerDemand.objects
            .filter(producer=producer, status__in=[
                ExternalCustomerDemandStatus.OPEN,
                ExternalCustomerDemandStatus.PARTIALLY_COVERED,
                ExternalCustomerDemandStatus.COVERED,
            ])
            .select_related("product__category")
        )
        for demand in demand_categories:
            category = getattr(getattr(demand, "product", None), "category", None)
            if category:
                category_map[str(category.id)] = category
    available_categories = sorted(
        category_map.values(),
        key=lambda category: (category.name or "").lower(),
    )

    validated_need_id = ""
    selected_need_row = None
    if selected_need_id and producer:
        selected_need = get_need_for_producer(producer=producer, need_id=selected_need_id)
        if selected_need and selected_need.status != NeedStatus.IGNORED:
            validated_need_id = str(selected_need.id)
            if not need_prefill_product_id:
                need_prefill_product_id = str(selected_need.product_id)
            matched_row = next(
                (row for row in need_my_rows if str(row["need"].id) == str(selected_need.id)),
                None,
            )
            if matched_row and not need_prefill_quantity:
                need_prefill_quantity = str(matched_row["remaining_to_plan"])
            selected_need_row = matched_row or build_selected_need_row(selected_need)

    # Flat cards: não auto-selecionar a primeira need.
    # Cards ficam colapsados por defeito; só expandem se houver selected_need_id explícito.

    validated_edit_need_id = ""
    is_editing_selected_need = False
    if (
        edit_need_id
        and selected_need_row
        and str(edit_need_id) == str(selected_need_row["need"].id)
        and selected_need_row.get("can_edit")
    ):
        validated_edit_need_id = str(selected_need_row["need"].id)
        is_editing_selected_need = True
        if need_edit_form is None:
            need_edit_form = NeedEditForm(need=selected_need_row["need"])

    need_response_rows = (
        list_need_responses_for_owner(
            owner_producer=producer,
            q=q,
            category_id=category_id,
            need_id=validated_need_id,
        )
        if producer and validated_need_id
        else []
    )
    active_need_response_rows = [
        response for response in need_response_rows
        if response.response_status == "PENDING"
    ]
    all_need_response_rows = (
        list_need_responses_for_owner(
            owner_producer=producer,
            q=q,
            category_id=category_id,
        )
        if producer
        else []
    )
    all_active_received_proposals = [
        response for response in all_need_response_rows
        if response.response_status == "PENDING"
    ]
    visible_active_received_proposals = [
        response for response in all_active_received_proposals
        if not validated_need_id or str(getattr(response, "need_id", "")) != validated_need_id
    ]

    generated_needs_count = sum(
        1 for row in need_my_rows
        if row.get("status") in {NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED}
    )
    received_proposals_pending_count = len(all_active_received_proposals)
    sent_proposals_pending_count = (
        MarketplaceListing.objects.filter(
            producer=producer,
            need_id__isnull=False,
            status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
            need_response_status=NeedResponseStatus.PENDING,
        ).count()
        if producer
        else 0
    )

    active_statuses = [
        ExternalCustomerDemandStatus.OPEN,
        ExternalCustomerDemandStatus.PARTIALLY_COVERED,
        ExternalCustomerDemandStatus.COVERED,
    ]

    external_demands_open_count = (
        ExternalCustomerDemand.objects.filter(
            producer=producer,
            status__in=active_statuses,
        ).count()
        if producer
        else 0
    )

    # Preview de pedidos externos para mostrar no topo da página
    active_demands_preview = []
    past_demands_preview = []
    has_more_demands = False
    preview_demand_kpis = {}
    if producer:
        active_demands_qs = list_external_customer_demands(
            producer=producer,
            q=q,
            category_id=category_id,
            active_only=True,
        )
        demands_sample = list(active_demands_qs[:9])
        has_more_demands = len(demands_sample) > 8
        active_demands_preview = demands_sample[:8]

        plans_rows_by_product = {}
        for demand in active_demands_preview:
            pid = str(demand.product_id)
            if pid not in plans_rows_by_product:
                plan = calculate_external_demand_plan(producer=producer, product=demand.product)
                plans_rows_by_product[pid] = {r["delivery_date"]: r for r in plan["rows"]}

        today = timezone.now().date()
        for demand in active_demands_preview:
            pid = str(demand.product_id)
            plan_row = plans_rows_by_product.get(pid, {}).get(demand.requested_delivery_date)
            if plan_row:
                deficit = plan_row["deficit_until_date"]
                remaining = plan_row["remaining_capacity_until_date"]
                demand.capacity_until_date = plan_row["capacity_until_date"]
                demand.stock_diff = -deficit if deficit > Decimal("0") else remaining
            else:
                demand.capacity_until_date = Decimal("0")
                demand.stock_diff = Decimal("0")
            days = (demand.requested_delivery_date - today).days
            if days < 0:
                demand.urgency = "overdue"
            elif days <= 7:
                demand.urgency = "soon"
            elif days <= 14:
                demand.urgency = "warning"
            else:
                demand.urgency = "ok"
            demand.days_remaining = days
            demand.stock_deficit = max(-demand.stock_diff, Decimal("0"))
            demand.stock_surplus = max(demand.stock_diff, Decimal("0"))
            if demand.stock_diff < Decimal("0"):
                demand.coverage_key = "deficit"
            elif demand.stock_diff == Decimal("0"):
                demand.coverage_key = "no_margin"
            else:
                demand.coverage_key = "covered"

        preview_demand_kpis = {
            "total_count": len(active_demands_preview),
            "deficit_count": sum(1 for d in active_demands_preview if d.stock_diff < Decimal("0")),
            "covered_count": sum(1 for d in active_demands_preview if d.stock_diff >= Decimal("0")),
            "overdue_count": sum(1 for d in active_demands_preview if getattr(d, "urgency", "") == "overdue"),
        }
        past_demands_preview = [
            demand for demand in list(
                list_external_customer_demands(
                    producer=producer,
                    q=q,
                    category_id=category_id,
                )
            )
            if demand.status in {
                ExternalCustomerDemandStatus.FULFILLED,
                ExternalCustomerDemandStatus.CANCELLED,
            }
        ]
        past_demands_preview.sort(
            key=lambda demand: getattr(demand, "updated_at", None) or timezone.now(),
            reverse=True,
        )
        past_demands_preview = past_demands_preview[:5]
        for demand in past_demands_preview:
            demand.result_at = demand.fulfilled_at or demand.cancelled_at or demand.updated_at

    return {
        "page_title": "Necessidades",
        "q": q,
        "selected_category_id": category_id,
        "need_public_rows": need_public_rows,
        "need_my_rows": need_my_rows,
        "need_products": need_products,
        "need_response_rows": need_response_rows,
        "active_need_response_rows": active_need_response_rows,
        "all_active_received_proposals": all_active_received_proposals,
        "visible_active_received_proposals": visible_active_received_proposals,
        "external_demands_open_count": external_demands_open_count,
        "active_demands_preview": active_demands_preview,
        "past_demands_preview": past_demands_preview,
        "has_more_demands": has_more_demands,
        "preview_demand_kpis": preview_demand_kpis,
        "generated_needs_count": generated_needs_count,
        "received_proposals_pending_count": received_proposals_pending_count,
        "sent_proposals_pending_count": sent_proposals_pending_count,
        "selected_need_id": validated_need_id,
        "selected_need_row": selected_need_row,
        "need_prefill_product_id": need_prefill_product_id,
        "need_prefill_quantity": need_prefill_quantity,
        "show_need_form": bool(show_need_form),
        "edit_need_id": validated_edit_need_id,
        "is_editing_selected_need": is_editing_selected_need,
        "need_edit_form": need_edit_form,
        "edit_need_url": build_needs_index_url(
            q=q,
            category_id=category_id,
            selected_need_id=validated_need_id,
            need_prefill_product_id=need_prefill_product_id,
            need_prefill_quantity=need_prefill_quantity,
            show_need_form=show_need_form,
            edit_need_id=validated_need_id,
        ) if selected_need_row and selected_need_row.get("can_edit") else "",
        "close_edit_need_url": build_needs_index_url(
            q=q,
            category_id=category_id,
            selected_need_id=validated_need_id,
            need_prefill_product_id=need_prefill_product_id,
            need_prefill_quantity=need_prefill_quantity,
            show_need_form=show_need_form,
        ),
        "available_categories": available_categories,
        "can_publish": bool(producer),
        "current_url": build_needs_index_url(
            q=q,
            category_id=category_id,
            selected_need_id=validated_need_id,
            need_prefill_product_id=need_prefill_product_id,
            need_prefill_quantity=need_prefill_quantity,
            show_need_form=show_need_form,
            edit_need_id=validated_edit_need_id,
        ),
    }
