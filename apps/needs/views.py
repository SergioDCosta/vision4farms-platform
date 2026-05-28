from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.common.audit import log_audit_event
from apps.common.decorators import client_only_required
from apps.common.htmx import with_htmx_toast
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.marketplace.services import (
    MarketplaceServiceError,
    build_delivery_text,
    create_listing,
    get_forecast_available_quantity,
    get_current_producer_for_user,
    get_max_publishable_quantity,
    get_stock_for_product,
)
from django.utils import timezone
from apps.inventory.models import ProductionForecast, Stock
from apps.needs.forms import ExternalCustomerDemandForm, NeedCreateForm, NeedEditForm, NeedResponseEditForm, NeedResponsePublishForm
from apps.needs.navigation import build_needs_index_url
from apps.needs.models import ExternalCustomerDemand, ExternalCustomerDemandStatus, Need, NeedResponseStatus, NeedSourceSystem, NeedStatus
from apps.needs.services import (
    calculate_external_demand_plan,
    calculate_need_coverage,
    cancel_external_customer_demand,
    DuplicateActiveNeedError,
    create_external_customer_demand,
    create_need,
    evaluate_external_demand_conflict_with_listings,
    build_need_response_for_listing,
    get_need_edit_help_text,
    get_need_minimum_edit_quantity,
    get_active_need_response_for_responder,
    get_critical_stock_product_ids,
    get_external_customer_demand_for_producer,
    get_external_customer_demand_summary,
    get_editable_need_response_for_responder,
    get_need_response_listing_for_viewer,
    get_need_candidate_products,
    get_need_for_producer,
    get_need_response_counts_for_owner,
    get_need_response_summaries_for_responder,
    ignore_need,
    list_need_responses_for_owner,
    build_external_demand_plans,
    list_external_customer_demands,
    list_marketplace_my_needs,
    list_marketplace_public_needs,
    mark_external_customer_demand_fulfilled,
    normalize_external_demands_search_query,
    normalize_needs_search_query,
    publish_need_to_marketplace,
    reject_need_response,
    update_external_customer_demand,
    update_need,
    update_need_response,
    withdraw_need_from_marketplace,
)


def sync_alerts_after_need_change(producer, acting_user):
    try:
        from apps.alerts.services import sync_alerts_for_producer
        sync_alerts_for_producer(producer, acting_user=acting_user)
    except Exception:
        return


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _warn_listing_conflict_for_external_demand(request, producer, product):
    conflict = evaluate_external_demand_conflict_with_listings(
        producer=producer,
        product=product,
    )
    if not conflict:
        return None
    deficit = conflict.get("max_deficit") or Decimal("0.000")
    deficit_date = conflict.get("first_deficit_date")
    published = conflict.get("published_quantity") or Decimal("0.000")
    count = conflict.get("affected_listings_count") or 0
    date_phrase = f" antes de {deficit_date:%d/%m/%Y}" if deficit_date else ""
    message = (
        f"Atenção: tem {published:.3f} {product.unit} publicados em "
        f"{count} anúncio{'s' if count != 1 else ''} ativo{'s' if count != 1 else ''} "
        f"que podem comprometer este pedido (défice de "
        f"{deficit:.3f} {product.unit}{date_phrase}). "
        "Reveja os seus anúncios em \"Meus anúncios\" no Marketplace."
    )
    messages.warning(request, message)
    return message


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


@client_only_required
def external_customer_demands_view(request):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    q, status, product_id, edit_id, show_form = get_external_demands_filters(request)
    context = build_external_demands_context(
        producer,
        q=q,
        status=status,
        product_id=product_id,
        edit_id=edit_id,
        show_form=show_form,
    )
    return render(request, "needs/external_demands.html", context)


@client_only_required
def external_customer_demand_create_view(request):
    if request.method != "POST":
        return redirect("needs:external_demands")

    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    q, status, product_id, _, _ = get_external_demands_filters(request)
    form = ExternalCustomerDemandForm(request.POST, producer=producer)
    if form.is_valid():
        new_product = form.cleaned_data["product_id"]
        published_need_ids_before = set(
            Need.objects.filter(
                producer=producer,
                product=new_product,
                source_system=NeedSourceSystem.CUSTOMER_DEMAND,
                is_marketplace_published=True,
            ).values_list("id", flat=True)
        )
        try:
            create_external_customer_demand(
                producer=producer,
                product=new_product,
                client_name=form.cleaned_data["client_name"],
                client_contact=form.cleaned_data.get("client_contact"),
                client_reference=form.cleaned_data.get("client_reference"),
                requested_quantity=form.cleaned_data["requested_quantity"],
                requested_delivery_date=form.cleaned_data["requested_delivery_date"],
                notes=form.cleaned_data.get("notes"),
                created_by=request.current_user,
            )
        except ValidationError as exc:
            form.add_error(None, str(exc))
        else:
            auto_unpublished = False
            if published_need_ids_before:
                still_published_ids = set(
                    Need.objects.filter(
                        id__in=published_need_ids_before,
                        is_marketplace_published=True,
                    ).values_list("id", flat=True)
                )
                auto_unpublished = bool(published_need_ids_before - still_published_ids)

            messages.success(request, "Pedido externo registado com sucesso.")
            if auto_unpublished:
                messages.warning(
                    request,
                    "A procura associada foi retirada do marketplace porque os termos mudaram. "
                    "Pode republicá-la em Necessidades se quiser voltar a receber propostas.",
                )
            listing_conflict_message = _warn_listing_conflict_for_external_demand(
                request, producer, new_product
            )
            sync_alerts_after_need_change(producer, request.current_user)
            next_url = build_external_demands_url(q=q, status=status, product_id=product_id)
            if _is_htmx(request):
                context = build_external_demands_context(
                    producer,
                    q=q,
                    status=status,
                    product_id=product_id,
                )
                response = render(request, "needs/external_demands.html", context)
                if listing_conflict_message:
                    with_htmx_toast(response, "warning", listing_conflict_message)
                elif auto_unpublished:
                    with_htmx_toast(
                        response,
                        "warning",
                        "Pedido registado. A procura no marketplace foi retirada porque a quantidade ou data mudou — republique se ainda quiser receber propostas.",
                    )
                else:
                    with_htmx_toast(response, "success", "Pedido externo registado com sucesso.")
                return response
            return redirect(next_url)

    add_form_errors_to_messages(request, form)
    context = build_external_demands_context(
        producer,
        q=q,
        status=status,
        product_id=product_id,
        show_form=True,
        create_form=form,
    )
    return render(request, "needs/external_demands.html", context)


@client_only_required
def external_customer_demand_edit_view(request, demand_id):
    if request.method != "POST":
        return redirect(build_external_demands_url(edit_id=str(demand_id)))

    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    q, status, product_id, _, show_form = get_external_demands_filters(request)
    demand = get_external_customer_demand_for_producer(producer=producer, demand_id=demand_id)
    if not demand:
        messages.error(request, "Pedido externo não encontrado.")
        return redirect("needs:external_demands")

    form = ExternalCustomerDemandForm(request.POST, producer=producer, demand=demand)
    if form.is_valid():
        new_product = form.cleaned_data["product_id"]
        published_need_ids_before = set(
            Need.objects.filter(
                producer=producer,
                product__in={demand.product, new_product},
                source_system=NeedSourceSystem.CUSTOMER_DEMAND,
                is_marketplace_published=True,
            ).values_list("id", flat=True)
        )
        try:
            update_external_customer_demand(
                demand=demand,
                producer=producer,
                product=new_product,
                client_name=form.cleaned_data["client_name"],
                client_contact=form.cleaned_data.get("client_contact"),
                client_reference=form.cleaned_data.get("client_reference"),
                requested_quantity=form.cleaned_data["requested_quantity"],
                requested_delivery_date=form.cleaned_data["requested_delivery_date"],
                notes=form.cleaned_data.get("notes"),
                updated_by=request.current_user,
            )
        except ValidationError as exc:
            form.add_error(None, str(exc))
        else:
            auto_unpublished = False
            if published_need_ids_before:
                still_published_ids = set(
                    Need.objects.filter(
                        id__in=published_need_ids_before,
                        is_marketplace_published=True,
                    ).values_list("id", flat=True)
                )
                auto_unpublished = bool(published_need_ids_before - still_published_ids)

            messages.success(request, "Pedido externo atualizado com sucesso.")
            if auto_unpublished:
                messages.warning(
                    request,
                    "A procura associada foi retirada do marketplace porque os termos mudaram. "
                    "Pode republicá-la em Necessidades se quiser voltar a receber propostas.",
                )
            listing_conflict_message = _warn_listing_conflict_for_external_demand(
                request, producer, new_product
            )
            sync_alerts_after_need_change(producer, request.current_user)
            next_url = build_external_demands_url(q=q, status=status, product_id=product_id)
            if _is_htmx(request):
                context = build_external_demands_context(
                    producer,
                    q=q,
                    status=status,
                    product_id=product_id,
                )
                response = render(request, "needs/external_demands.html", context)
                if listing_conflict_message:
                    with_htmx_toast(response, "warning", listing_conflict_message)
                elif auto_unpublished:
                    with_htmx_toast(
                        response,
                        "warning",
                        "Pedido atualizado. A procura no marketplace foi retirada porque a quantidade ou data mudou — republique se ainda quiser receber propostas.",
                    )
                else:
                    with_htmx_toast(response, "success", "Pedido externo atualizado com sucesso.")
                return response
            return redirect(next_url)

    add_form_errors_to_messages(request, form)
    context = build_external_demands_context(
        producer,
        q=q,
        status=status,
        product_id=product_id,
        show_form=show_form,
        edit_id=str(demand.id),
        edit_form=form,
    )
    return render(request, "needs/external_demands.html", context)


@client_only_required
def external_customer_demand_cancel_view(request, demand_id):
    if request.method != "POST":
        return redirect("needs:external_demands")

    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    q, status, product_id, _, show_form = get_external_demands_filters(request)
    demand = get_external_customer_demand_for_producer(producer=producer, demand_id=demand_id)
    if not demand:
        messages.error(request, "Pedido externo não encontrado.")
        return redirect("needs:external_demands")

    toast_level, toast_msg = "info", ""
    try:
        _, changed = cancel_external_customer_demand(
            demand=demand,
            producer=producer,
            updated_by=request.current_user,
        )
    except ValidationError as exc:
        toast_level, toast_msg = "error", _validation_error_text(exc)
    else:
        if changed:
            toast_level, toast_msg = "success", "Pedido externo cancelado."
            sync_alerts_after_need_change(producer, request.current_user)
        else:
            toast_msg = "O pedido externo já estava cancelado."

    if _is_htmx(request):
        context = build_external_demands_context(
            producer,
            q=q,
            status=status,
            product_id=product_id,
            show_form=show_form,
        )
        response = render(request, "needs/external_demands.html", context)
        return with_htmx_toast(response, toast_level, toast_msg)
    if toast_level == "error":
        messages.error(request, toast_msg)
    elif toast_level == "success":
        messages.success(request, toast_msg)
    else:
        messages.info(request, toast_msg)
    return redirect(build_external_demands_url(q=q, status=status, product_id=product_id, show_form=show_form))


@client_only_required
def external_customer_demand_fulfill_view(request, demand_id):
    if request.method != "POST":
        return redirect("needs:external_demands")

    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    q, status, product_id, _, show_form = get_external_demands_filters(request)
    demand = get_external_customer_demand_for_producer(producer=producer, demand_id=demand_id)
    if not demand:
        messages.error(request, "Pedido externo não encontrado.")
        return redirect("needs:external_demands")

    toast_level, toast_msg = "info", ""
    try:
        _, changed = mark_external_customer_demand_fulfilled(
            demand=demand,
            producer=producer,
            updated_by=request.current_user,
        )
    except ValidationError as exc:
        toast_level, toast_msg = "error", _validation_error_text(exc)
    else:
        if changed:
            toast_level = "success"
            toast_msg = "Pedido marcado como cumprido. A saída foi registada no stock e os compromissos foram recalculados."
            sync_alerts_after_need_change(producer, request.current_user)
        else:
            toast_msg = "O pedido externo já estava marcado como cumprido."

    if _is_htmx(request):
        context = build_external_demands_context(
            producer,
            q=q,
            status=status,
            product_id=product_id,
            show_form=show_form,
        )
        response = render(request, "needs/external_demands.html", context)
        return with_htmx_toast(response, toast_level, toast_msg)
    if toast_level == "error":
        messages.error(request, toast_msg)
    elif toast_level == "success":
        messages.success(request, toast_msg)
    else:
        messages.info(request, toast_msg)
    return redirect(build_external_demands_url(q=q, status=status, product_id=product_id, show_form=show_form))


@client_only_required
def needs_index_view(request):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    q, category_id, selected_need_id, requested_product_id, requested_quantity, show_need_form = get_needs_filters(request)
    edit_need_id = (request.GET.get("edit_need") or "").strip()
    context = build_needs_index_context(
        producer,
        q=q,
        category_id=category_id,
        selected_need_id=selected_need_id,
        need_prefill_product_id=requested_product_id,
        need_prefill_quantity=requested_quantity,
        show_need_form=show_need_form,
        edit_need_id=edit_need_id,
    )
    return render(request, "needs/index.html", context)


@client_only_required
def need_response_publish_view(request):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    requested_need_id = (
        request.POST.get("need_id")
        or request.POST.get("need")
        or request.GET.get("need")
        or ""
    )
    requested_need_id = str(requested_need_id).strip()
    requested_product_id = str(request.POST.get("product") or request.GET.get("product") or "").strip()
    if not requested_need_id:
        messages.error(request, "Necessidade não indicada.")
        return redirect("needs:index")

    try:
        need = (
            Need.objects
            .select_related("product", "producer", "producer__user", "product__category")
            .filter(
                id=requested_need_id,
                status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
                is_marketplace_published=True,
            )
            .first()
        )
    except (TypeError, ValueError, ValidationError):
        need = None
    if not need:
        messages.error(request, "Esta procura não está publicada ou já não está disponível para resposta.")
        return redirect("marketplace:index")
    if need.producer_id == producer.id:
        messages.error(request, "Não pode responder à sua própria necessidade.")
        return redirect(build_needs_index_url(selected_need_id=str(need.id)))
    if requested_product_id and str(need.product_id) != requested_product_id:
        messages.error(request, "Produto inválido para responder a esta necessidade.")
        return redirect("needs:index")

    form_initial = {}
    requested_quantity = (request.GET.get("qty") or request.GET.get("quantity") or "").strip()
    if request.method == "GET" and requested_quantity:
        form_initial["quantity"] = requested_quantity
    coverage = calculate_need_coverage(need)
    existing_response = get_active_need_response_for_responder(
        responder_producer=producer,
        need=need,
    )
    latest_response_summary = get_need_response_summaries_for_responder(
        responder_producer=producer,
        need_ids=[need.id],
    ).get(str(need.id))

    if existing_response and request.method == "GET":
        return redirect("needs:response_edit", listing_id=existing_response.id)

    if existing_response and request.method == "POST":
        form = NeedResponseEditForm(request.POST, listing=existing_response)
        if form.is_valid():
            try:
                listing = update_need_response(
                    listing=existing_response,
                    responder_producer=producer,
                    quantity=form.cleaned_data["quantity"],
                    unit_price=form.cleaned_data["unit_price"],
                    delivery_mode=form.cleaned_data["delivery_mode"],
                    delivery_radius_km=form.cleaned_data.get("delivery_radius_km"),
                    delivery_fee=form.cleaned_data.get("delivery_fee"),
                    notes=form.cleaned_data.get("notes"),
                    acting_user=request.current_user,
                )
            except ValidationError as exc:
                log_audit_event(
                    request=request,
                    action="LISTING_INVALID_ATTEMPT",
                    entity_type="marketplace_listings",
                    entity_id=existing_response.id,
                    notes=f"Edição de proposta privada recusada: {exc}",
                )
                form.add_error(None, str(exc))
            else:
                sync_alerts_after_need_change(producer, request.current_user)
                sync_alerts_after_need_change(need.producer, request.current_user)
                messages.success(request, "Proposta atualizada com sucesso.")
                return redirect("needs:response_detail", listing_id=listing.id)

        context = {
            "page_title": "Editar proposta",
            "header_kicker": "Editar proposta",
            "header_title": f"Proposta para {need.product.name}",
            "header_description": "Atualize a proposta privada que enviou para esta necessidade.",
            "form_title": "Editar proposta",
            "form_description": "Enquanto a proposta estiver pendente, pode ajustar quantidade, preço e condições.",
            "submit_label": "Guardar alterações",
            "submit_icon": "bi-check2",
            "is_need_response_edit": True,
            "form": form,
            "need": need,
            "coverage": coverage,
            "existing_response": existing_response,
            "responder_inventory": build_need_response_inventory_context(producer, need.product),
            "back_to_needs_url": build_needs_index_url(),
        }
        return render(request, "needs/respond.html", context)

    if latest_response_summary and not latest_response_summary.can_send_new_proposal:
        messages.info(
            request,
            "Já existe uma proposta enviada para esta necessidade que não permite nova proposta neste momento.",
        )
        return redirect("needs:response_detail", listing_id=latest_response_summary.listing_id)

    form = NeedResponsePublishForm(
        request.POST or None,
        producer=producer,
        need=need,
        initial=form_initial,
    )

    if request.method == "POST" and form.is_valid():
        try:
            listing = create_listing(
                producer=producer,
                product=need.product,
                quantity=form.cleaned_data["quantity"],
                unit_price=form.cleaned_data["unit_price"],
                delivery_mode=form.cleaned_data["delivery_mode"],
                delivery_radius_km=form.cleaned_data.get("delivery_radius_km"),
                delivery_fee=form.cleaned_data.get("delivery_fee"),
                show_location_on_map=True,
                notes=form.cleaned_data.get("notes"),
                photo_path=None,
                status=ListingStatus.ACTIVE,
                expires_at=None,
                listing_source=form.cleaned_data["listing_source"],
                forecast=form.cleaned_data.get("forecast"),
                need=need,
                acting_user=request.current_user,
            )
        except MarketplaceServiceError as exc:
            log_audit_event(
                request=request,
                action="LISTING_INVALID_ATTEMPT",
                entity_type="marketplace_listings",
                notes=f"Criação de proposta privada recusada: {exc}",
                new_values={
                    "need_id": str(need.id),
                    "product_id": str(need.product_id),
                    "quantity_total": str(form.cleaned_data["quantity"]),
                },
            )
            form.add_error(None, str(exc))
        else:
            sync_alerts_after_need_change(producer, request.current_user)
            sync_alerts_after_need_change(need.producer, request.current_user)
            messages.success(request, "Resposta enviada. A oferta ficou ligada à necessidade selecionada.")
            return redirect("needs:response_detail", listing_id=listing.id)

    context = {
        "page_title": "Responder a necessidade",
        "header_kicker": "Resposta a necessidade",
        "header_title": f"Oferta privada para {need.product.name}",
        "header_description": "Envie uma proposta diretamente para o produtor que anunciou esta necessidade.",
        "form_title": "Enviar proposta",
        "form_description": "A resposta fica privada e só pode ser comprada pelo produtor desta necessidade.",
        "submit_label": "Enviar proposta",
        "submit_icon": "bi-send",
        "is_need_response_edit": False,
        "form": form,
        "need": need,
        "coverage": coverage,
        "existing_response": existing_response,
        "responder_inventory": build_need_response_inventory_context(producer, need.product),
        "back_to_needs_url": build_needs_index_url(),
    }
    return render(request, "needs/respond.html", context)


@client_only_required
def need_create_view(request):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)

    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    source = request.POST if request.method == "POST" else request.GET
    source_context = (source.get("source_context") or "").strip().lower()
    source_system = (
        NeedSourceSystem.VISION4FARMS
        if source_context in {"recommendation", "vision4farms"}
        else NeedSourceSystem.MANUAL
    )
    external_id = (source.get("external_id") or "").strip() or None

    if request.method == "POST":
        form = NeedCreateForm(request.POST, producer=producer)
        if form.is_valid():
            try:
                need, _ = create_need(
                    producer=producer,
                    product=form.cleaned_data["product_id"],
                    required_quantity=form.cleaned_data["required_quantity"],
                    needed_by_date=form.cleaned_data.get("needed_by_date"),
                    source_system=source_system,
                    external_id=external_id,
                    notes=form.cleaned_data.get("notes"),
                    acting_user=request.current_user,
                )
            except DuplicateActiveNeedError as exc:
                messages.warning(
                    request,
                    "Já existe uma necessidade ativa para este produto. Abra-a para editar quantidade, data ou observações.",
                )
                return redirect(build_needs_index_url(selected_need_id=str(exc.existing_need.id)))
            except ValidationError as exc:
                form.add_error(None, _validation_error_text(exc))
            else:
                messages.success(request, "Necessidade anunciada com sucesso.")
                sync_alerts_after_need_change(producer, request.current_user)
                return redirect(build_needs_index_url(selected_need_id=str(need.id)))
    else:
        form_initial = {}
        requested_product_id = (request.GET.get("product") or request.GET.get("product_id") or "").strip()
        requested_quantity = (request.GET.get("qty") or request.GET.get("quantity") or "").strip()
        if requested_product_id:
            form_initial["product_id"] = requested_product_id
        if requested_quantity:
            form_initial["required_quantity"] = requested_quantity
        form = NeedCreateForm(producer=producer, initial=form_initial)

    context = {
        "page_title": "Publicar necessidade",
        "form": form,
        "back_to_needs_url": build_needs_index_url(),
        "source_context": source_context,
        "external_id": external_id or "",
    }
    return render(request, "needs/publish.html", context)


@client_only_required
def need_edit_view(request, need_id):
    if request.method != "POST":
        return redirect(build_needs_index_url(selected_need_id=str(need_id), edit_need_id=str(need_id)))

    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)

    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    q, category_id, _, requested_product_id, requested_quantity, show_need_form = get_needs_filters(request)
    requested_quantity = (request.POST.get("qty") or "").strip()
    need = get_need_for_producer(producer=producer, need_id=need_id)
    if not need:
        messages.error(request, "Necessidade não encontrada.")
        return redirect("needs:index")

    if need.source_system == NeedSourceSystem.CUSTOMER_DEMAND:
        messages.warning(
            request,
            "Esta procura é gerada automaticamente a partir dos pedidos de clientes. "
            "Para alterar a quantidade ou a data, edite os pedidos de origem.",
        )
        return redirect(f"{reverse('needs:external_demands')}?product={need.product_id}")

    form = NeedEditForm(request.POST, need=need)
    if form.is_valid():
        try:
            updated_need, _, changed = update_need(
                need=need,
                producer=producer,
                required_quantity=form.cleaned_data["required_quantity"],
                needed_by_date=form.cleaned_data.get("needed_by_date"),
                notes=form.cleaned_data.get("notes"),
            )
        except ValidationError as exc:
            form.add_error(None, str(exc))
        else:
            if changed:
                messages.success(request, "Necessidade atualizada com sucesso.")
                sync_alerts_after_need_change(producer, request.current_user)
            else:
                messages.info(request, "Não havia alterações para guardar.")

            next_url = build_needs_index_url(
                q=q,
                category_id=category_id,
                selected_need_id=str(updated_need.id),
                need_prefill_product_id=requested_product_id,
                need_prefill_quantity=requested_quantity,
                show_need_form=show_need_form,
            )
            if _is_htmx(request):
                context = build_needs_index_context(
                    producer,
                    q=q,
                    category_id=category_id,
                    selected_need_id=str(updated_need.id),
                    need_prefill_product_id=requested_product_id,
                    need_prefill_quantity=requested_quantity,
                    show_need_form=show_need_form,
                )
                return render(request, "needs/index.html", context)
            return redirect(next_url)

    if _is_htmx(request):
        context = build_needs_index_context(
            producer,
            q=q,
            category_id=category_id,
            selected_need_id=str(need.id),
            need_prefill_product_id=requested_product_id,
            need_prefill_quantity=requested_quantity,
            show_need_form=show_need_form,
            edit_need_id=str(need.id),
            need_edit_form=form,
        )
        return render(request, "needs/index.html", context)

    context = build_needs_index_context(
        producer,
        q=q,
        category_id=category_id,
        selected_need_id=str(need.id),
        need_prefill_product_id=requested_product_id,
        need_prefill_quantity=requested_quantity,
        show_need_form=show_need_form,
        edit_need_id=str(need.id),
        need_edit_form=form,
    )
    return render(request, "needs/index.html", context)


@client_only_required
def need_publish_marketplace_view(request, need_id):
    if request.method != "POST":
        return redirect(build_needs_index_url(selected_need_id=str(need_id)))

    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    need = get_need_for_producer(producer=producer, need_id=need_id)
    if not need:
        messages.error(request, "Procura não encontrada.")
        return redirect("needs:index")

    toast_level, toast_msg = "info", ""
    try:
        _, changed = publish_need_to_marketplace(
            need=need,
            producer=producer,
            acting_user=request.current_user,
        )
    except ValidationError as exc:
        toast_level, toast_msg = "error", str(exc)
        messages.error(request, str(exc))
    else:
        if changed:
            toast_level = "success"
            toast_msg = "Procura publicada no marketplace. Outros produtores já a podem ver."
            messages.success(request, toast_msg)
        else:
            toast_msg = "Esta procura já estava publicada no marketplace."
            messages.info(request, toast_msg)

    q, category_id, _, requested_product_id, requested_quantity, show_need_form = get_needs_filters(request)
    if _is_htmx(request):
        response = render(
            request,
            "needs/index.html",
            build_needs_index_context(
                producer,
                q=q,
                category_id=category_id,
                selected_need_id=str(need.id),
                need_prefill_product_id=requested_product_id,
                need_prefill_quantity=requested_quantity,
                show_need_form=show_need_form,
            ),
        )
        if toast_msg:
            with_htmx_toast(response, toast_level, toast_msg)
        return response
    return redirect(
        build_needs_index_url(
            q=q,
            category_id=category_id,
            selected_need_id=str(need.id),
            need_prefill_product_id=requested_product_id,
            need_prefill_quantity=requested_quantity,
            show_need_form=show_need_form,
        )
    )


@client_only_required
def need_withdraw_marketplace_view(request, need_id):
    if request.method != "POST":
        return redirect(build_needs_index_url(selected_need_id=str(need_id)))

    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    need = get_need_for_producer(producer=producer, need_id=need_id)
    if not need:
        messages.error(request, "Procura não encontrada.")
        return redirect("needs:index")

    toast_level, toast_msg = "info", ""
    try:
        _, changed = withdraw_need_from_marketplace(
            need=need,
            producer=producer,
            acting_user=request.current_user,
        )
    except ValidationError as exc:
        toast_level, toast_msg = "error", str(exc)
        messages.error(request, str(exc))
    else:
        if changed:
            toast_level = "success"
            toast_msg = "Procura retirada do marketplace."
            messages.success(request, toast_msg)
        else:
            toast_msg = "Esta procura já não estava publicada."
            messages.info(request, toast_msg)

    q, category_id, _, requested_product_id, requested_quantity, show_need_form = get_needs_filters(request)
    if _is_htmx(request):
        response = render(
            request,
            "needs/index.html",
            build_needs_index_context(
                producer,
                q=q,
                category_id=category_id,
                selected_need_id=str(need.id),
                need_prefill_product_id=requested_product_id,
                need_prefill_quantity=requested_quantity,
                show_need_form=show_need_form,
            ),
        )
        if toast_msg:
            with_htmx_toast(response, toast_level, toast_msg)
        return response
    return redirect(
        build_needs_index_url(
            q=q,
            category_id=category_id,
            selected_need_id=str(need.id),
            need_prefill_product_id=requested_product_id,
            need_prefill_quantity=requested_quantity,
            show_need_form=show_need_form,
        )
    )


@client_only_required
def need_ignore_view(request, need_id):
    if request.method != "POST":
        return redirect("needs:index")

    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)

    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    need = get_need_for_producer(producer=producer, need_id=need_id)
    if not need:
        messages.error(request, "Necessidade não encontrada.")
        return redirect("needs:index")

    previous_status = need.status
    try:
        changed = ignore_need(need=need, producer=producer)
    except ValidationError as exc:
        messages.error(request, str(exc))
    else:
        if changed:
            if previous_status == NeedStatus.COVERED:
                messages.success(request, "Necessidade removida da lista (soft delete).")
            else:
                messages.success(request, "Necessidade ignorada com sucesso.")
            sync_alerts_after_need_change(producer, request.current_user)
        else:
            messages.info(request, "A necessidade já estava ignorada.")

    q, category_id, selected_need_id, requested_product_id, requested_quantity, show_need_form = get_needs_filters(request)
    if _is_htmx(request):
        context = build_needs_index_context(
            producer,
            q=q,
            category_id=category_id,
            selected_need_id=selected_need_id,
            need_prefill_product_id=requested_product_id,
            need_prefill_quantity=requested_quantity,
            show_need_form=show_need_form,
        )
        return render(request, "needs/index.html", context)

    return redirect(
        build_needs_index_url(
            q=q,
            category_id=category_id,
            selected_need_id=selected_need_id,
            need_prefill_product_id=requested_product_id,
            need_prefill_quantity=requested_quantity,
            show_need_form=show_need_form,
        )
    )


@client_only_required
def need_response_edit_view(request, listing_id):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    listing = get_editable_need_response_for_responder(
        responder_producer=producer,
        listing_id=listing_id,
    )
    if not listing:
        messages.error(request, "Esta proposta já não pode ser editada.")
        return redirect("needs:index")

    need = listing.need
    form = NeedResponseEditForm(request.POST or None, listing=listing)
    coverage = calculate_need_coverage(need)

    if request.method == "POST" and form.is_valid():
        try:
            updated_listing = update_need_response(
                listing=listing,
                responder_producer=producer,
                quantity=form.cleaned_data["quantity"],
                unit_price=form.cleaned_data["unit_price"],
                delivery_mode=form.cleaned_data["delivery_mode"],
                delivery_radius_km=form.cleaned_data.get("delivery_radius_km"),
                delivery_fee=form.cleaned_data.get("delivery_fee"),
                notes=form.cleaned_data.get("notes"),
                acting_user=request.current_user,
            )
        except ValidationError as exc:
            log_audit_event(
                request=request,
                action="LISTING_INVALID_ATTEMPT",
                entity_type="marketplace_listings",
                entity_id=listing.id,
                notes=f"Edição de proposta privada recusada: {exc}",
            )
            form.add_error(None, str(exc))
        else:
            sync_alerts_after_need_change(producer, request.current_user)
            sync_alerts_after_need_change(need.producer, request.current_user)
            messages.success(request, "Proposta atualizada com sucesso.")
            return redirect("needs:response_detail", listing_id=updated_listing.id)

    context = {
        "page_title": "Editar proposta",
        "header_kicker": "Editar proposta",
        "header_title": f"Proposta para {need.product.name}",
        "header_description": "Atualize a proposta privada que enviou para esta necessidade.",
        "form_title": "Editar proposta",
        "form_description": "Enquanto a proposta estiver pendente, pode ajustar quantidade, preço e condições.",
        "submit_label": "Guardar alterações",
        "submit_icon": "bi-check2",
        "is_need_response_edit": True,
        "form": form,
        "need": need,
        "coverage": coverage,
        "existing_response": listing,
        "responder_inventory": build_need_response_inventory_context(producer, need.product),
        "back_to_needs_url": build_needs_index_url(),
    }
    return render(request, "needs/respond.html", context)


@client_only_required
def need_response_detail_view(request, listing_id):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    listing = get_need_response_listing_for_viewer(
        viewer_producer=producer,
        listing_id=listing_id,
    )
    if not listing:
        raise Http404("Resposta não encontrada.")

    response = build_need_response_for_listing(listing)
    is_need_owner = bool(listing.need and listing.need.producer_id == producer.id)
    is_responder = bool(listing.producer_id == producer.id)
    opened_from_marketplace = (request.GET.get("from") or "").strip().lower() == "marketplace"
    quantity_for_total = response.ordered_quantity if response.ordered_quantity > 0 else response.offered_quantity
    marketplace_proposals_url = f"{reverse('marketplace:index')}?tab=respostas"
    back_url = (
        marketplace_proposals_url
        if is_responder or opened_from_marketplace
        else build_needs_index_url(
            selected_need_id=str(listing.need_id) if is_need_owner else "",
        )
    )
    context = {
        "page_title": "Oferta para necessidade",
        "listing": listing,
        "need": listing.need,
        "response": response,
        "response_total_amount": quantity_for_total * response.unit_price,
        "is_need_owner": is_need_owner,
        "is_responder": is_responder,
        "delivery_text": build_delivery_text(listing),
        "purchase_url": reverse("orders:create_from_listing", kwargs={"listing_id": listing.id}),
        "back_to_needs_url": back_url,
        "back_label": "Voltar às propostas" if is_responder or opened_from_marketplace else "Voltar às necessidades",
    }
    return render(request, "needs/response_detail.html", context)


@client_only_required
def need_response_reject_view(request, listing_id):
    if request.method != "POST":
        return redirect("needs:response_detail", listing_id=listing_id)

    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    listing = get_need_response_listing_for_viewer(
        viewer_producer=producer,
        listing_id=listing_id,
    )
    if not listing:
        raise Http404("Resposta não encontrada.")
    next_url = (request.POST.get("next") or "").strip()

    try:
        changed = reject_need_response(
            listing=listing,
            owner_producer=producer,
            acting_user=request.current_user,
        )
    except ValidationError as exc:
        messages.error(request, str(exc))
    else:
        if changed:
            messages.success(request, "Oferta rejeitada. A resposta deixou de estar disponível para compra.")
        else:
            messages.info(request, "Esta oferta já estava rejeitada.")

    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect("needs:response_detail", listing_id=listing_id)
