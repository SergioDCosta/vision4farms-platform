from django.urls import reverse
from urllib.parse import urlencode
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.common.decorators import client_only_required
from apps.common.htmx import with_htmx_toast
from apps.inventory.models import ProducerProfile, Stock
from apps.needs.navigation import build_needs_index_url
from apps.needs.models import NeedSourceSystem
from apps.needs.services import (
    DuplicateActiveNeedError,
    create_need,
    list_marketplace_public_needs,
    update_need,
)
from apps.orders.services import (
    OrderServiceError,
    create_order_from_recommendation,
    is_order_forecast_only,
)
from apps.recommendations.forms import RecommendationRequestForm
from apps.recommendations.models import Recommendation
from apps.recommendations.services import (
    RecommendationGenerationError,
    RECOMMENDATION_DIRECTION_BALANCED,
    RECOMMENDATION_DIRECTION_BUY,
    RECOMMENDATION_DIRECTION_SELL,
    accept_recommendation,
    build_recommendation_inventory_rows,
    calculate_current_deficit,
    generate_recommendation,
    get_market_alternative_listings,
    get_producer_products,
    get_recommendation_totals,
    get_selected_items,
)


ZERO_QTY = Decimal("0.000")


def _sync_alerts_after_need_change(producer, acting_user):
    try:
        from apps.alerts.services import sync_alerts_for_producer
        sync_alerts_for_producer(producer, acting_user=acting_user)
    except Exception:
        return


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _get_current_producer(request):
    user = getattr(request, "current_user", None)
    if not user:
        return None
    return ProducerProfile.objects.filter(user=user).first()


def _render_wizard(request, context):
    return render(request, "recommendations/partials/wizard.html", context)


def _get_form_products(producer):
    products = list(get_producer_products(producer))
    if not products:
        return products

    product_ids = [product.id for product in products]
    stock_rows = Stock.objects.filter(
        producer=producer,
        product_id__in=product_ids,
    ).values_list("product_id", "current_quantity", "reserved_quantity", "safety_stock")

    # Sem registo de stock assume crítico enquanto não houver dados reais de inventário.
    critical_product_ids = {str(product_id) for product_id in product_ids}
    surplus_product_ids = set()

    for product_id, current_quantity, reserved_quantity, safety_stock in stock_rows:
        current_qty = Decimal(str(current_quantity or 0))
        reserved_qty = Decimal(str(reserved_quantity or 0))
        minimum_qty = Decimal(str(safety_stock or 0))
        available_qty = current_qty - reserved_qty
        warning_upper_qty = minimum_qty + (minimum_qty * Decimal("0.10"))
        if available_qty >= minimum_qty:
            critical_product_ids.discard(str(product_id))
        if available_qty > minimum_qty and (minimum_qty <= 0 or available_qty > warning_upper_qty):
            surplus_product_ids.add(str(product_id))

    for product in products:
        product.is_critical_stock = str(product.id) in critical_product_ids
        product.is_surplus_stock = str(product.id) in surplus_product_ids

    return products


def _recommendation_ui_meta(metrics):
    direction = metrics.get("recommendation_direction") or RECOMMENDATION_DIRECTION_BALANCED
    if direction == RECOMMENDATION_DIRECTION_BUY:
        return {
            "recommendation_direction": direction,
            "quantity_label": "Quantidade a comprar",
            "quantity_help": "Valor sugerido para repor o stock disponível até ao stock de segurança. Podes ajustar manualmente antes de gerar.",
            "suggested_quantity_label": "Sugestão de compra",
            "suggested_quantity_hint": "Valor sugerido para repor o stock disponível até ao stock de segurança.",
            "submit_label": "Gerar recomendação de compra",
            "can_generate": True,
        }
    if direction == RECOMMENDATION_DIRECTION_SELL:
        return {
            "recommendation_direction": direction,
            "quantity_label": "Quantidade a vender",
            "quantity_help": "Valor sugerido para vender apenas o excedente acima do stock de segurança.",
            "suggested_quantity_label": "Sugestão de venda",
            "suggested_quantity_hint": "Excedente disponível acima do stock de segurança.",
            "submit_label": "Gerar recomendação de venda",
            "can_generate": True,
        }
    return {
        "recommendation_direction": direction,
        "quantity_label": "Quantidade sugerida",
        "quantity_help": "O stock disponível está alinhado com o stock de segurança.",
        "suggested_quantity_label": "Stock equilibrado",
        "suggested_quantity_hint": "Não há compra nem venda recomendada neste momento.",
        "submit_label": "Sem ação recomendada",
        "can_generate": False,
    }


def _empty_recommendation_metrics():
    return {
        "safety_stock": ZERO_QTY,
        "reserved_quantity": ZERO_QTY,
        "current_stock": ZERO_QTY,
        "available_stock": ZERO_QTY,
        "deficit_quantity": ZERO_QTY,
        "buy_quantity": ZERO_QTY,
        "sell_quantity": ZERO_QTY,
        "suggested_quantity": ZERO_QTY,
        "recommendation_direction": RECOMMENDATION_DIRECTION_BALANCED,
    }


def _metrics_from_inventory_row(row):
    if not row:
        return _empty_recommendation_metrics()
    return {
        "safety_stock": row["safety_stock"],
        "reserved_quantity": row["reserved_quantity"],
        "current_stock": row["current_stock"],
        "available_stock": row["available_stock"],
        "deficit_quantity": row["buy_quantity"],
        "buy_quantity": row["buy_quantity"],
        "sell_quantity": row["sell_quantity"],
        "suggested_quantity": row["suggested_quantity"],
        "recommendation_direction": row["recommendation_direction"],
    }


def _find_inventory_row(inventory_rows, product_id):
    if not product_id:
        return None
    for row in inventory_rows.get("all_rows", []):
        if row["product_id"] == str(product_id):
            return row
    return None


def _pick_default_inventory_row(inventory_rows):
    if inventory_rows.get("buy_rows"):
        return inventory_rows["buy_rows"][0]
    if inventory_rows.get("sell_rows"):
        return inventory_rows["sell_rows"][0]
    if inventory_rows.get("balanced_rows"):
        return inventory_rows["balanced_rows"][0]
    return None


def _build_step_1_context(
    *,
    form,
    wizard_step=1,
    errors=None,
    initial_deficit_quantity="0",
    initial_current_quantity="0",
    metrics=None,
    inventory_rows=None,
):
    metrics = metrics or _empty_recommendation_metrics()
    inventory_rows = inventory_rows or {
        "buy_rows": [],
        "sell_rows": [],
        "balanced_rows": [],
        "all_rows": [],
    }
    ui_meta = _recommendation_ui_meta(metrics)
    return {
        "wizard_step": wizard_step,
        "products": form.fields["product_id"].choices[1:],
        "recommendation_buy_rows": inventory_rows["buy_rows"],
        "recommendation_sell_rows": inventory_rows["sell_rows"],
        "recommendation_balanced_rows": inventory_rows["balanced_rows"],
        "errors": errors or {},
        "initial_product_id": form.initial.get("product_id", ""),
        "initial_deficit_quantity": initial_deficit_quantity,
        "initial_current_quantity": initial_current_quantity,
        "initial_requested_quantity": form.initial.get("requested_quantity", ""),
        "recommendation_metrics": metrics,
        **ui_meta,
    }


def _remaining_deficit_from_recommendation(recommendation):
    deficit = recommendation.deficit_quantity
    if deficit is None:
        return Decimal("0.000")
    return max(Decimal(str(deficit)), Decimal("0.000"))


def _build_step_2_context(
    recommendation,
    *,
    market_options_expanded=False,
    alternative_items=None,
    need_prompt=None,
):
    totals = get_recommendation_totals(recommendation)
    remaining_deficit = _remaining_deficit_from_recommendation(recommendation)
    return {
        "wizard_step": 2,
        "recommendation": recommendation,
        "selected_items": totals["items"],
        "selected_total_quantity": totals["selected_total_quantity"],
        "remaining_deficit": remaining_deficit,
        "market_options_expanded": market_options_expanded,
        "alternative_items": alternative_items or [],
        "can_accept": len(totals["items"]) > 0,
        "need_prompt": need_prompt,
    }


def _need_response_url(need_id, product_id, quantity=None):
    query = {
        "from": "need",
        "need": str(need_id),
        "product": str(product_id),
    }
    if quantity is not None:
        query["qty"] = str(quantity)
    return f"{reverse('needs:respond')}?{urlencode(query)}"


def _build_sell_recommendation_context(*, producer, product, requested_quantity, metrics):
    compatible_needs = []
    for row in list_marketplace_public_needs(viewer_producer=producer):
        if str(row["need"].product_id) != str(product.id):
            continue
        response_quantity = min(
            Decimal(str(row.get("public_quantity") or 0)),
            Decimal(str(requested_quantity or 0)),
        )
        row["response_url"] = _need_response_url(row["need"].id, product.id, response_quantity)
        compatible_needs.append(row)

    publish_query = urlencode({
        "source": "stock",
        "product": str(product.id),
        "from": "recommendations",
        "qty": str(requested_quantity),
    })
    return {
        "wizard_step": 2,
        "recommendation_direction": RECOMMENDATION_DIRECTION_SELL,
        "product": product,
        "requested_quantity": requested_quantity,
        "recommendation_metrics": metrics,
        "compatible_need_rows": compatible_needs[:6],
        "publish_url": f"{reverse('marketplace:publish')}?{publish_query}",
    }


@client_only_required
def recommendations_index_view(request):
    producer = _get_current_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    form_products = _get_form_products(producer)
    inventory_rows = build_recommendation_inventory_rows(producer, form_products)
    requested_product_id = (request.GET.get("product") or "").strip()
    selected_row = _find_inventory_row(inventory_rows, requested_product_id) or _pick_default_inventory_row(inventory_rows)
    selected_product = selected_row["product"] if selected_row else None

    form_initial = {}
    initial_deficit_quantity = "0"
    initial_current_quantity = "0"

    if selected_row:
        deficit_data = _metrics_from_inventory_row(selected_row)
        form_initial["product_id"] = str(selected_product.id)
        form_initial["requested_quantity"] = deficit_data["suggested_quantity"]
        initial_deficit_quantity = deficit_data["deficit_quantity"]
        initial_current_quantity = deficit_data["current_stock"]
    else:
        deficit_data = _empty_recommendation_metrics()

    form = RecommendationRequestForm(products=form_products, initial=form_initial)

    context = _build_step_1_context(
        form=form,
        initial_deficit_quantity=initial_deficit_quantity,
        initial_current_quantity=initial_current_quantity,
        metrics=deficit_data,
        inventory_rows=inventory_rows,
    )
    return render(request, "recommendations/index.html", context)


@client_only_required
def recommendations_product_metrics_view(request):
    producer = _get_current_producer(request)
    if not producer:
        return HttpResponse("")

    product_id = (request.GET.get("product_id") or "").strip()
    if not product_id:
        metrics = _empty_recommendation_metrics()
        context = {
            "initial_deficit_quantity": "0",
            "initial_current_quantity": "0",
            "initial_requested_quantity": "0",
            "recommendation_metrics": metrics,
            **_recommendation_ui_meta(metrics),
        }
        return render(request, "recommendations/partials/step_1_metrics.html", context)

    form_products = _get_form_products(producer)
    product = next((p for p in form_products if str(p.id) == product_id), None)

    if not product:
        metrics = _empty_recommendation_metrics()
        context = {
            "initial_deficit_quantity": "0",
            "initial_current_quantity": "0",
            "initial_requested_quantity": "0",
            "recommendation_metrics": metrics,
            **_recommendation_ui_meta(metrics),
        }
        return render(request, "recommendations/partials/step_1_metrics.html", context)

    deficit_data = calculate_current_deficit(producer, product)

    context = {
        "initial_deficit_quantity": deficit_data["deficit_quantity"],
        "initial_current_quantity": deficit_data["current_stock"],
        "initial_requested_quantity": deficit_data["suggested_quantity"],
        "recommendation_metrics": deficit_data,
        **_recommendation_ui_meta(deficit_data),
    }
    return render(request, "recommendations/partials/step_1_metrics.html", context)


@client_only_required
def recommendations_generate_view(request):
    if request.method != "POST":
        return redirect("recommendations:index")

    producer = _get_current_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    form_products = _get_form_products(producer)
    inventory_rows = build_recommendation_inventory_rows(producer, form_products)
    form = RecommendationRequestForm(request.POST, products=form_products)

    product = None
    product_id = (request.POST.get("product_id") or "").strip()
    if product_id:
        product = next((p for p in form_products if str(p.id) == product_id), None)

    if not form.is_valid() or not product:
        deficit_quantity = "0"
        current_quantity = "0"

        if product:
            deficit_data = calculate_current_deficit(producer, product)
            deficit_quantity = deficit_data["deficit_quantity"]
            current_quantity = deficit_data["current_stock"]
        else:
            deficit_data = _empty_recommendation_metrics()

        initial_requested_quantity = request.POST.get("requested_quantity", "")

        form.initial.update({
            "product_id": product_id,
            "requested_quantity": initial_requested_quantity,
        })

        context = _build_step_1_context(
            form=form,
            errors={k: v[0] for k, v in form.errors.items()},
            initial_deficit_quantity=deficit_quantity,
            initial_current_quantity=current_quantity,
            metrics=deficit_data,
            inventory_rows=inventory_rows,
        )
        return _render_wizard(request, context)

    requested_quantity = form.cleaned_data["requested_quantity"]
    deficit_data = calculate_current_deficit(producer, product)
    direction = deficit_data["recommendation_direction"]

    if direction == RECOMMENDATION_DIRECTION_BALANCED:
        form.initial.update({
            "product_id": str(product.id),
            "requested_quantity": requested_quantity,
        })
        context = _build_step_1_context(
            form=form,
            errors={"requested_quantity": "O stock está equilibrado. Não há compra nem venda recomendada para este produto."},
            initial_deficit_quantity=deficit_data["deficit_quantity"],
            initial_current_quantity=deficit_data["current_stock"],
            metrics=deficit_data,
            inventory_rows=inventory_rows,
        )
        return _render_wizard(request, context)

    if requested_quantity <= 0:
        form.initial.update({
            "product_id": str(product.id),
            "requested_quantity": requested_quantity,
        })
        context = _build_step_1_context(
            form=form,
            errors={"requested_quantity": "A quantidade deve ser superior a zero."},
            initial_deficit_quantity=deficit_data["deficit_quantity"],
            initial_current_quantity=deficit_data["current_stock"],
            metrics=deficit_data,
            inventory_rows=inventory_rows,
        )
        return _render_wizard(request, context)

    if direction == RECOMMENDATION_DIRECTION_SELL:
        context = _build_sell_recommendation_context(
            producer=producer,
            product=product,
            requested_quantity=requested_quantity,
            metrics=deficit_data,
        )
        return _render_wizard(request, context)

    try:
        recommendation = generate_recommendation(
            producer=producer,
            product=product,
            requested_quantity=requested_quantity,
            deadline_date=None,
        )
    except RecommendationGenerationError as exc:
        form.initial.update({
            "product_id": str(product.id),
            "requested_quantity": requested_quantity,
        })
        context = _build_step_1_context(
            form=form,
            errors={"requested_quantity": str(exc)},
            initial_deficit_quantity=deficit_data["deficit_quantity"],
            initial_current_quantity=deficit_data["current_stock"],
            metrics=deficit_data,
            inventory_rows=inventory_rows,
        )
        return _render_wizard(request, context)

    context = _build_step_2_context(recommendation)
    return _render_wizard(request, context)


@client_only_required
def recommendations_back_to_need_view(request, recommendation_id):
    producer = _get_current_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    recommendation = get_object_or_404(
        Recommendation.objects.select_related("product", "producer"),
        id=recommendation_id,
        producer=producer,
    )

    deficit_data = calculate_current_deficit(producer, recommendation.product)
    form_products = _get_form_products(producer)
    inventory_rows = build_recommendation_inventory_rows(producer, form_products)

    form = RecommendationRequestForm(
        products=form_products,
        initial={
            "product_id": str(recommendation.product.id),
            "requested_quantity": recommendation.requested_quantity,
        },
    )

    context = _build_step_1_context(
        form=form,
        initial_deficit_quantity=deficit_data["deficit_quantity"],
        initial_current_quantity=deficit_data["current_stock"],
        metrics=deficit_data,
        inventory_rows=inventory_rows,
    )
    return _render_wizard(request, context)


@client_only_required
def recommendations_create_need_view(request, recommendation_id):
    if request.method != "POST":
        return redirect("recommendations:index")

    producer = _get_current_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    recommendation = get_object_or_404(
        Recommendation.objects.select_related("product", "producer"),
        id=recommendation_id,
        producer=producer,
    )

    remaining_deficit = _remaining_deficit_from_recommendation(recommendation)
    context = _build_step_2_context(recommendation)

    if remaining_deficit <= 0:
        response = _render_wizard(request, context)
        return with_htmx_toast(
            response,
            "info",
            "Não existe défice remanescente para anunciar como necessidade.",
        )

    created = False
    try:
        if recommendation.need_id:
            need, _, _ = update_need(
                need=recommendation.need,
                producer=producer,
                required_quantity=remaining_deficit,
                needed_by_date=recommendation.deadline_date,
                notes=f"Necessidade atualizada a partir da recomendação #{recommendation.id}.",
            )
        else:
            need, _ = create_need(
                producer=producer,
                product=recommendation.product,
                required_quantity=remaining_deficit,
                source_system=NeedSourceSystem.VISION4FARMS,
                external_id=str(recommendation.id),
                notes=f"Necessidade criada a partir da recomendação #{recommendation.id}.",
            )
            recommendation.need = need
            recommendation.updated_at = timezone.now()
            recommendation.save(update_fields=["need", "updated_at"])
            created = True
    except DuplicateActiveNeedError as exc:
        need_url = build_needs_index_url(selected_need_id=str(exc.existing_need.id))
        updated_context = _build_step_2_context(
            recommendation,
            need_prompt={
                "title": "Necessidade já existente",
                "message": "Já existe uma necessidade ativa para este produto. Abra essa necessidade para rever ou editar o pedido.",
                "url": need_url,
                "product_name": recommendation.product.name,
                "quantity": remaining_deficit,
                "unit": recommendation.product.unit,
            },
        )
        response = _render_wizard(request, updated_context)
        return with_htmx_toast(response, "warning", "Já existe uma necessidade ativa para este produto.")
    except ValidationError as exc:
        response = _render_wizard(request, context)
        return with_htmx_toast(response, "error", str(exc))

    _sync_alerts_after_need_change(producer, request.current_user)

    need_url = build_needs_index_url(
        selected_need_id=str(need.id),
    )
    updated_context = _build_step_2_context(
        recommendation,
        need_prompt={
            "title": "Necessidade criada" if created else "Necessidade atualizada",
            "message": (
                "A necessidade foi anunciada com o défice que ficou por cobrir. Quer abrir agora o detalhe desse pedido?"
                if created
                else "A necessidade associada foi atualizada com o défice mais recente. Quer abrir agora o detalhe desse pedido?"
            ),
            "url": need_url,
            "product_name": recommendation.product.name,
            "quantity": remaining_deficit,
            "unit": recommendation.product.unit,
        },
    )
    response = _render_wizard(request, updated_context)
    return with_htmx_toast(
        response,
        "success",
        "Necessidade anunciada com sucesso."
        if created
        else "Necessidade existente atualizada com o novo défice.",
    )


@client_only_required
def recommendations_prepare_confirm_view(request, recommendation_id):
    if request.method != "GET":
        return redirect("recommendations:index")

    producer = _get_current_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    recommendation = get_object_or_404(
        Recommendation.objects.select_related("product", "producer"),
        id=recommendation_id,
        producer=producer,
    )

    totals = get_recommendation_totals(recommendation)
    can_accept = len(totals["items"]) > 0

    context = {
        "wizard_step": 3,
        "recommendation": recommendation,
        "selected_items": totals["items"],
        "selected_total_quantity": totals["selected_total_quantity"],
        "selected_total_amount": totals["selected_total_amount"],
        "can_accept": can_accept,
    }
    return _render_wizard(request, context)


@client_only_required
def recommendations_accept_view(request, recommendation_id):
    if request.method != "POST":
        return redirect("recommendations:index")

    producer = _get_current_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    recommendation = get_object_or_404(
        Recommendation.objects.select_related("product", "producer"),
        id=recommendation_id,
        producer=producer,
    )

    selected_items = get_selected_items(recommendation)
    if not selected_items.exists():
        totals = get_recommendation_totals(recommendation)
        context = {
            "wizard_step": 3,
            "recommendation": recommendation,
            "selected_items": totals["items"],
            "selected_total_quantity": totals["selected_total_quantity"],
            "selected_total_amount": totals["selected_total_amount"],
            "can_accept": False,
        }
        response = _render_wizard(request, context)
        return with_htmx_toast(
            response,
            "warning",
            "Não existem linhas de recomendação para confirmar.",
        )

    try:
        order_group, created_orders = create_order_from_recommendation(
            buyer_producer=producer,
            recommendation=recommendation,
            acting_user=request.current_user,
        )
    except OrderServiceError as exc:
        totals = get_recommendation_totals(recommendation)
        context = {
            "wizard_step": 3,
            "recommendation": recommendation,
            "selected_items": totals["items"],
            "selected_total_quantity": totals["selected_total_quantity"],
            "selected_total_amount": totals["selected_total_amount"],
            "can_accept": True,
        }
        response = _render_wizard(request, context)
        return with_htmx_toast(
            response,
            "error",
            str(exc),
        )

    redirect_url = reverse("orders:group_detail", args=[order_group.id])
    if created_orders and all(is_order_forecast_only(order) for order in created_orders):
        redirect_url = f"{reverse('orders:index')}?tab=pre_vendas"

    if _is_htmx(request):
        response = HttpResponse("")
        response["HX-Redirect"] = redirect_url
        return response

    messages.success(request, f"Foram criadas {len(created_orders)} encomenda(s) no grupo #{order_group.group_number}.")
    return redirect(redirect_url)


@client_only_required
def recommendations_market_options_view(request, recommendation_id):
    producer = _get_current_producer(request)
    if not producer:
        return HttpResponse("")

    recommendation = get_object_or_404(
        Recommendation.objects.select_related("product", "producer"),
        id=recommendation_id,
        producer=producer,
    )

    expanded = str(request.GET.get("expanded", "0")).lower() in {"1", "true", "yes", "on"}
    alternative_items = get_market_alternative_listings(recommendation) if expanded else []

    context = {
        "recommendation": recommendation,
        "market_options_expanded": expanded,
        "alternative_items": alternative_items,
    }
    return render(request, "recommendations/partials/step_2_market_toggle.html", context)


@client_only_required
def recommendations_replace_item_view(request, recommendation_id):
    producer = _get_current_producer(request)
    if not producer:
        return HttpResponse("")

    recommendation = get_object_or_404(
        Recommendation.objects.select_related("product", "producer"),
        id=recommendation_id,
        producer=producer,
    )

    context = _build_step_2_context(recommendation)
    response = _render_wizard(request, context)
    return with_htmx_toast(
        response,
        "info",
        "A substituição manual de produtores fica para a próxima versão.",
    )

