from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from decimal import Decimal

from apps.common.decorators import login_required, client_only_required
from apps.common.htmx import with_htmx_toast
from apps.inventory.models import ProducerProfile, Stock
from apps.recommendations.forms import RecommendationRequestForm
from apps.recommendations.models import Recommendation
from apps.recommendations.services import (
    RecommendationGenerationError,
    accept_recommendation,
    calculate_current_deficit,
    generate_recommendation,
    get_market_alternative_listings,
    get_producer_products,
    get_recommendation_totals,
    get_selected_items,
)


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
    ).values_list("product_id", "current_quantity", "minimum_threshold")

    # Sem registo de stock assume crítico (equivalente a 0 atual vs 0 mínimo).
    critical_product_ids = {str(product_id) for product_id in product_ids}

    for product_id, current_quantity, minimum_threshold in stock_rows:
        current_qty = Decimal(str(current_quantity or 0))
        minimum_qty = Decimal(str(minimum_threshold or 0))
        if current_qty > minimum_qty:
            critical_product_ids.discard(str(product_id))

    for product in products:
        product.is_critical_stock = str(product.id) in critical_product_ids

    return products


def _build_step_1_context(
    *,
    form,
    wizard_step=1,
    errors=None,
    initial_deficit_quantity="0",
    initial_current_quantity="0",
):
    return {
        "wizard_step": wizard_step,
        "products": form.fields["product_id"].choices[1:],
        "errors": errors or {},
        "initial_product_id": form.initial.get("product_id", ""),
        "initial_deficit_quantity": initial_deficit_quantity,
        "initial_current_quantity": initial_current_quantity,
        "initial_requested_quantity": form.initial.get("requested_quantity", ""),
    }


def _remaining_deficit_from_recommendation(recommendation):
    deficit = recommendation.deficit_quantity
    if deficit is None:
        return Decimal("0.000")
    return max(Decimal(str(deficit)), Decimal("0.000"))


@login_required
@client_only_required
def recommendations_index_view(request):
    producer = _get_current_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    form_products = _get_form_products(producer)
    form = RecommendationRequestForm(products=form_products)

    context = _build_step_1_context(
        form=form,
        initial_deficit_quantity="0",
        initial_current_quantity="0",
    )
    return render(request, "recommendations/index.html", context)


@login_required
@client_only_required
def recommendations_product_metrics_view(request):
    producer = _get_current_producer(request)
    if not producer:
        return HttpResponse("")

    product_id = (request.GET.get("product_id") or "").strip()
    if not product_id:
        context = {
            "initial_deficit_quantity": "0",
            "initial_current_quantity": "0",
            "initial_requested_quantity": "0",
        }
        return render(request, "recommendations/partials/step_1_metrics.html", context)

    form_products = _get_form_products(producer)
    product = next((p for p in form_products if str(p.id) == product_id), None)

    if not product:
        context = {
            "initial_deficit_quantity": "0",
            "initial_current_quantity": "0",
            "initial_requested_quantity": "0",
        }
        return render(request, "recommendations/partials/step_1_metrics.html", context)

    deficit_data = calculate_current_deficit(producer, product)

    context = {
        "initial_deficit_quantity": deficit_data["deficit_quantity"],
        "initial_current_quantity": deficit_data["current_stock"],
        "initial_requested_quantity": deficit_data["deficit_quantity"],
    }
    return render(request, "recommendations/partials/step_1_metrics.html", context)


@login_required
@client_only_required
def recommendations_generate_view(request):
    if request.method != "POST":
        return redirect("recommendations:index")

    producer = _get_current_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    form_products = _get_form_products(producer)
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
        )
        return _render_wizard(request, context)

    requested_quantity = form.cleaned_data["requested_quantity"]

    try:
        recommendation = generate_recommendation(
            producer=producer,
            product=product,
            requested_quantity=requested_quantity,
            deadline_date=None,
        )
    except RecommendationGenerationError as exc:
        deficit_data = calculate_current_deficit(producer, product)
        form.initial.update({
            "product_id": str(product.id),
            "requested_quantity": requested_quantity,
        })
        context = _build_step_1_context(
            form=form,
            errors={"requested_quantity": str(exc)},
            initial_deficit_quantity=deficit_data["deficit_quantity"],
            initial_current_quantity=deficit_data["current_stock"],
        )
        return _render_wizard(request, context)

    selected_items = get_selected_items(recommendation)
    can_accept = selected_items.exists()
    remaining_deficit = _remaining_deficit_from_recommendation(recommendation)

    context = {
        "wizard_step": 2,
        "recommendation": recommendation,
        "selected_items": selected_items,
        "remaining_deficit": remaining_deficit,
        "market_options_expanded": False,
        "alternative_items": [],
        "can_accept": can_accept,
    }
    return _render_wizard(request, context)


@login_required
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
    )
    return _render_wizard(request, context)


@login_required
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


@login_required
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

    accept_recommendation(recommendation)

    if _is_htmx(request):
        response = HttpResponse("")
        response["HX-Redirect"] = "/encomendas/"
        return response

    messages.success(request, "Recomendação aceite com sucesso.")
    return redirect("/encomendas/")


@login_required
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


@login_required
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

    selected_items = get_selected_items(recommendation)
    remaining_deficit = _remaining_deficit_from_recommendation(recommendation)
    context = {
        "wizard_step": 2,
        "recommendation": recommendation,
        "selected_items": selected_items,
        "remaining_deficit": remaining_deficit,
        "market_options_expanded": False,
        "alternative_items": [],
        "can_accept": selected_items.exists(),
    }
    response = _render_wizard(request, context)
    return with_htmx_toast(
        response,
        "info",
        "A substituição manual de produtores fica para a próxima versão.",
    )
