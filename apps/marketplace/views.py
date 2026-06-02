import uuid
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.common.decorators import login_required, client_only_required
from apps.common.audit import log_audit_event
from apps.common.htmx import is_htmx_request as _is_htmx
from apps.common.redirects import get_safe_next_url
from apps.inventory.models import ProductionForecast
from apps.needs.models import NeedResponseStatus
from apps.needs.navigation import build_needs_index_url
from apps.marketplace.forms import MarketplacePublishForm, MarketplaceEditForm
from apps.marketplace.media import (
    _delete_uploaded_file,
    _listing_photo_url,
    _maybe_crop_uploaded_photo,
    _save_listing_photo,
)
from apps.marketplace.presenters import (
    _build_listing_purchase_quote,
    _build_marketplace_detail_context,
    _build_marketplace_index_context,
    _build_marketplace_index_query,
    _get_index_filters,
)
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.marketplace.services import (
    LISTING_SOURCE_FORECAST,
    LISTING_SOURCE_STOCK,
    MarketplaceServiceError,
    create_listing,
    expire_due_active_listings,
    get_marketplace_eligible_forecasts,
    get_current_producer_for_user,
    get_forecast_available_quantity,
    get_listing_detail_queryset,
    get_market_price_trends_for_product_sources,
    get_publishable_products,
    get_publishable_products_summary,
    is_listing_editable_in_marketplace,
    is_listing_retirable_in_marketplace,
    reactivate_listing,
    retire_listing,
    update_listing,
)


def _sync_alerts_after_marketplace_change(producer, acting_user):
    try:
        from apps.alerts.services import sync_alerts_for_producer
        sync_alerts_for_producer(producer, acting_user=acting_user)
    except Exception:
        return


def _audit_listing_context(listing):
    return {
        "listing_id": str(getattr(listing, "id", "")) or None,
        "product_id": str(getattr(listing, "product_id", "")) or None,
        "product_name": getattr(getattr(listing, "product", None), "name", None),
        "origin": (
            "need_response"
            if getattr(listing, "need_id", None)
            else LISTING_SOURCE_FORECAST
            if getattr(listing, "forecast_id", None)
            else LISTING_SOURCE_STOCK
        ),
        "need_id": str(listing.need_id) if getattr(listing, "need_id", None) else None,
    }

def _activate_forecast_for_marketplace_if_possible(*, producer, product_id, forecast_id):
    if not producer or not product_id or not forecast_id:
        return None, None

    try:
        forecast = ProductionForecast.objects.get(
            id=forecast_id,
            producer=producer,
            product_id=product_id,
        )
    except ProductionForecast.DoesNotExist:
        return None, "A previsão selecionada não foi encontrada para este produto."

    if get_forecast_available_quantity(forecast) <= Decimal("0"):
        return None, "Esta previsão não tem quantidade disponível para pré-venda."

    if not forecast.is_marketplace_enabled:
        forecast.is_marketplace_enabled = True
        if hasattr(forecast, "updated_at"):
            forecast.updated_at = timezone.now()
            forecast.save(update_fields=["is_marketplace_enabled", "updated_at"])
        else:
            forecast.save(update_fields=["is_marketplace_enabled"])

    return forecast, None




@login_required
def marketplace_index_view(request):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    expire_due_active_listings()
    active_tab, q, category_id, origin, sort, only_available, kind, selected_need_id, requested_product_id, requested_quantity, show_need_form = _get_index_filters(request)
    if active_tab == "necessidades":
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
    if active_tab == "meus" and not producer:
        active_tab = "todos"
    context = _build_marketplace_index_context(
        producer,
        active_tab=active_tab,
        q=q,
        category_id=category_id,
        origin=origin,
        sort=sort,
        only_available=only_available,
        kind=kind,
        selected_need_id=selected_need_id,
        need_prefill_product_id=requested_product_id,
        need_prefill_quantity=requested_quantity,
        show_need_form=show_need_form,
    )
    return render(request, "marketplace/index.html", context)


@login_required
def marketplace_detail_view(request, listing_id):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    expire_due_active_listings()

    listing = get_object_or_404(
        get_listing_detail_queryset(producer=producer),
        id=listing_id,
    )
    if getattr(listing, "need_id", None):
        return redirect("marketplace:proposal_detail", listing_id=listing.id)

    if producer and listing.producer_id == producer.id:
        return redirect("marketplace:owner_detail", listing_id=listing.id)
    return redirect("marketplace:public_detail", listing_id=listing.id)


@login_required
def marketplace_owner_detail_view(request, listing_id):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    expire_due_active_listings()

    listing = get_object_or_404(
        get_listing_detail_queryset(producer=producer),
        id=listing_id,
    )
    if getattr(listing, "need_id", None):
        return redirect("marketplace:proposal_detail", listing_id=listing.id)
    if not producer or listing.producer_id != producer.id:
        return redirect("marketplace:public_detail", listing_id=listing.id)

    context = _build_marketplace_detail_context(request, listing, producer)
    return render(request, "marketplace/detail.html", context)


@login_required
def marketplace_public_detail_view(request, listing_id):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    expire_due_active_listings()

    listing = get_object_or_404(
        get_listing_detail_queryset(producer=producer),
        id=listing_id,
    )
    if getattr(listing, "need_id", None):
        return redirect("marketplace:proposal_detail", listing_id=listing.id)
    if producer and listing.producer_id == producer.id:
        return redirect("marketplace:owner_detail", listing_id=listing.id)

    context = _build_marketplace_detail_context(request, listing, producer)
    return render(request, "marketplace/detail.html", context)


@login_required
def marketplace_detail_total_view(request, listing_id):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    expire_due_active_listings()

    listing = get_object_or_404(
        get_listing_detail_queryset(producer=producer),
        id=listing_id,
    )
    quote = _build_listing_purchase_quote(
        listing,
        raw_quantity=request.GET.get("qty"),
    )
    context = {
        "listing": listing,
        **quote,
    }
    return render(request, "marketplace/partials/detail_total.html", context)


@client_only_required
def marketplace_publish_view(request):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)

    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    legacy_need_id = (
        request.POST.get("need_id")
        or request.POST.get("need")
        or request.GET.get("need")
        or ""
    )
    legacy_origin = (request.POST.get("from") or request.GET.get("from") or "").strip().lower()
    if legacy_origin == "need" or legacy_need_id:
        if legacy_need_id:
            try:
                uuid.UUID(str(legacy_need_id).strip())
            except (TypeError, ValueError):
                return redirect(f"{reverse('marketplace:index')}?tab=todos&kind=needs")
            query = {}
            legacy_product_id = (request.POST.get("product") or request.GET.get("product") or "").strip()
            if legacy_product_id:
                query["product"] = legacy_product_id
            query_string = urlencode(query)
            response_url = reverse("marketplace:need_respond", args=[str(legacy_need_id).strip()])
            return redirect(f"{response_url}?{query_string}" if query_string else response_url)
        legacy_product_id = (request.POST.get("product") or request.GET.get("product") or "").strip()
        query = {"tab": "todos", "kind": "needs"}
        return redirect(f"{reverse('marketplace:index')}?{urlencode(query)}")

    success = request.GET.get("success") == "1"
    created_listing_id = request.GET.get("listing_id")
    requested_product_id = (request.POST.get("product") or request.GET.get("product") or "").strip()
    requested_quantity = (request.GET.get("qty") or request.GET.get("quantity") or "").strip()
    requested_source = (request.POST.get("listing_source") or request.GET.get("source") or LISTING_SOURCE_STOCK).strip().lower()
    requested_forecast_id = (request.POST.get("forecast") or request.GET.get("forecast") or "").strip()
    prefill_origin = (request.POST.get("from") or request.GET.get("from") or "").strip().lower()
    forecast_quantity_limit = None

    is_forecast_prefill_flow = (
        requested_source == LISTING_SOURCE_FORECAST
        and bool(requested_product_id)
        and bool(requested_forecast_id)
    )
    is_stock_prefill_flow = (
        prefill_origin in {"inventory", "recommendations"}
        and requested_source == LISTING_SOURCE_STOCK
        and bool(requested_product_id)
    )

    if is_forecast_prefill_flow:
        activated_forecast, activation_error = _activate_forecast_for_marketplace_if_possible(
            producer=producer,
            product_id=requested_product_id,
            forecast_id=requested_forecast_id,
        )
        if activation_error:
            messages.error(request, activation_error)
            is_forecast_prefill_flow = False
        elif activated_forecast:
            requested_product_id = str(activated_forecast.product_id)
            requested_forecast_id = str(activated_forecast.id)
            forecast_quantity_limit = get_forecast_available_quantity(activated_forecast)

    lock_listing_source = is_forecast_prefill_flow or is_stock_prefill_flow
    lock_product = lock_listing_source

    form_initial = {}
    if requested_product_id:
        form_initial["product"] = requested_product_id
    if request.method == "GET" and requested_quantity:
        form_initial["quantity"] = requested_quantity
    form_initial["listing_source"] = (
        requested_source if requested_source in {LISTING_SOURCE_STOCK, LISTING_SOURCE_FORECAST}
        else LISTING_SOURCE_STOCK
    )
    if requested_forecast_id:
        form_initial["forecast"] = requested_forecast_id

    form = MarketplacePublishForm(
        request.POST or None,
        request.FILES or None,
        producer=producer,
        initial=form_initial,
        lock_listing_source=lock_listing_source,
        lock_product=lock_product,
    )
    if is_forecast_prefill_flow and forecast_quantity_limit is not None:
        form.fields["quantity"].widget.attrs["max"] = str(forecast_quantity_limit)
        form.fields["quantity"].widget.attrs["data-max"] = str(forecast_quantity_limit)
        if request.method == "GET":
            form.initial.setdefault("quantity", forecast_quantity_limit)

    selected_product_raw = form["product"].value()
    selected_product_id = (str(selected_product_raw).strip() if selected_product_raw else "")
    selected_source_raw = form["listing_source"].value()
    selected_source = (str(selected_source_raw).strip().lower() if selected_source_raw else LISTING_SOURCE_STOCK)
    if selected_source not in {LISTING_SOURCE_STOCK, LISTING_SOURCE_FORECAST}:
        selected_source = LISTING_SOURCE_STOCK

    all_publishable_products = list(
        get_publishable_products(producer).values("id", "name")
    )
    eligible_forecasts_for_picker = get_marketplace_eligible_forecasts(producer)
    forecast_picker_options = []
    for forecast in eligible_forecasts_for_picker:
        local_start = (
            timezone.localtime(forecast.period_start)
            if forecast.period_start and timezone.is_aware(forecast.period_start)
            else forecast.period_start
        )
        local_end = (
            timezone.localtime(forecast.period_end)
            if forecast.period_end and timezone.is_aware(forecast.period_end)
            else forecast.period_end
        )
        if local_start and local_end:
            period_label = f"{local_start.strftime('%d/%m/%Y')} - {local_end.strftime('%d/%m/%Y')}"
        elif local_start:
            period_label = f"A partir de {local_start.strftime('%d/%m/%Y')}"
        else:
            period_label = "Sem período definido"

        available_qty = get_forecast_available_quantity(forecast)
        forecast_picker_options.append({
            "id": str(forecast.id),
            "product_id": str(forecast.product_id),
            "label": (
                f"{forecast.product.name} · {period_label} · "
                f"Disponível {available_qty} {forecast.product.unit}"
            ),
        })

    product_ids_for_trends = list(
        form.fields["product"].queryset.values_list("id", flat=True)
    )
    trend_map = get_market_price_trends_for_product_sources(
        producer,
        product_ids=product_ids_for_trends,
    )
    publishable_summary = get_publishable_products_summary(
        producer,
        trend_map=trend_map,
    )

    if is_stock_prefill_flow:
        publishable_summary = [
            row for row in publishable_summary
            if row["product_id"] == requested_product_id and row["source"] == LISTING_SOURCE_STOCK
        ]
        selected_product_id = requested_product_id
        selected_source = LISTING_SOURCE_STOCK

    initial_market_trend = None
    if selected_product_id:
        selected_row = next(
            (
                row for row in publishable_summary
                if row["product_id"] == selected_product_id and row["source"] == selected_source
            ),
            None,
        )
        if selected_row:
            initial_market_trend = {
                "product_name": selected_row["product"].name,
                "product_unit": selected_row["product"].unit,
                "source": selected_row["source"],
                "source_label": (
                    "Disponível agora"
                    if selected_row["source"] == LISTING_SOURCE_STOCK
                    else "Pré-venda"
                ),
                "market_min_price": selected_row.get("market_min_price"),
                "market_max_price": selected_row.get("market_max_price"),
                "market_count": selected_row.get("market_count", 0),
            }

    if request.method == "POST" and form.is_valid():
        uploaded_photo = request.FILES.get("photo")
        photo_crop = form.cleaned_data.get("photo_crop")
        photo_path = None
        listing_source = form.cleaned_data.get("listing_source") or LISTING_SOURCE_STOCK
        selected_forecast = form.cleaned_data.get("forecast")

        try:
            if listing_source == LISTING_SOURCE_STOCK and selected_forecast is not None:
                raise MarketplaceServiceError(
                    "Configuração inválida da oferta: stock atual não pode ter previsão associada."
                )
            if listing_source == LISTING_SOURCE_FORECAST and selected_forecast is None:
                raise MarketplaceServiceError(
                    "Configuração inválida da oferta: pré-venda exige previsão associada."
                )

            if uploaded_photo:
                cropped_photo = _maybe_crop_uploaded_photo(uploaded_photo, photo_crop)
                photo_path = _save_listing_photo(producer, cropped_photo)

            listing = create_listing(
                producer=producer,
                product=form.cleaned_data["product"],
                quantity=form.cleaned_data["quantity"],
                unit_price=form.cleaned_data["unit_price"],
                delivery_mode=form.cleaned_data["delivery_mode"],
                delivery_radius_km=form.cleaned_data.get("delivery_radius_km"),
                delivery_fee=form.cleaned_data.get("delivery_fee"),
                show_location_on_map=form.cleaned_data.get("show_location_on_map", True),
                notes=form.cleaned_data.get("notes"),
                photo_path=photo_path,
                status=form.cleaned_data.get("status"),
                expires_at=form.cleaned_data.get("expires_at_final"),
                listing_source=listing_source,
                forecast=selected_forecast,
                need=None,
                acting_user=request.current_user,
            )
        except MarketplaceServiceError as exc:
            _delete_uploaded_file(photo_path)
            log_audit_event(
                request=request,
                action="LISTING_INVALID_ATTEMPT",
                entity_type="marketplace_listings",
                notes=f"Publicação recusada pelas regras do marketplace: {exc}",
                new_values={
                    "product_id": str(form.cleaned_data["product"].id),
                    "product_name": form.cleaned_data["product"].name,
                    "quantity_total": str(form.cleaned_data["quantity"]),
                    "origin": listing_source,
                },
            )
            form.add_error(None, str(exc))
        except Exception:
            _delete_uploaded_file(photo_path)
            form.add_error(None, "Não foi possível guardar a foto do anúncio.")
        else:
            _sync_alerts_after_marketplace_change(producer, request.current_user)

            messages.success(request, "Anúncio publicado com sucesso.")
            url = reverse("marketplace:publish")
            return redirect(f"{url}?success=1&listing_id={listing.id}")

    context = {
        "page_title": "Publicar Anúncio",
        "publish_title": "Publicar Anúncio",
        "publish_subtitle": "Venda os seus produtos no marketplace da cooperativa.",
        "publish_submit_label": "Publicar no marketplace",
        "publish_cancel_label": "Cancelar",
        "publish_cancel_url": reverse("marketplace:index"),
        "success": success,
        "form": form,
        "created_listing_id": created_listing_id,
        "publishable_summary": publishable_summary,
        "forecast_quantity_limit": forecast_quantity_limit,
        "selected_product_id": selected_product_id,
        "selected_source": selected_source,
        "initial_market_trend": initial_market_trend,
        "is_inventory_stock_prefill_flow": is_stock_prefill_flow,
        "product_picker_options": [
            {"id": str(row["id"]), "label": row["name"]}
            for row in all_publishable_products
        ],
        "forecast_picker_options": forecast_picker_options,
    }
    return render(request, "marketplace/publish.html", context)


@client_only_required
def marketplace_edit_view(request, listing_id):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)

    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    expire_due_active_listings()
    listing = get_object_or_404(
        MarketplaceListing.objects.select_related("product", "stock", "forecast", "producer"),
        id=listing_id,
        producer=producer,
    )
    if listing.need_id:
        messages.warning(
            request,
            "Esta proposta pertence ao separador Propostas do marketplace.",
        )
        if listing.need_response_status == NeedResponseStatus.PENDING and listing.status == ListingStatus.ACTIVE:
            return redirect("marketplace:proposal_edit", listing_id=listing.id)
        return redirect("marketplace:proposal_detail", listing_id=listing.id)

    if not is_listing_editable_in_marketplace(listing):
        messages.warning(
            request,
            "Este anúncio já está reservado ou fechado e não pode ser editado.",
        )
        return redirect("marketplace:owner_detail", listing_id=listing.id)

    has_stock_source = bool(listing.stock_id)
    has_forecast_source = bool(listing.forecast_id)
    if has_stock_source == has_forecast_source:
        messages.error(
            request,
            "A listing está com origem inválida (stock/previsão). Corrija os dados antes de editar.",
        )
        return redirect(f"{reverse('marketplace:index')}?tab=meus")

    form = MarketplaceEditForm(request.POST or None, request.FILES or None, listing=listing)
    current_photo_url = _listing_photo_url(listing.photo_path)

    if request.method == "POST" and form.is_valid():
        uploaded_photo = request.FILES.get("photo")
        photo_crop = form.cleaned_data.get("photo_crop")
        new_photo_path = None
        old_photo_path = listing.photo_path

        try:
            if uploaded_photo:
                cropped_photo = _maybe_crop_uploaded_photo(uploaded_photo, photo_crop)
                new_photo_path = _save_listing_photo(producer, cropped_photo)

            update_listing(
                listing=listing,
                quantity_total=form.cleaned_data["quantity_total"],
                unit_price=form.cleaned_data["unit_price"],
                delivery_mode=form.cleaned_data["delivery_mode"],
                delivery_radius_km=form.cleaned_data.get("delivery_radius_km"),
                delivery_fee=form.cleaned_data.get("delivery_fee"),
                show_location_on_map=form.cleaned_data.get("show_location_on_map", True),
                notes=form.cleaned_data.get("notes"),
                status=form.cleaned_data["status"],
                expires_at=form.cleaned_data.get("expires_at_final"),
                photo_path=new_photo_path if uploaded_photo else listing.photo_path,
                acting_user=request.current_user,
            )
        except MarketplaceServiceError as exc:
            _delete_uploaded_file(new_photo_path)
            log_audit_event(
                request=request,
                action="LISTING_INVALID_ATTEMPT",
                entity_type="marketplace_listings",
                entity_id=listing.id,
                notes=f"Edição recusada pelas regras do marketplace: {exc}",
                old_values=_audit_listing_context(listing) | {"status": listing.status},
            )
            form.add_error(None, str(exc))
        except Exception:
            _delete_uploaded_file(new_photo_path)
            form.add_error(None, "Não foi possível atualizar o anúncio.")
        else:
            if new_photo_path and old_photo_path and old_photo_path != new_photo_path:
                _delete_uploaded_file(old_photo_path)
            messages.success(request, "Anúncio atualizado com sucesso.")
            _sync_alerts_after_marketplace_change(producer, request.current_user)
            return redirect(f"{reverse('marketplace:index')}?tab=meus")

    context = {
        "page_title": "Editar Anúncio",
        "listing": listing,
        "form": form,
        "current_photo_url": current_photo_url,
    }
    return render(request, "marketplace/edit.html", context)


@client_only_required
def marketplace_delete_view(request, listing_id):
    if request.method != "POST":
        return redirect("marketplace:index")

    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    listing = get_object_or_404(
        MarketplaceListing.objects.select_related("producer"),
        id=listing_id,
        producer=producer,
    )
    active_tab, q, category_id, origin, sort, only_available, kind, selected_need_id, requested_product_id, requested_quantity, show_need_form = _get_index_filters(request)

    if listing.need_id:
        messages.warning(
            request,
            "Esta proposta pertence ao separador Propostas e não deve ser removida como anúncio.",
        )
        return redirect("marketplace:proposal_detail", listing_id=listing.id)

    if not is_listing_retirable_in_marketplace(listing):
        messages.warning(
            request,
            "Este anúncio já tem histórico reservado/fechado e não pode ser removido.",
        )
        if _is_htmx(request):
            context = _build_marketplace_index_context(
                producer,
                active_tab=active_tab,
                q=q,
                category_id=category_id,
                origin=origin,
                sort=sort,
                only_available=only_available,
                kind=kind,
                selected_need_id=selected_need_id,
                need_prefill_product_id=requested_product_id,
                need_prefill_quantity=requested_quantity,
                show_need_form=show_need_form,
            )
            return render(request, "marketplace/index.html", context)
        return redirect("marketplace:owner_detail", listing_id=listing.id)

    reserved_quantity = Decimal(str(listing.quantity_reserved or 0))
    if reserved_quantity > 0:
        messages.error(
            request,
            (
                "Não pode eliminar este anúncio porque tem quantidade reservada. "
                "Desative-o ou ajuste primeiro."
            ),
        )
        if _is_htmx(request):
            context = _build_marketplace_index_context(
                producer,
                active_tab=active_tab,
                q=q,
                category_id=category_id,
                origin=origin,
                sort=sort,
                only_available=only_available,
                kind=kind,
                selected_need_id=selected_need_id,
                need_prefill_product_id=requested_product_id,
                need_prefill_quantity=requested_quantity,
                show_need_form=show_need_form,
            )
            return render(request, "marketplace/index.html", context)

        next_url = get_safe_next_url(request, request.POST.get("next"))
        if next_url:
            return redirect(next_url)
        return redirect("marketplace:edit", listing_id=listing.id)

    photo_path = listing.photo_path
    listing._audit_actor = request.current_user
    retire_listing(listing=listing)
    _delete_uploaded_file(photo_path)

    messages.success(request, "Anúncio removido do marketplace com sucesso.")
    _sync_alerts_after_marketplace_change(producer, request.current_user)
    if _is_htmx(request):
        context = _build_marketplace_index_context(
            producer,
            active_tab=active_tab,
            q=q,
            category_id=category_id,
            origin=origin,
            sort=sort,
            only_available=only_available,
            kind=kind,
            selected_need_id=selected_need_id,
            need_prefill_product_id=requested_product_id,
            need_prefill_quantity=requested_quantity,
            show_need_form=show_need_form,
        )
        return render(request, "marketplace/index.html", context)

    return redirect(f"{reverse('marketplace:index')}?tab=meus")


@client_only_required
def marketplace_toggle_status_view(request, listing_id):
    if request.method != "POST":
        return redirect("marketplace:index")

    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    expire_due_active_listings()
    listing = get_object_or_404(
        MarketplaceListing.objects.select_related("producer"),
        id=listing_id,
        producer=producer,
    )

    if listing.need_id:
        messages.warning(
            request,
            "Esta proposta pertence ao separador Propostas do marketplace.",
        )
        return redirect("marketplace:proposal_detail", listing_id=listing.id)

    now = timezone.now()
    previous_status = listing.status
    feedback = None
    blocked_message = None
    status_saved_by_service = False

    if listing.status == ListingStatus.ACTIVE:
        listing.status = ListingStatus.CANCELLED
        feedback = "Anúncio desativado com sucesso."
    else:
        try:
            listing = reactivate_listing(listing=listing, acting_user=request.current_user)
        except MarketplaceServiceError as exc:
            blocked_message = str(exc)
        else:
            status_saved_by_service = True
            feedback = "Anúncio ativado com sucesso."

    if blocked_message:
        messages.warning(request, blocked_message)
        if _is_htmx(request) and (request.POST.get("source") or "") == "detail":
            detail_listing = get_object_or_404(
                get_listing_detail_queryset(producer=producer),
                id=listing_id,
            )
            detail_context = _build_marketplace_detail_context(request, detail_listing, producer)
            return render(request, "marketplace/detail.html", detail_context)

        active_tab, q, category_id, origin, sort, only_available, kind, selected_need_id, requested_product_id, requested_quantity, show_need_form = _get_index_filters(request)
        context = _build_marketplace_index_context(
            producer,
            active_tab=active_tab,
            q=q,
            category_id=category_id,
            origin=origin,
            sort=sort,
            only_available=only_available,
            kind=kind,
            selected_need_id=selected_need_id,
            need_prefill_product_id=requested_product_id,
            need_prefill_quantity=requested_quantity,
            show_need_form=show_need_form,
        )
        if _is_htmx(request):
            return render(request, "marketplace/index.html", context)

        next_url = get_safe_next_url(request, request.POST.get("next"))
        if next_url:
            return redirect(next_url)

        query = _build_marketplace_index_query(
            active_tab=active_tab,
            q=q,
            category_id=category_id,
            origin=origin,
            sort=sort,
            only_available=only_available,
            kind=kind,
        )
        return redirect(f"{reverse('marketplace:index')}?{query}")

    if not status_saved_by_service:
        listing.updated_at = now
        listing.save(update_fields=["status", "expires_at", "updated_at"])
        log_audit_event(
            request=request,
            action="LISTING_STATUS_CHANGED",
            entity_type="marketplace_listings",
            entity_id=listing.id,
            notes="Estado do anúncio alterado pelo produtor.",
            old_values=_audit_listing_context(listing) | {"status": previous_status},
            new_values=_audit_listing_context(listing) | {"status": listing.status},
        )
    messages.success(request, feedback)
    _sync_alerts_after_marketplace_change(producer, request.current_user)

    next_url = get_safe_next_url(request, request.POST.get("next"))
    if next_url and not _is_htmx(request):
        return redirect(next_url)

    if _is_htmx(request) and (request.POST.get("source") or "") == "detail":
        detail_listing = get_object_or_404(
            get_listing_detail_queryset(producer=producer),
            id=listing_id,
        )
        detail_context = _build_marketplace_detail_context(request, detail_listing, producer)
        return render(request, "marketplace/detail.html", detail_context)

    active_tab, q, category_id, origin, sort, only_available, kind, selected_need_id, requested_product_id, requested_quantity, show_need_form = _get_index_filters(request)
    context = _build_marketplace_index_context(
        producer,
        active_tab=active_tab,
        q=q,
        category_id=category_id,
        origin=origin,
        sort=sort,
        only_available=only_available,
        kind=kind,
        selected_need_id=selected_need_id,
        need_prefill_product_id=requested_product_id,
        need_prefill_quantity=requested_quantity,
        show_need_form=show_need_form,
    )

    if _is_htmx(request):
        return render(request, "marketplace/index.html", context)

    query = _build_marketplace_index_query(
        active_tab=active_tab,
        q=q,
        category_id=category_id,
        origin=origin,
        sort=sort,
        only_available=only_available,
        kind=kind,
    )
    return redirect(f"{reverse('marketplace:index')}?{query}")
