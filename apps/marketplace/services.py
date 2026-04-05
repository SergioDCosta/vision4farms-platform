from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.catalog.models import Product, ProductCategory
from apps.inventory.models import ProducerProfile, ProducerProduct, Stock
from apps.marketplace.models import MarketplaceListing, ListingStatus, DeliveryMode


QTY_DECIMAL = Decimal("0.001")


class MarketplaceServiceError(Exception):
    pass


def quantize_qty(value):
    return Decimal(str(value)).quantize(QTY_DECIMAL)


def get_current_producer_for_user(user):
    if not user:
        return None
    return ProducerProfile.objects.filter(user=user).first()


def get_producer_display_name(producer):
    if not producer:
        return "Produtor"

    if getattr(producer, "display_name", None):
        return producer.display_name

    if getattr(producer, "company_name", None):
        return producer.company_name

    user = getattr(producer, "user", None)
    if user:
        full_name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
        if full_name:
            return full_name
        return user.email

    return "Produtor"


def get_producer_initials(producer):
    name = get_producer_display_name(producer)
    parts = [p for p in name.split() if p.strip()]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    if parts:
        return parts[0][:2].upper()
    return "PR"


def get_producer_location(producer):
    if not producer:
        return "Localização não indicada"

    city = (getattr(producer, "city", None) or "").strip()
    district = (getattr(producer, "district", None) or "").strip()

    if city and district:
        return f"{city}, {district}"
    if city:
        return city
    if district:
        return district

    return "Localização não indicada"


def build_delivery_text(listing):
    mode = listing.delivery_mode
    radius = listing.delivery_radius_km
    fee = listing.delivery_fee

    if mode == DeliveryMode.PICKUP:
        return "Levantamento na exploração."

    if mode == DeliveryMode.DELIVERY:
        parts = ["Entrega disponível"]
        if radius:
            parts.append(f"num raio de {radius} km")
        if fee is not None:
            parts.append(f"(taxa adicional de {fee}€)")
        return " ".join(parts) + "."

    if mode == DeliveryMode.BOTH:
        parts = ["Levantamento na exploração ou entrega disponível"]
        if radius:
            parts.append(f"num raio de {radius} km")
        if fee is not None:
            parts.append(f"(taxa adicional de {fee}€)")
        return " ".join(parts) + "."

    return "Condições de entrega a combinar com o produtor."


def get_base_listing_queryset():
    return (
        MarketplaceListing.objects
        .select_related("producer", "producer__user", "product", "stock")
        .order_by("-published_at", "-created_at")
    )


def expire_due_active_listings():
    now = timezone.now()
    MarketplaceListing.objects.filter(
        status=ListingStatus.ACTIVE,
        expires_at__isnull=False,
        expires_at__lte=now,
    ).update(
        status=ListingStatus.EXPIRED,
        updated_at=now,
    )


def get_public_listings(*, producer=None, q="", category_id=""):
    qs = get_base_listing_queryset().filter(
        status=ListingStatus.ACTIVE,
        quantity_available__gt=0,
    )

    if producer:
        qs = qs.exclude(producer=producer)

    if q:
        q = q.strip()
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(producer__display_name__icontains=q)
            | Q(producer__company_name__icontains=q)
            | Q(producer__user__first_name__icontains=q)
            | Q(producer__user__last_name__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    return qs


def get_my_listings(*, producer, q="", category_id=""):
    qs = get_base_listing_queryset().filter(producer=producer)

    if q:
        q = q.strip()
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    return qs


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
        return qs.filter(Q(status=ListingStatus.ACTIVE) | Q(producer=producer))

    return qs.filter(status=ListingStatus.ACTIVE)


def get_producer_products(producer):
    product_ids = ProducerProduct.objects.filter(
        producer=producer,
        is_active=True,
    ).values_list("product_id", flat=True)

    return Product.objects.filter(
        id__in=product_ids,
        is_active=True,
    ).order_by("name")


def get_stock_for_product(producer, product):
    return Stock.objects.filter(producer=producer, product=product).first()


def get_stock_available_quantity(stock):
    if not stock:
        return Decimal("0.000")

    current_quantity = Decimal(str(stock.current_quantity or 0))
    reserved_quantity = Decimal(str(stock.reserved_quantity or 0))
    return quantize_qty(current_quantity - reserved_quantity)


def get_max_publishable_quantity(stock):
    """
    Regra v1:
    excedente publicável = current_quantity - reserved_quantity - minimum_threshold
    """
    if not stock:
        return Decimal("0.000")

    current_quantity = Decimal(str(stock.current_quantity or 0))
    reserved_quantity = Decimal(str(stock.reserved_quantity or 0))
    minimum_threshold = Decimal(str(stock.minimum_threshold or 0))

    publishable = current_quantity - reserved_quantity - minimum_threshold
    if publishable < 0:
        publishable = Decimal("0.000")

    return quantize_qty(publishable)


def get_publishable_products(producer):
    product_ids = []

    stocks = (
        Stock.objects
        .select_related("product")
        .filter(producer=producer, product__is_active=True)
        .order_by("product__name")
    )

    for stock in stocks:
        if get_max_publishable_quantity(stock) > 0:
            product_ids.append(stock.product_id)

    return Product.objects.filter(id__in=product_ids, is_active=True).order_by("name")


def get_publishable_products_summary(producer):
    rows = []
    if not producer:
        return rows

    stocks = (
        Stock.objects.select_related("product", "product__category")
        .filter(producer=producer, product__is_active=True)
        .order_by("product__name")
    )

    for stock in stocks:
        publishable_quantity = get_max_publishable_quantity(stock)
        if publishable_quantity <= 0:
            continue

        current_quantity = quantize_qty(stock.current_quantity or 0)
        reserved_quantity = quantize_qty(stock.reserved_quantity or 0)

        rows.append({
            "product": stock.product,
            "category_name": stock.product.category.name if stock.product.category else "Sem categoria",
            "current_quantity": current_quantity,
            "reserved_quantity": reserved_quantity,
            "publishable_quantity": publishable_quantity,
        })

    return rows


def create_listing(
    *,
    producer,
    product,
    quantity,
    unit_price,
    delivery_mode,
    delivery_radius_km=None,
    delivery_fee=None,
    notes=None,
    photo_path=None,
    status=ListingStatus.ACTIVE,
    expires_at=None,
):
    stock = get_stock_for_product(producer, product)
    max_publishable = get_max_publishable_quantity(stock)

    quantity = Decimal(str(quantity))
    unit_price = Decimal(str(unit_price))
    now = timezone.now()

    if quantity <= 0:
        raise MarketplaceServiceError("A quantidade tem de ser superior a zero.")

    if unit_price <= 0:
        raise MarketplaceServiceError("O preço tem de ser superior a zero.")

    if status == ListingStatus.ACTIVE and expires_at and expires_at <= now:
        raise MarketplaceServiceError("Para manter ativo, a data de expiração deve ser no futuro.")

    if max_publishable <= 0:
        raise MarketplaceServiceError("Este produto não tem excedente disponível para publicar.")

    if quantity > max_publishable:
        raise MarketplaceServiceError(
            f"A quantidade excede o máximo publicável ({max_publishable} {product.unit})."
        )

    if delivery_mode == DeliveryMode.PICKUP:
        delivery_radius_km = None
        delivery_fee = None

    if status == ListingStatus.EXPIRED and not expires_at:
        expires_at = now

    return MarketplaceListing.objects.create(
        producer=producer,
        product=product,
        stock=stock,
        quantity_total=quantity,
        quantity_available=quantity,
        quantity_reserved=Decimal("0.000"),
        unit_price=unit_price,
        delivery_mode=delivery_mode,
        delivery_radius_km=delivery_radius_km,
        delivery_fee=delivery_fee,
        notes=notes or None,
        photo_path=photo_path or None,
        status=status or ListingStatus.ACTIVE,
        expires_at=expires_at,
        published_at=now,
    )


def update_listing(
    *,
    listing,
    quantity_total,
    unit_price,
    delivery_mode,
    delivery_radius_km=None,
    delivery_fee=None,
    notes=None,
    status=ListingStatus.ACTIVE,
    expires_at=None,
    photo_path=None,
):
    quantity_total = Decimal(str(quantity_total))
    unit_price = Decimal(str(unit_price))
    reserved_quantity = Decimal(str(listing.quantity_reserved or 0))
    now = timezone.now()

    if quantity_total <= 0:
        raise MarketplaceServiceError("A quantidade listada deve ser superior a zero.")

    if quantity_total < reserved_quantity:
        raise MarketplaceServiceError(
            f"A quantidade listada não pode ser inferior à reservada ({reserved_quantity})."
        )

    if unit_price <= 0:
        raise MarketplaceServiceError("O preço tem de ser superior a zero.")

    if status == ListingStatus.ACTIVE and expires_at and expires_at <= now:
        raise MarketplaceServiceError("Para manter ativo, a data de expiração deve ser no futuro.")

    if delivery_mode == DeliveryMode.PICKUP:
        delivery_radius_km = None
        delivery_fee = None

    quantity_available = quantity_total - reserved_quantity

    if status == ListingStatus.EXPIRED and not expires_at:
        expires_at = now

    listing.quantity_total = quantity_total
    listing.quantity_available = quantity_available
    listing.unit_price = unit_price
    listing.delivery_mode = delivery_mode
    listing.delivery_radius_km = delivery_radius_km
    listing.delivery_fee = delivery_fee
    listing.notes = notes or None
    listing.status = status
    listing.expires_at = expires_at
    if photo_path is not None:
        listing.photo_path = photo_path
    listing.updated_at = now
    listing.save(
        update_fields=[
            "quantity_total",
            "quantity_available",
            "unit_price",
            "delivery_mode",
            "delivery_radius_km",
            "delivery_fee",
            "notes",
            "status",
            "expires_at",
            "photo_path",
            "updated_at",
        ]
    )
    return listing
