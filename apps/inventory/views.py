from decimal import Decimal, ROUND_HALF_UP

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils import timezone

from apps.common.decorators import client_only_required, login_required
from apps.inventory.models import ProducerProduct
from apps.inventory import services
from apps.inventory.forms import (
    AddProducerProductForm,
    CreateCustomProductForm,
    UpdateStockForm,
)

def _get_producer_or_redirect(request):
    producer = services.get_producer_profile(request.current_user.id)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
    return producer


def _decimal_to_int(value):
    if value is None:
        return 0
    decimal_value = Decimal(str(value))
    return int(decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@login_required
@client_only_required
def meus_produtos(request):
    """
    Página principal: Stocks e Compras.
    - tab=stock
    - tab=compras
    - tab=desativados
    - HTMX usado na pesquisa do tab Stock
    """
    producer = _get_producer_or_redirect(request)
    if not producer:
        return redirect("dashboard:painel")

    active_tab = (request.GET.get("tab") or "stock").strip().lower()
    if active_tab not in {"stock", "compras", "desativados"}:
        active_tab = "stock"

    q = (request.GET.get("q") or "").strip()
    sort = (request.GET.get("sort") or "name").strip().lower()
    if sort not in {"name", "stock_desc", "stock_asc", "state"}:
        sort = "name"

    context = {
        "page_title": "Stocks e Compras",
        "active_tab": active_tab,
        "q": q,
        "sort": sort,
    }

    if active_tab == "compras":
        context.update(services.get_purchase_dashboard(producer))
        panel_template = "inventory/partials/compras_panel.html"
    elif active_tab == "desativados":
        context.update(services.get_deactivated_products_dashboard(producer, q=q))
        panel_template = "inventory/partials/deactivated_panel.html"
    else:
        context.update(services.get_stock_dashboard(producer, q=q, sort=sort))
        panel_template = "inventory/partials/stocks_panel.html"

    context["panel_template"] = panel_template

    if request.htmx:
        hx_target = (request.headers.get("HX-Target") or "").lstrip("#")
        if hx_target == "inventory-shell":
            return render(request, "inventory/partials/stocks_compras_shell.html", context)
        if hx_target == "shellMain":
            return render(request, "inventory/stocks_compras.html", context)
        return render(request, panel_template, context)

    return render(request, "inventory/stocks_compras.html", context)


@login_required
@client_only_required
def adicionar_produto(request):
    producer = _get_producer_or_redirect(request)
    if not producer:
        return redirect("dashboard:painel")

    available_products = services.get_available_products_to_add(producer)

    selected_product = None
    show_catalog_modal = False
    show_custom_modal = False

    if request.method == "POST":
        form_type = request.POST.get("form_type")

        if form_type == "custom":
            form = AddProducerProductForm()
            custom_form = CreateCustomProductForm(request.POST)
            show_custom_modal = True

            if custom_form.is_valid():
                try:
                    producer_product, stock, product_created, link_created = services.create_custom_product_for_producer(
                        producer=producer,
                        category=custom_form.cleaned_data["category"],
                        name=custom_form.cleaned_data["name"],
                        unit=custom_form.cleaned_data["unit"],
                        producer_description=custom_form.cleaned_data.get("producer_description", ""),
                        initial_quantity=custom_form.cleaned_data["initial_quantity"],
                        minimum_threshold=custom_form.cleaned_data["minimum_threshold"],
                        user=request.current_user,
                    )

                    if product_created:
                        messages.success(
                            request,
                            f"O produto {producer_product.product.name} foi criado e associado ao teu inventário."
                        )
                    else:
                        messages.success(
                            request,
                            f"O produto {producer_product.product.name} já existia e foi associado ao teu inventário."
                        )

                    return redirect("inventory:stock_detalhe", product_id=producer_product.product_id)

                except ValidationError as exc:
                    custom_form.add_error(None, str(exc))
                except Exception as exc:
                    custom_form.add_error(None, f"Erro ao criar produto: {exc}")

        else:
            form = AddProducerProductForm(request.POST)
            custom_form = CreateCustomProductForm()

            selected_product_id = request.POST.get("product_id")
            if selected_product_id:
                selected_product = available_products.filter(id=selected_product_id).first()
                show_catalog_modal = True

            if form.is_valid():
                try:
                    producer_product, stock, product_created, link_created = services.add_product_to_producer(
                        producer=producer,
                        product_id=form.cleaned_data["product_id"],
                        producer_description=form.cleaned_data.get("producer_description", ""),
                        initial_quantity=form.cleaned_data["initial_quantity"],
                        minimum_threshold=form.cleaned_data["minimum_threshold"],
                        user=request.current_user,
                    )

                    messages.success(
                        request,
                        f"{producer_product.product.name} foi adicionado com sucesso ao teu inventário."
                    )
                    return redirect("inventory:stock_detalhe", product_id=producer_product.product_id)

                except ValidationError as exc:
                    form.add_error(None, str(exc))
                except Exception as exc:
                    form.add_error(None, f"Erro ao adicionar produto: {exc}")

    else:
        form = AddProducerProductForm()
        custom_form = CreateCustomProductForm()

    context = {
        "form": form,
        "custom_form": custom_form,
        "available_products": available_products,
        "selected_product": selected_product,
        "show_catalog_modal": show_catalog_modal,
        "show_custom_modal": show_custom_modal,
        "page_title": "Adicionar Produto",
    }
    return render(request, "inventory/adicionar_produto.html", context)

@login_required
@client_only_required
@require_POST
def remover_produto(request, producer_product_id):
    producer = _get_producer_or_redirect(request)
    if not producer:
        return redirect("dashboard:painel")

    success, error = services.remove_product_from_producer(producer, producer_product_id)

    if success:
        messages.success(request, "Produto desativado com sucesso. Pode reativá-lo na aba de produtos desativados.")
    else:
        messages.error(request, error)

    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)

    return redirect(f"{reverse('inventory:meus_produtos')}?tab=desativados")


@login_required
@client_only_required
@require_POST
def reativar_produto(request, producer_product_id):
    producer = _get_producer_or_redirect(request)
    if not producer:
        return redirect("dashboard:painel")

    success, error = services.reactivate_product_from_producer(producer, producer_product_id)

    if success:
        messages.success(request, "Produto reativado com sucesso.")
    else:
        messages.error(request, error)

    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)

    return redirect(f"{reverse('inventory:meus_produtos')}?tab=desativados")


@login_required
@client_only_required
def stock_detalhe(request, product_id):
    producer = _get_producer_or_redirect(request)
    if not producer:
        return redirect("dashboard:painel")

    stock = services.get_stock_for_product(producer, product_id)
    if not stock:
        messages.error(request, "Produto não encontrado no teu inventário.")
        return redirect("inventory:meus_produtos")

    producer_product = ProducerProduct.objects.filter(
        producer=producer,
        product_id=product_id,
    ).first()

    activity_items = services.get_stock_activity_feed(stock)

    context = {
        "stock": stock,
        "producer_product": producer_product,
        "activity_items": activity_items,
        "page_title": f"Stock — {stock.product.name}",
    }
    return render(request, "inventory/stock_detalhe.html", context)


@login_required
@client_only_required
def atualizar_stock(request, product_id):
    producer = _get_producer_or_redirect(request)
    if not producer:
        return redirect("dashboard:painel")

    stock = services.get_stock_for_product(producer, product_id)
    if not stock:
        messages.error(request, "Produto não encontrado no teu inventário.")
        return redirect("inventory:meus_produtos")

    if request.method == "POST":
        form = UpdateStockForm(request.POST)
        if form.is_valid():
            try:
                new_quantity = Decimal(str(form.cleaned_data["new_quantity"]))
                minimum_threshold = Decimal(str(form.cleaned_data["minimum_threshold"]))
                services.update_stock(
                    stock=stock,
                    new_quantity=new_quantity,
                    minimum_threshold=minimum_threshold,
                    movement_type=form.cleaned_data["movement_type"],
                    user=request.current_user,
                    notes=form.cleaned_data.get("notes", ""),
                )
                messages.success(request, "Stock atualizado com sucesso.")
                return redirect("inventory:stock_detalhe", product_id=product_id)

            except ValidationError as exc:
                form.add_error(None, str(exc))

            except Exception as exc:
                form.add_error(None, f"Erro ao atualizar stock: {exc}")
    else:
        form = UpdateStockForm(initial={
            "new_quantity": _decimal_to_int(stock.current_quantity),
            "minimum_threshold": _decimal_to_int(stock.minimum_threshold),
        })

    context = {
        "form": form,
        "stock": stock,
        "page_title": f"Atualizar Stock — {stock.product.name}",
    }
    return render(request, "inventory/atualizar_stock.html", context)

@login_required
@client_only_required
def compras_export_pdf(request):
    producer = _get_producer_or_redirect(request)
    if not producer:
        return redirect("dashboard:painel")

    export_data = services.get_recent_orders_for_export(producer, limit=50)

    context = {
        "page_title": "Exportar Compras",
        "recent_orders": export_data["recent_orders"],
        "export_total": export_data["export_total"],
        "producer": producer,
        "generated_at": timezone.now(),
    }
    return render(request, "inventory/compras_export.html", context)
