import io
import json
import uuid
from pathlib import Path
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile, InMemoryUploadedFile
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from PIL import Image, ImageOps

from apps.common.decorators import login_required, client_only_required
from apps.accounts.models import UserRole
from apps.inventory.models import ProducerProduct, ProductionForecast
from apps.marketplace.forms import MarketplacePublishForm, MarketplaceEditForm
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.marketplace.services import (
    LISTING_SOURCE_FORECAST,
    LISTING_SOURCE_STOCK,
    MarketplaceServiceError,
    build_delivery_text,
    create_listing,
    expire_due_active_listings,
    get_current_producer_for_user,
    get_forecast_available_quantity,
    get_listing_categories_for_queryset,
    get_listing_detail_queryset,
    get_market_price_trends_for_product_sources,
    get_my_listings,
    get_publishable_products_summary,
    get_producer_display_name,
    get_producer_initials,
    get_producer_location,
    get_public_listings,
    update_listing,
)


def _listing_photo_url(photo_path):
    if not photo_path:
        return None

    photo_path = str(photo_path).strip()
    if not photo_path:
        return None

    if photo_path.startswith(("http://", "https://", "/")):
        return photo_path

    return f"{settings.MEDIA_URL}{photo_path.lstrip('/')}"


def _attach_listing_photo_urls(listings):
    attached = []
    for listing in listings:
        listing.photo_url = _listing_photo_url(getattr(listing, "photo_path", None))
        has_stock_source = bool(getattr(listing, "stock_id", None))
        has_forecast_source = bool(getattr(listing, "forecast_id", None))
        if has_forecast_source and not has_stock_source:
            listing.source_key = LISTING_SOURCE_FORECAST
            listing.source_label = "Pré-venda"
            listing.source_badge_class = "mk-badge--forecast"
            if getattr(listing, "forecast", None):
                period_start = getattr(listing.forecast, "period_start", None)
                period_end = getattr(listing.forecast, "period_end", None)
                local_start = timezone.localtime(period_start) if period_start and timezone.is_aware(period_start) else period_start
                local_end = timezone.localtime(period_end) if period_end and timezone.is_aware(period_end) else period_end
                if period_start and period_end:
                    listing.source_period = (
                        f"{local_start.strftime('%d/%m/%Y')} - "
                        f"{local_end.strftime('%d/%m/%Y')}"
                    )
                elif period_start:
                    listing.source_period = f"A partir de {local_start.strftime('%d/%m/%Y')}"
                else:
                    listing.source_period = None
            else:
                listing.source_period = None
        else:
            listing.source_key = LISTING_SOURCE_STOCK
            listing.source_label = "Disponível agora"
            listing.source_badge_class = "mk-badge--stock"
            listing.source_period = None
        attached.append(listing)
    return attached


def _first_non_empty_text(*values):
    for value in values:
        text = (value or "").strip()
        if text:
            return text
    return None


def _save_listing_photo(producer, uploaded_file):
    if not isinstance(uploaded_file, UploadedFile):
        raise ValueError("O ficheiro enviado para o anúncio é inválido.")

    extension = Path(uploaded_file.name).suffix.lower() or ".jpg"
    filename = (
        f"marketplace/listings/{producer.id}/"
        f"{uuid.uuid4().hex}{extension}"
    )
    return default_storage.save(filename, uploaded_file)


def _delete_uploaded_file(file_path):
    if not file_path:
        return
    try:
        if default_storage.exists(file_path):
            default_storage.delete(file_path)
    except Exception:
        return


def _parse_photo_crop_payload(payload):
    if not payload:
        return None

    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    try:
        x = float(parsed.get("x", 0))
        y = float(parsed.get("y", 0))
        w = float(parsed.get("w", 0))
        h = float(parsed.get("h", 0))
    except (TypeError, ValueError):
        return None

    x = max(0.0, min(x, 1.0))
    y = max(0.0, min(y, 1.0))
    w = max(0.0, min(w, 1.0))
    h = max(0.0, min(h, 1.0))

    if w <= 0 or h <= 0:
        return None

    if x + w > 1.0:
        w = 1.0 - x
    if y + h > 1.0:
        h = 1.0 - y

    if w <= 0 or h <= 0:
        return None

    return x, y, w, h


def _maybe_crop_uploaded_photo(uploaded_file, crop_payload):
    crop_data = _parse_photo_crop_payload(crop_payload)
    if not crop_data:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
        return uploaded_file

    try:
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size

            left = int(round(crop_data[0] * width))
            top = int(round(crop_data[1] * height))
            right = int(round((crop_data[0] + crop_data[2]) * width))
            bottom = int(round((crop_data[1] + crop_data[3]) * height))

            left = max(0, min(left, width - 1))
            top = max(0, min(top, height - 1))
            right = max(left + 1, min(right, width))
            bottom = max(top + 1, min(bottom, height))

            if left == 0 and top == 0 and right == width and bottom == height:
                uploaded_file.seek(0)
                return uploaded_file

            cropped = image.crop((left, top, right, bottom))

            output = io.BytesIO()
            source_format = (image.format or "JPEG").upper()
            save_format = source_format if source_format in {"JPEG", "JPG", "PNG", "WEBP"} else "JPEG"

            if save_format in {"JPEG", "JPG"} and cropped.mode not in {"RGB", "L"}:
                cropped = cropped.convert("RGB")
                save_format = "JPEG"

            save_kwargs = {"format": save_format}
            if save_format == "JPEG":
                save_kwargs["quality"] = 90
                save_kwargs["optimize"] = True

            cropped.save(output, **save_kwargs)
            output.seek(0)

            content_type_map = {
                "JPEG": "image/jpeg",
                "JPG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }
            content_type = content_type_map.get(save_format, uploaded_file.content_type or "image/jpeg")

            return InMemoryUploadedFile(
                file=output,
                field_name=getattr(uploaded_file, "field_name", None),
                name=uploaded_file.name,
                content_type=content_type,
                size=output.getbuffer().nbytes,
                charset=None,
            )
    except Exception:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
        return uploaded_file


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


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


def _get_index_filters(request):
    source = request.POST if request.method == "POST" else request.GET
    active_tab = (source.get("tab") or "todos").strip()
    if active_tab not in {"todos", "meus"}:
        active_tab = "todos"
    q = (source.get("q") or "").strip()
    category_id = (source.get("category") or "").strip()
    return active_tab, q, category_id


def _build_marketplace_index_context(producer, *, active_tab, q, category_id):
    public_listings = get_public_listings(
        producer=producer,
        q=q,
        category_id=category_id,
    )
    my_listings = get_my_listings(
        producer=producer,
        q=q,
        category_id=category_id,
    ) if producer else MarketplaceListing.objects.none()

    categories_source = (
        get_my_listings(producer=producer, q=q, category_id="")
        if active_tab == "meus" and producer
        else get_public_listings(producer=producer, q=q, category_id="")
    )
    available_categories = list(get_listing_categories_for_queryset(categories_source))

    if category_id and all(str(category.id) != category_id for category in available_categories):
        selected_public = (
            get_public_listings(producer=producer, q="", category_id=category_id)
            .exclude(product__category_id__isnull=True)
            .first()
        )
        selected_private = (
            get_my_listings(producer=producer, q="", category_id=category_id)
            .exclude(product__category_id__isnull=True)
            .first()
            if producer else None
        )
        selected_listing = selected_private or selected_public
        if selected_listing and selected_listing.product and selected_listing.product.category:
            available_categories.append(selected_listing.product.category)

    public_listings = _attach_listing_photo_urls(public_listings)
    my_listings = _attach_listing_photo_urls(my_listings)

    return {
        "page_title": "Marketplace",
        "active_tab": active_tab,
        "q": q,
        "selected_category_id": category_id,
        "listings": public_listings,
        "my_listings": my_listings,
        "available_categories": available_categories,
        "can_publish": bool(producer),
    }


def _build_listing_purchase_quote(listing, raw_quantity=None):
    default_quantity = Decimal("100")
    has_user_quantity_input = raw_quantity not in (None, "")
    parsed_quantity = None

    if has_user_quantity_input:
        try:
            parsed_quantity = Decimal(str(raw_quantity).strip())
        except Exception:
            parsed_quantity = None
    else:
        parsed_quantity = default_quantity

    invalid_quantity_input = parsed_quantity is None
    quantity = parsed_quantity if parsed_quantity is not None else Decimal("1")
    max_quantity = Decimal(str(listing.quantity_available or 0))
    is_quantity_clamped = False

    if max_quantity <= 0:
        if quantity != Decimal("0"):
            is_quantity_clamped = True
        quantity = Decimal("0")
    else:
        if quantity < Decimal("1"):
            quantity = Decimal("1")
            is_quantity_clamped = True
        if quantity > max_quantity:
            quantity = max_quantity
            is_quantity_clamped = True

    if invalid_quantity_input and has_user_quantity_input:
        is_quantity_clamped = True

    total = quantity * Decimal(str(listing.unit_price or 0))

    return {
        "quantity": quantity,
        "max_quantity": max_quantity,
        "total": total,
        "is_quantity_clamped": is_quantity_clamped,
        "has_user_quantity_input": has_user_quantity_input,
    }


def _build_marketplace_detail_context(request, listing, producer):
    quote = _build_listing_purchase_quote(
        listing,
        raw_quantity=request.GET.get("qty"),
    )

    producer_name = get_producer_display_name(listing.producer)
    producer_initials = get_producer_initials(listing.producer)
    producer_location = get_producer_location(listing.producer)
    delivery_text = build_delivery_text(listing)
    producer_product = ProducerProduct.objects.filter(
        producer_id=listing.producer_id,
        product_id=listing.product_id,
    ).first()
    producer_product_description = (
        producer_product.producer_description
        if producer_product
        else None
    )
    detail_description = _first_non_empty_text(
        listing.notes,
        producer_product_description,
        getattr(listing.product, "description", None),
    ) or "Sem descrição disponível para este anúncio."

    producer_member_since = None
    producer_user = getattr(listing.producer, "user", None)
    if producer_user and getattr(producer_user, "created_at", None):
        producer_member_since = producer_user.created_at.year

    is_owner_listing = bool(producer and listing.producer_id == producer.id)
    is_admin_user = getattr(request.current_user, "role", None) == UserRole.ADMIN
    show_buybox = is_owner_listing or not is_admin_user
    expires_at_local = None
    if listing.expires_at:
        expires_at_local = timezone.localtime(listing.expires_at)

    has_stock_source = bool(listing.stock_id)
    has_forecast_source = bool(listing.forecast_id)
    if has_forecast_source and not has_stock_source:
        listing_source_key = LISTING_SOURCE_FORECAST
        listing_source_label = "Pré-venda"
        listing_source_badge_class = "mkd-badge--forecast"
        forecast_period_text = None
        if listing.forecast:
            local_start = (
                timezone.localtime(listing.forecast.period_start)
                if listing.forecast.period_start and timezone.is_aware(listing.forecast.period_start)
                else listing.forecast.period_start
            )
            local_end = (
                timezone.localtime(listing.forecast.period_end)
                if listing.forecast.period_end and timezone.is_aware(listing.forecast.period_end)
                else listing.forecast.period_end
            )
            if listing.forecast.period_start and listing.forecast.period_end:
                forecast_period_text = (
                    f"{local_start.strftime('%d/%m/%Y')} - "
                    f"{local_end.strftime('%d/%m/%Y')}"
                )
            elif listing.forecast.period_start:
                forecast_period_text = (
                    f"A partir de {local_start.strftime('%d/%m/%Y')}"
                )
    else:
        listing_source_key = LISTING_SOURCE_STOCK
        listing_source_label = "Disponível agora"
        listing_source_badge_class = "mkd-badge--stock"
        forecast_period_text = None

    return {
        "page_title": "Detalhe do Produto",
        "listing": listing,
        "listing_photo_url": _listing_photo_url(listing.photo_path),
        **quote,
        "producer_name": producer_name,
        "producer_initials": producer_initials,
        "producer_location": producer_location,
        "delivery_text": delivery_text,
        "detail_description": detail_description,
        "producer_member_since": producer_member_since,
        "is_owner_listing": is_owner_listing,
        "is_admin_user": is_admin_user,
        "show_buybox": show_buybox,
        "expires_at_local": expires_at_local,
        "listing_source_key": listing_source_key,
        "listing_source_label": listing_source_label,
        "listing_source_badge_class": listing_source_badge_class,
        "forecast_period_text": forecast_period_text,
    }


@login_required
def marketplace_index_view(request):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)
    expire_due_active_listings()
    active_tab, q, category_id = _get_index_filters(request)
    context = _build_marketplace_index_context(
        producer,
        active_tab=active_tab,
        q=q,
        category_id=category_id,
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


@login_required
@client_only_required
def marketplace_publish_view(request):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)

    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    success = request.GET.get("success") == "1"
    created_listing_id = request.GET.get("listing_id")
    requested_product_id = (request.GET.get("product") or "").strip()
    requested_source = (request.GET.get("source") or LISTING_SOURCE_STOCK).strip().lower()
    requested_forecast_id = (request.GET.get("forecast") or "").strip()
    prefill_origin = (request.GET.get("from") or "").strip().lower()
    forecast_quantity_limit = None

    is_forecast_prefill_flow = (
        requested_source == LISTING_SOURCE_FORECAST
        and bool(requested_product_id)
        and bool(requested_forecast_id)
    )
    is_inventory_stock_prefill_flow = (
        prefill_origin == "inventory"
        and requested_source == LISTING_SOURCE_STOCK
        and bool(requested_product_id)
    )
    should_lock_origin_and_product = (
        is_forecast_prefill_flow or is_inventory_stock_prefill_flow
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

    form_initial = {}
    if requested_product_id:
        form_initial["product"] = requested_product_id
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
        lock_listing_source=should_lock_origin_and_product,
        lock_product=should_lock_origin_and_product,
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

    if is_inventory_stock_prefill_flow:
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
                notes=form.cleaned_data.get("notes"),
                photo_path=photo_path,
                status=form.cleaned_data.get("status"),
                expires_at=form.cleaned_data.get("expires_at_final"),
                listing_source=listing_source,
                forecast=selected_forecast,
            )
        except MarketplaceServiceError as exc:
            _delete_uploaded_file(photo_path)
            form.add_error(None, str(exc))
        except Exception:
            _delete_uploaded_file(photo_path)
            form.add_error(None, "Não foi possível guardar a foto do anúncio.")
        else:
            messages.success(request, "Anúncio publicado com sucesso.")
            url = reverse("marketplace:publish")
            return redirect(f"{url}?success=1&listing_id={listing.id}")

    context = {
        "page_title": "Publicar Excedente",
        "success": success,
        "form": form,
        "created_listing_id": created_listing_id,
        "publishable_summary": publishable_summary,
        "forecast_quantity_limit": forecast_quantity_limit,
        "selected_product_id": selected_product_id,
        "selected_source": selected_source,
        "initial_market_trend": initial_market_trend,
        "is_inventory_stock_prefill_flow": is_inventory_stock_prefill_flow,
    }
    return render(request, "marketplace/publish.html", context)


@login_required
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
                notes=form.cleaned_data.get("notes"),
                status=form.cleaned_data["status"],
                expires_at=form.cleaned_data.get("expires_at_final"),
                photo_path=new_photo_path if uploaded_photo else listing.photo_path,
            )
        except MarketplaceServiceError as exc:
            _delete_uploaded_file(new_photo_path)
            form.add_error(None, str(exc))
        except Exception:
            _delete_uploaded_file(new_photo_path)
            form.add_error(None, "Não foi possível atualizar o anúncio.")
        else:
            if new_photo_path and old_photo_path and old_photo_path != new_photo_path:
                _delete_uploaded_file(old_photo_path)
            messages.success(request, "Anúncio atualizado com sucesso.")
            return redirect(f"{reverse('marketplace:index')}?tab=meus")

    context = {
        "page_title": "Editar Anúncio",
        "listing": listing,
        "form": form,
        "current_photo_url": current_photo_url,
    }
    return render(request, "marketplace/edit.html", context)


@login_required
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
    active_tab, q, category_id = _get_index_filters(request)

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
            )
            return render(request, "marketplace/index.html", context)

        next_url = (request.POST.get("next") or "").strip()
        if next_url:
            return redirect(next_url)
        return redirect("marketplace:edit", listing_id=listing.id)

    photo_path = listing.photo_path
    listing.delete()
    _delete_uploaded_file(photo_path)

    messages.success(request, "Anúncio eliminado com sucesso.")
    if _is_htmx(request):
        context = _build_marketplace_index_context(
            producer,
            active_tab=active_tab,
            q=q,
            category_id=category_id,
        )
        return render(request, "marketplace/index.html", context)

    return redirect(f"{reverse('marketplace:index')}?tab=meus")


@login_required
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

    now = timezone.now()
    if listing.status == ListingStatus.ACTIVE:
        listing.status = ListingStatus.CANCELLED
        feedback = "Anúncio desativado com sucesso."
    else:
        listing.status = ListingStatus.ACTIVE
        if listing.expires_at and listing.expires_at <= now:
            listing.expires_at = None
        feedback = "Anúncio ativado com sucesso."

    listing.updated_at = now
    listing.save(update_fields=["status", "expires_at", "updated_at"])
    messages.success(request, feedback)

    next_url = (request.POST.get("next") or "").strip()
    if next_url and not _is_htmx(request):
        return redirect(next_url)

    if _is_htmx(request) and (request.POST.get("source") or "") == "detail":
        detail_listing = get_object_or_404(
            get_listing_detail_queryset(producer=producer),
            id=listing_id,
        )
        detail_context = _build_marketplace_detail_context(request, detail_listing, producer)
        return render(request, "marketplace/detail.html", detail_context)

    active_tab, q, category_id = _get_index_filters(request)
    context = _build_marketplace_index_context(
        producer,
        active_tab=active_tab,
        q=q,
        category_id=category_id,
    )

    if _is_htmx(request):
        return render(request, "marketplace/index.html", context)

    query = urlencode({"tab": active_tab, "q": q, "category": category_id})
    return redirect(f"{reverse('marketplace:index')}?{query}")
