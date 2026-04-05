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
from apps.inventory.models import ProducerProduct
from apps.marketplace.forms import MarketplacePublishForm, MarketplaceEditForm
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.marketplace.services import (
    MarketplaceServiceError,
    build_delivery_text,
    create_listing,
    expire_due_active_listings,
    get_current_producer_for_user,
    get_listing_categories_for_queryset,
    get_listing_detail_queryset,
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


def _build_marketplace_detail_context(request, listing, producer):
    try:
        quantity = Decimal(request.GET.get("qty", "100"))
    except Exception:
        quantity = Decimal("100")

    max_quantity = Decimal(str(listing.quantity_available or 0))
    if max_quantity <= 0:
        quantity = Decimal("0")
    else:
        if quantity < Decimal("1"):
            quantity = Decimal("1")
        if quantity > max_quantity:
            quantity = max_quantity

    total = quantity * Decimal(str(listing.unit_price))

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
    expires_at_local = None
    if listing.expires_at:
        expires_at_local = timezone.localtime(listing.expires_at)

    return {
        "page_title": "Detalhe do Produto",
        "listing": listing,
        "listing_photo_url": _listing_photo_url(listing.photo_path),
        "quantity": quantity,
        "total": total,
        "producer_name": producer_name,
        "producer_initials": producer_initials,
        "producer_location": producer_location,
        "delivery_text": delivery_text,
        "detail_description": detail_description,
        "producer_member_since": producer_member_since,
        "is_owner_listing": is_owner_listing,
        "expires_at_local": expires_at_local,
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
@client_only_required
def marketplace_publish_view(request):
    current_user = request.current_user
    producer = get_current_producer_for_user(current_user)

    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    success = request.GET.get("success") == "1"
    created_listing_id = request.GET.get("listing_id")

    form = MarketplacePublishForm(request.POST or None, request.FILES or None, producer=producer)
    publishable_summary = get_publishable_products_summary(producer)

    if request.method == "POST" and form.is_valid():
        uploaded_photo = request.FILES.get("photo")
        photo_path = None

        try:
            if uploaded_photo:
                photo_path = _save_listing_photo(producer, uploaded_photo)

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
        MarketplaceListing.objects.select_related("product", "stock", "producer"),
        id=listing_id,
        producer=producer,
    )

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
