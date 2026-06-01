from django.urls import reverse
from urllib.parse import urlencode
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.common.decorators import client_only_required
from apps.common.htmx import is_htmx_request as _is_htmx, with_htmx_toast
from apps.inventory.models import ProducerProfile, Stock
from apps.inventory.services import calculate_inventory_commitment_state
from apps.marketplace.models import ListingStatus
from apps.marketplace.services import (
    get_forecast_available_quantity,
    get_marketplace_eligible_forecasts,
    get_max_publishable_quantity,
    get_my_listings,
)
from apps.needs.navigation import build_needs_index_url
from apps.needs.models import Need, NeedSourceSystem, NeedStatus
from apps.needs.services import (
    DuplicateActiveNeedError,
    create_need,
    list_marketplace_public_needs,
    publish_need_to_marketplace,
    sync_need_from_external_demands,
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
    stock_by_product_id = {
        stock.product_id: stock
        for stock in Stock.objects.filter(
            producer=producer,
            product_id__in=product_ids,
        )
    }

    # Sem registo de stock assume crítico enquanto não houver dados reais de inventário.
    critical_product_ids = {str(product_id) for product_id in product_ids}
    surplus_product_ids = set()

    for product in products:
        commitment_state = calculate_inventory_commitment_state(
            producer,
            product,
            stock=stock_by_product_id.get(product.id),
        )
        product_id = str(product.id)
        if commitment_state.get("state_key") != "critical":
            critical_product_ids.discard(product_id)
        if commitment_state.get("state_key") == "excess":
            surplus_product_ids.add(product_id)

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
            "quantity_help": "Valor sugerido para cumprir pedidos externos em aberto. Podes ajustar manualmente antes de gerar.",
            "suggested_quantity_label": "Sugestão de compra",
            "suggested_quantity_hint": "Quantidade em falta para cumprir pedidos externos.",
            "submit_label": "Gerar recomendação de compra",
            "can_generate": True,
        }
    if direction == RECOMMENDATION_DIRECTION_SELL:
        return {
            "recommendation_direction": direction,
            "quantity_label": "Quantidade a vender",
            "quantity_help": "Valor sugerido para vender apenas o excedente acima dos compromissos externos.",
            "suggested_quantity_label": "Sugestão de venda",
            "suggested_quantity_hint": "Excedente disponível acima dos compromissos externos.",
            "submit_label": "Gerar recomendação de venda",
            "can_generate": True,
        }
    return {
        "recommendation_direction": direction,
        "quantity_label": "Quantidade sugerida",
        "quantity_help": "O stock disponível está alinhado com os compromissos externos.",
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
        "physical_deficit_quantity": ZERO_QTY,
        "planned_need_quantity": ZERO_QTY,
        "useful_forecast_total": ZERO_QTY,
        "first_deficit_date": None,
        "deadline_date": None,
        "customer_demand_need": None,
        "customer_demand_need_published": False,
        "has_customer_demand_deficit": False,
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
        "physical_deficit_quantity": row.get("physical_deficit_quantity", row["buy_quantity"]),
        "planned_need_quantity": row.get("planned_need_quantity", ZERO_QTY),
        "useful_forecast_total": row.get("useful_forecast_total", ZERO_QTY),
        "first_deficit_date": row.get("first_deficit_date"),
        "deadline_date": row.get("deadline_date"),
        "customer_demand_need": row.get("customer_demand_need"),
        "customer_demand_need_published": row.get("customer_demand_need_published", False),
        "has_customer_demand_deficit": row.get("has_customer_demand_deficit", False),
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
    linked_need = getattr(recommendation, "need", None)
    is_customer_demand_purchase = (
        linked_need is not None
        and getattr(linked_need, "source_system", None) == NeedSourceSystem.CUSTOMER_DEMAND
    )
    additional_needs = []
    if getattr(recommendation, "producer_id", None) and getattr(recommendation, "product_id", None):
        additional_needs = list(
            Need.objects.filter(
                producer_id=recommendation.producer_id,
                product_id=recommendation.product_id,
                status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            )
            .exclude(id=getattr(recommendation, "need_id", None))
            .exclude(source_system=NeedSourceSystem.CUSTOMER_DEMAND)
            .order_by("-updated_at")[:3]
        )
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
        "is_customer_demand_purchase": is_customer_demand_purchase,
        "additional_active_needs": additional_needs,
        "need_action_label": (
            "Gerir procura publicada"
            if is_customer_demand_purchase and getattr(linked_need, "is_marketplace_published", False)
            else "Publicar procura"
            if is_customer_demand_purchase
            else "Atualizar necessidade"
            if getattr(recommendation, "need_id", None)
            else "Criar necessidade"
        ),
    }


def _need_response_url(need_id, product_id, quantity=None):
    query = {"product": str(product_id)}
    if quantity is not None:
        query["qty"] = str(quantity)
    return f"{reverse('marketplace:need_respond', args=[need_id])}?{urlencode(query)}"


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

    stock = Stock.objects.filter(producer=producer, product=product).first()
    stock_publishable = get_max_publishable_quantity(stock) if stock else ZERO_QTY
    stock_active_listing = (
        get_my_listings(producer=producer, origin="stock")
        .filter(
            product=product,
            status=ListingStatus.ACTIVE,
            quantity_available__gt=ZERO_QTY,
        )
        .first()
    )
    active_listing = stock_active_listing
    publish_source = ""
    publish_quantity = ZERO_QTY
    publish_forecast = None
    if not active_listing and stock_publishable > ZERO_QTY:
        publish_source = "stock"
        publish_quantity = min(Decimal(str(requested_quantity)), stock_publishable)
    elif not active_listing:
        forecast_active_listing = (
            get_my_listings(producer=producer, origin="forecast")
            .filter(
                product=product,
                status=ListingStatus.ACTIVE,
                quantity_available__gt=ZERO_QTY,
            )
            .first()
        )
        if forecast_active_listing:
            active_listing = forecast_active_listing
        else:
            for forecast in get_marketplace_eligible_forecasts(producer, product=product):
                forecast_publishable = get_forecast_available_quantity(forecast)
                if forecast_publishable > ZERO_QTY:
                    publish_source = "forecast"
                    publish_forecast = forecast
                    publish_quantity = min(Decimal(str(requested_quantity)), forecast_publishable)
                    break

    publish_query = {
        "source": publish_source,
        "product": str(product.id),
        "from": "recommendations",
        "qty": str(publish_quantity),
    }
    if publish_forecast:
        publish_query["forecast"] = str(publish_forecast.id)
    return {
        "wizard_step": 2,
        "recommendation_direction": RECOMMENDATION_DIRECTION_SELL,
        "product": product,
        "requested_quantity": requested_quantity,
        "recommendation_metrics": metrics,
        "compatible_need_rows": compatible_needs[:6],
        "publish_url": (
            f"{reverse('marketplace:publish')}?{urlencode(publish_query)}"
            if publish_source
            else ""
        ),
        "publish_source": publish_source,
        "publish_quantity": publish_quantity,
        "publish_forecast": publish_forecast,
        "active_listing": active_listing,
        "manage_listing_url": (
            reverse("marketplace:owner_detail", args=[active_listing.id])
            if active_listing
            else ""
        ),
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

    if deficit_data.get("has_customer_demand_deficit") and not deficit_data.get("customer_demand_need"):
        sync_need_from_external_demands(
            producer=producer,
            product=product,
            acting_user=request.current_user,
        )
        deficit_data = calculate_current_deficit(producer, product)

    linked_need = deficit_data.get("customer_demand_need")
    if deficit_data.get("has_customer_demand_deficit") and not linked_need:
        form.initial.update({
            "product_id": str(product.id),
            "requested_quantity": requested_quantity,
        })
        context = _build_step_1_context(
            form=form,
            errors={"requested_quantity": "Não foi possível associar esta compra à procura calculada dos pedidos de clientes. Atualize os pedidos e tente novamente."},
            initial_deficit_quantity=deficit_data["deficit_quantity"],
            initial_current_quantity=deficit_data["current_stock"],
            metrics=deficit_data,
            inventory_rows=inventory_rows,
        )
        return _render_wizard(request, context)

    if linked_need and requested_quantity > deficit_data["buy_quantity"]:
        form.initial.update({
            "product_id": str(product.id),
            "requested_quantity": deficit_data["buy_quantity"],
        })
        context = _build_step_1_context(
            form=form,
            errors={
                "requested_quantity": (
                    f"Para cumprir os pedidos de clientes, compre no máximo "
                    f"{deficit_data['buy_quantity']} {product.unit}. "
                    "Quantidades adicionais devem ser tratadas separadamente."
                )
            },
            initial_deficit_quantity=deficit_data["deficit_quantity"],
            initial_current_quantity=deficit_data["current_stock"],
            metrics=deficit_data,
            inventory_rows=inventory_rows,
        )
        return _render_wizard(request, context)

    try:
        recommendation = generate_recommendation(
            producer=producer,
            product=product,
            requested_quantity=requested_quantity,
            deadline_date=deficit_data.get("deadline_date"),
            need=linked_need,
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
        Recommendation.objects.select_related("product", "producer", "need"),
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
        Recommendation.objects.select_related("product", "producer", "need"),
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
        if (
            recommendation.need_id
            and recommendation.need.source_system == NeedSourceSystem.CUSTOMER_DEMAND
        ):
            need = recommendation.need
            need, created = publish_need_to_marketplace(
                need=need,
                producer=producer,
                acting_user=request.current_user,
            )
        elif recommendation.need_id:
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
                acting_user=request.current_user,
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
            "title": (
                "Procura publicada"
                if recommendation.need_id
                and recommendation.need.source_system == NeedSourceSystem.CUSTOMER_DEMAND
                and created
                else "Procura já publicada"
                if recommendation.need_id
                and recommendation.need.source_system == NeedSourceSystem.CUSTOMER_DEMAND
                else "Necessidade criada"
                if created
                else "Necessidade atualizada"
            ),
            "message": (
                "A procura calculada a partir dos pedidos dos clientes está publicada para receber propostas. Quer abri-la agora?"
                if recommendation.need_id
                and recommendation.need.source_system == NeedSourceSystem.CUSTOMER_DEMAND
                else "A necessidade foi anunciada com o défice que ficou por cobrir. Quer abrir agora o detalhe desse pedido?"
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
        "Procura publicada para receber propostas."
        if recommendation.need_id
        and recommendation.need.source_system == NeedSourceSystem.CUSTOMER_DEMAND
        else "Necessidade anunciada com sucesso."
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
        Recommendation.objects.select_related("product", "producer", "need"),
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
        Recommendation.objects.select_related("product", "producer", "need"),
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
        Recommendation.objects.select_related("product", "producer", "need"),
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
        Recommendation.objects.select_related("product", "producer", "need"),
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

