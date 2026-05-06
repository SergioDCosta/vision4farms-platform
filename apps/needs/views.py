from datetime import datetime, time
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.common.decorators import client_only_required, login_required
from apps.marketplace.services import (
    build_delivery_text,
    expire_due_active_listings,
    get_current_producer_for_user,
)
from apps.needs.navigation import build_needs_index_url
from apps.needs.models import NeedSourceSystem, NeedStatus
from apps.needs.services import (
    calculate_need_coverage,
    create_or_update_need,
    build_need_response_for_listing,
    get_critical_stock_product_ids,
    get_need_response_listing_for_viewer,
    get_need_candidate_products,
    get_need_for_producer,
    get_need_response_counts_for_owner,
    ignore_need,
    list_need_responses_for_owner,
    list_marketplace_my_needs,
    list_marketplace_public_needs,
    reject_need_response,
)


def sync_alerts_after_need_change(producer, acting_user):
    try:
        from apps.alerts.services import sync_alerts_for_producer
        sync_alerts_for_producer(producer, acting_user=acting_user)
    except Exception:
        return


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def parse_need_datetime(value):
    raw_value = (value or "").strip()
    if not raw_value:
        return None
    try:
        if "T" in raw_value:
            parsed = datetime.strptime(raw_value, "%Y-%m-%dT%H:%M")
        else:
            parsed_date = datetime.strptime(raw_value, "%Y-%m-%d").date()
            parsed = datetime.combine(parsed_date, time.max)
    except ValueError:
        raise ValidationError("Data limite inválida para a necessidade.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def build_selected_need_row(need):
    coverage = calculate_need_coverage(need)
    return {
        "need": need,
        "status": need.status,
        "status_label": need.get_status_display(),
        "required_quantity": coverage["required_quantity"],
        "planned_qty": coverage["planned_qty"],
        "completed_qty": coverage["completed_qty"],
        "remaining_to_plan": coverage["remaining_to_plan"],
        "remaining_to_receive": coverage["remaining_to_receive"],
    }


def get_needs_filters(request):
    source = request.POST if request.method == "POST" else request.GET
    q = (source.get("q") or "").strip()
    category_id = (source.get("category") or "").strip()
    need_id = (source.get("need") or "").strip()
    requested_product_id = (source.get("product") or source.get("product_id") or "").strip()
    requested_quantity = (source.get("qty") or source.get("required_quantity") or "").strip()
    show_need_form = (source.get("show_need_form") or "").strip().lower() in {"1", "true", "yes", "on"}
    return q, category_id, need_id, requested_product_id, requested_quantity, show_need_form


def build_needs_index_context(
    producer,
    *,
    q,
    category_id,
    selected_need_id="",
    need_prefill_product_id="",
    need_prefill_quantity="",
    show_need_form=False,
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

    if not selected_need_row and need_my_rows:
        selected_need_row = need_my_rows[0]
        validated_need_id = str(selected_need_row["need"].id)
        if not need_prefill_product_id:
            need_prefill_product_id = str(selected_need_row["need"].product_id)
        if not need_prefill_quantity:
            need_prefill_quantity = str(selected_need_row["remaining_to_plan"])

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
    past_need_response_rows = [
        response for response in need_response_rows
        if response.response_status != "PENDING"
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
    all_past_need_response_rows = [
        response for response in all_need_response_rows
        if response.response_status != "PENDING"
    ]

    return {
        "page_title": "Necessidades",
        "q": q,
        "selected_category_id": category_id,
        "need_public_rows": need_public_rows,
        "need_my_rows": need_my_rows,
        "need_products": need_products,
        "need_response_rows": need_response_rows,
        "active_need_response_rows": active_need_response_rows,
        "past_need_response_rows": past_need_response_rows,
        "all_past_need_response_rows": all_past_need_response_rows,
        "selected_need_id": validated_need_id,
        "selected_need_row": selected_need_row,
        "need_prefill_product_id": need_prefill_product_id,
        "need_prefill_quantity": need_prefill_quantity,
        "show_need_form": bool(show_need_form),
        "available_categories": available_categories,
        "can_publish": bool(producer),
        "current_url": build_needs_index_url(
            q=q,
            category_id=category_id,
            selected_need_id=validated_need_id,
            need_prefill_product_id=need_prefill_product_id,
            need_prefill_quantity=need_prefill_quantity,
            show_need_form=show_need_form,
        ),
    }


@login_required
@client_only_required
def needs_index_view(request):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    expire_due_active_listings()
    q, category_id, selected_need_id, requested_product_id, requested_quantity, show_need_form = get_needs_filters(request)
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


@login_required
@client_only_required
def need_create_view(request):
    if request.method != "POST":
        return redirect("needs:index")

    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)

    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    q, category_id, selected_need_id, requested_product_id, requested_quantity, _ = get_needs_filters(request)
    show_need_form = True
    created = False

    product_id = (request.POST.get("product_id") or "").strip()
    required_quantity_raw = (request.POST.get("required_quantity") or "").strip()
    notes = (request.POST.get("notes") or "").strip()
    source_context = (request.POST.get("source_context") or "").strip().lower()
    source_system = (
        NeedSourceSystem.VISION4FARMS
        if source_context in {"recommendation", "vision4farms"}
        else NeedSourceSystem.MANUAL
    )
    external_id = (request.POST.get("external_id") or "").strip() or None

    required_quantity = None
    try:
        required_quantity = Decimal(required_quantity_raw)
    except Exception:
        messages.error(request, "Quantidade necessária inválida.")

    product = None
    if required_quantity is not None:
        product = get_need_candidate_products(producer).filter(id=product_id).first()
        if not product:
            messages.error(request, "Produto inválido para criar necessidade.")

    if required_quantity is not None and product is not None:
        try:
            needed_by_date = parse_need_datetime(request.POST.get("needed_by_date"))
            _, _, created = create_or_update_need(
                producer=producer,
                product=product,
                required_quantity=required_quantity,
                needed_by_date=needed_by_date,
                source_system=source_system,
                external_id=external_id,
                notes=notes or None,
            )
        except ValidationError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                "Necessidade anunciada com sucesso."
                if created
                else "Necessidade existente atualizada com sucesso.",
            )
            sync_alerts_after_need_change(producer, request.current_user)
            show_need_form = False

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


@login_required
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


@login_required
@client_only_required
def need_response_detail_view(request, listing_id):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    expire_due_active_listings()
    listing = get_need_response_listing_for_viewer(
        viewer_producer=producer,
        listing_id=listing_id,
    )
    if not listing:
        raise Http404("Resposta não encontrada.")

    response = build_need_response_for_listing(listing)
    is_need_owner = bool(listing.need and listing.need.producer_id == producer.id)
    is_responder = bool(listing.producer_id == producer.id)
    context = {
        "page_title": "Oferta para necessidade",
        "listing": listing,
        "need": listing.need,
        "response": response,
        "is_need_owner": is_need_owner,
        "is_responder": is_responder,
        "delivery_text": build_delivery_text(listing),
        "purchase_url": reverse("orders:create_from_listing", kwargs={"listing_id": listing.id}),
        "back_to_needs_url": build_needs_index_url(
            selected_need_id=str(listing.need_id) if is_need_owner else "",
        ),
    }
    return render(request, "needs/response_detail.html", context)


@login_required
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
        changed = reject_need_response(listing=listing, owner_producer=producer)
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
