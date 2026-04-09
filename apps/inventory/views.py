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
    ProductionForecastForm,
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


def _to_datetime_local(value):
    if not value:
        return ""
    local_value = timezone.localtime(value) if timezone.is_aware(value) else value
    return local_value.strftime("%Y-%m-%dT%H:%M")


def _ensure_aware_datetime(value):
    if not value:
        return value
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _build_stock_detail_context(
    *,
    producer,
    stock,
    forecast_form=None,
    edit_forecast_id=None,
    forecast_mode=None,
    forecast_rows=None,
):
    producer_product = ProducerProduct.objects.filter(
        producer=producer,
        product_id=stock.product_id,
    ).first()
    is_product_active = bool(producer_product and producer_product.is_active)

    activity_items = services.get_stock_activity_feed(stock)
    if forecast_rows is None:
        forecast_rows = services.get_product_forecasts(producer, stock.product_id)

    forecast_count = len(forecast_rows)
    forecast_conflict = forecast_count > 1
    forecast_primary_row = forecast_rows[0] if forecast_rows else None
    forecast_primary = forecast_primary_row["forecast"] if forecast_primary_row else None

    if forecast_mode == "new":
        edit_forecast_id = None
    elif (
        forecast_mode == "edit"
        and not edit_forecast_id
        and not forecast_conflict
        and forecast_primary
    ):
        edit_forecast_id = str(forecast_primary.id)

    stock_state = services.get_stock_state(stock)

    if not forecast_form:
        forecast_form = ProductionForecastForm()

    if forecast_conflict:
        for field_name, field in forecast_form.fields.items():
            if field_name == "forecast_id":
                continue
            field.widget.attrs["disabled"] = "disabled"

    editing_forecast = None
    if edit_forecast_id:
        for row in forecast_rows:
            if str(row["forecast"].id) == str(edit_forecast_id):
                editing_forecast = row["forecast"]
                break

    context = {
        "stock": stock,
        "stock_state": stock_state,
        "producer_product": producer_product,
        "is_product_active": is_product_active,
        "stock_back_tab": "stock" if is_product_active else "desativados",
        "activity_items": activity_items,
        "forecast_rows": forecast_rows,
        "forecast_count": forecast_count,
        "forecast_conflict": forecast_conflict,
        "forecast_primary": forecast_primary,
        "forecast_mode": "edit" if editing_forecast else "new",
        "forecast_form": forecast_form,
        "editing_forecast": editing_forecast,
        "page_title": f"Stock — {stock.product.name}",
    }
    return context


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
                        safety_stock=custom_form.cleaned_data["safety_stock"],
                        surplus_threshold=Decimal(str(custom_form.cleaned_data.get("surplus_threshold") or 0)),
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
                        safety_stock=form.cleaned_data["safety_stock"],
                        surplus_threshold=Decimal(str(form.cleaned_data.get("surplus_threshold") or 0)),
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

    forecast_rows = services.get_product_forecasts(producer, product_id)
    forecast_conflict = len(forecast_rows) > 1
    forecast_mode = (request.GET.get("forecast_mode") or "").strip().lower()
    edit_forecast_id = (request.GET.get("edit_forecast") or "").strip() or None

    if forecast_mode == "new":
        edit_forecast_id = None
    elif (
        forecast_mode == "edit"
        and not edit_forecast_id
        and not forecast_conflict
        and forecast_rows
    ):
        edit_forecast_id = str(forecast_rows[0]["forecast"].id)

    initial = {}
    if edit_forecast_id and not forecast_conflict:
        for row in forecast_rows:
            forecast = row["forecast"]
            if str(forecast.id) == edit_forecast_id:
                initial = {
                    "forecast_id": str(forecast.id),
                    "forecast_quantity": forecast.forecast_quantity,
                    "period_start": _to_datetime_local(forecast.period_start),
                    "period_end": _to_datetime_local(forecast.period_end),
                    "is_marketplace_enabled": forecast.is_marketplace_enabled,
                }
                break

    forecast_form = ProductionForecastForm(initial=initial or None)
    context = _build_stock_detail_context(
        producer=producer,
        stock=stock,
        forecast_form=forecast_form,
        edit_forecast_id=edit_forecast_id,
        forecast_mode=forecast_mode,
        forecast_rows=forecast_rows,
    )
    return render(request, "inventory/stock_detalhe.html", context)


@login_required
@client_only_required
@require_POST
def guardar_previsao(request, product_id):
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
    if not producer_product or not producer_product.is_active:
        messages.warning(request, "Este produto está desativado. Reative-o para gerir produção futura.")
        return redirect("inventory:stock_detalhe", product_id=product_id)

    form = ProductionForecastForm(request.POST)
    if form.is_valid():
        try:
            forecast_id = form.cleaned_data.get("forecast_id")
            forecast, created = services.save_product_forecast(
                producer=producer,
                product=stock.product,
                forecast_quantity=form.cleaned_data["forecast_quantity"],
                period_start=_ensure_aware_datetime(form.cleaned_data.get("period_start")),
                period_end=_ensure_aware_datetime(form.cleaned_data.get("period_end")),
                is_marketplace_enabled=form.cleaned_data.get("is_marketplace_enabled", False),
                user=request.current_user,
                forecast_id=forecast_id,
            )

            if created:
                messages.success(request, "Produção futura registada com sucesso.")
            elif forecast_id:
                messages.success(request, "Produção futura atualizada com sucesso.")
            else:
                messages.success(
                    request,
                    "Produção futura substituída com sucesso (mesmo registo).",
                )

            return redirect("inventory:stock_detalhe", product_id=product_id)
        except ValidationError as exc:
            form.add_error(None, str(exc))
        except Exception as exc:
            form.add_error(None, f"Não foi possível guardar a previsão: {exc}")

    edit_forecast_id = form.data.get("forecast_id")
    forecast_mode = (form.data.get("forecast_mode") or "").strip().lower()
    forecast_rows = services.get_product_forecasts(producer, product_id)
    context = _build_stock_detail_context(
        producer=producer,
        stock=stock,
        forecast_form=form,
        edit_forecast_id=edit_forecast_id,
        forecast_mode=forecast_mode,
        forecast_rows=forecast_rows,
    )
    return render(request, "inventory/stock_detalhe.html", context, status=400)


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

    producer_product = ProducerProduct.objects.filter(
        producer=producer,
        product_id=product_id,
    ).first()
    if not producer_product or not producer_product.is_active:
        messages.warning(request, "Este produto está desativado. Reative-o para atualizar o stock.")
        return redirect("inventory:stock_detalhe", product_id=product_id)

    if request.method == "POST":
        form = UpdateStockForm(request.POST)
        if form.is_valid():
            try:
                new_quantity = Decimal(str(form.cleaned_data["new_quantity"]))
                safety_stock = Decimal(str(form.cleaned_data["safety_stock"]))
                surplus_threshold = Decimal(str(form.cleaned_data.get("surplus_threshold") or 0))
                services.update_stock(
                    stock=stock,
                    new_quantity=new_quantity,
                    safety_stock=safety_stock,
                    surplus_threshold=surplus_threshold,
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
            "safety_stock": _decimal_to_int(stock.safety_stock),
            "surplus_threshold": _decimal_to_int(stock.surplus_threshold),
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

