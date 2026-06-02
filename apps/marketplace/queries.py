from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.catalog.models import ProductCategory
from apps.marketplace.availability import _valid_listing_source_filter
from apps.marketplace.constants import (
    LISTING_SOURCE_FORECAST,
    LISTING_SOURCE_STOCK,
    MARKETPLACE_EDITABLE_STATUSES,
    MARKETPLACE_FINAL_STATUSES,
)
from apps.marketplace.models import ListingStatus, MarketplaceListing


def get_base_listing_queryset():
    return (
        MarketplaceListing.objects
        .select_related(
            "producer",
            "producer__user",
            "product",
            "stock",
            "forecast",
            "need",
            "need__producer",
            "need__producer__user",
        )
        .filter(_valid_listing_source_filter())
        .order_by("-published_at", "-created_at")
    )


def _retired_listing_filter():
    """Identify removed listings while keeping merely disabled listings manageable."""
    return (
        Q(status=ListingStatus.CANCELLED, quantity_available__lte=0)
        & (
            Q(expires_at__isnull=False, expires_at__lte=timezone.now())
            | Q(photo_path__isnull=True)
            | Q(photo_path="")
        )
    )


def is_listing_retired_in_marketplace(listing):
    if not listing or getattr(listing, "need_id", None):
        return False
    if getattr(listing, "status", None) != ListingStatus.CANCELLED:
        return False
    if Decimal(str(getattr(listing, "quantity_available", 0) or 0)) > 0:
        return False
    expires_at = getattr(listing, "expires_at", None)
    is_expired_marker = bool(expires_at and expires_at <= timezone.now())
    has_no_photo = not getattr(listing, "photo_path", None)
    return is_expired_marker or has_no_photo


def is_listing_editable_in_marketplace(listing):
    if not listing or getattr(listing, "need_id", None):
        return False
    if is_listing_retired_in_marketplace(listing):
        return False
    return getattr(listing, "status", None) in MARKETPLACE_EDITABLE_STATUSES


def is_listing_toggleable_in_marketplace(listing):
    if not listing or getattr(listing, "need_id", None):
        return False
    if is_listing_retired_in_marketplace(listing):
        return False
    return getattr(listing, "status", None) in MARKETPLACE_EDITABLE_STATUSES


def is_listing_retirable_in_marketplace(listing):
    if not listing or getattr(listing, "need_id", None):
        return False
    if is_listing_retired_in_marketplace(listing):
        return False
    if getattr(listing, "status", None) in MARKETPLACE_FINAL_STATUSES:
        return False
    return Decimal(str(getattr(listing, "quantity_reserved", 0) or 0)) <= 0


def _apply_listing_filters(qs, *, q="", category_id="", origin="", only_available=False):
    if q:
        q = q.strip()
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(notes__icontains=q)
            | Q(producer__display_name__icontains=q)
            | Q(producer__company_name__icontains=q)
            | Q(producer__city__icontains=q)
            | Q(producer__district__icontains=q)
            | Q(producer__user__first_name__icontains=q)
            | Q(producer__user__last_name__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    if origin == LISTING_SOURCE_STOCK:
        qs = qs.filter(stock_id__isnull=False, forecast_id__isnull=True)
    elif origin == LISTING_SOURCE_FORECAST:
        qs = qs.filter(stock_id__isnull=True, forecast_id__isnull=False)

    if only_available:
        qs = qs.filter(status=ListingStatus.ACTIVE, quantity_available__gt=0)

    return qs


def _apply_listing_sort(qs, *, sort="recent"):
    if sort == "price_asc":
        return qs.order_by("unit_price", "-published_at", "-created_at")
    if sort == "price_desc":
        return qs.order_by("-unit_price", "-published_at", "-created_at")
    if sort == "quantity_desc":
        return qs.order_by("-quantity_available", "-published_at", "-created_at")
    return qs.order_by("-published_at", "-created_at")


def get_public_listings(*, producer=None, q="", category_id="", origin="", sort="recent", only_available=True):
    # Os anúncios do próprio produtor também aparecem no feed geral: o cartão
    # distingue-os com "O seu anúncio" e a guarda de compra impede comprá-los.
    qs = get_base_listing_queryset().filter(
        status=ListingStatus.ACTIVE,
        quantity_available__gt=0,
        need_id__isnull=True,
        product__is_active=True,
    )

    qs = _apply_listing_filters(
        qs,
        q=q,
        category_id=category_id,
        origin=origin,
        only_available=only_available,
    )
    return _apply_listing_sort(qs, sort=sort)


def get_my_listings(*, producer, q="", category_id="", origin="", sort="recent", only_available=False):
    qs = (
        get_base_listing_queryset()
        .filter(producer=producer, need_id__isnull=True)
        .exclude(_retired_listing_filter())
    )
    qs = _apply_listing_filters(
        qs,
        q=q,
        category_id=category_id,
        origin=origin,
        only_available=only_available,
    )
    return _apply_listing_sort(qs, sort=sort)


def get_listing_categories_for_queryset(listings_qs):
    category_ids = (
        listings_qs.exclude(product__category_id__isnull=True)
        .values_list("product__category_id", flat=True)
        .distinct()
    )

    return ProductCategory.objects.filter(id__in=category_ids).order_by("name")


def get_listing_detail_queryset(*, producer=None):
    qs = get_base_listing_queryset()

    if producer:
        return qs.filter(
            Q(
                need_id__isnull=True,
                status=ListingStatus.ACTIVE,
                quantity_available__gt=0,
                product__is_active=True,
            )
            | (Q(producer=producer) & ~_retired_listing_filter())
            | Q(need__producer=producer)
        )

    return qs.filter(
        need_id__isnull=True,
        status=ListingStatus.ACTIVE,
        quantity_available__gt=0,
        product__is_active=True,
    )
