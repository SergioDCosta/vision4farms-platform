from datetime import timedelta
from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils.text import slugify
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from apps.catalog.models import Product, ProductCategory
from apps.inventory.models import (
    ForecastSourceSystem,
    ProductionForecast,
    ProducerProduct,
    ProducerProfile,
    Stock,
    StockMovement,
    StockMovementType,
)
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.orders.models import Order, OrderItem, OrderStatusHistory, OrderStatus


ZERO = Decimal("0.00")
IN_PROGRESS_ORDER_STATUSES = ["CONFIRMED", "IN_PROGRESS", "DELIVERING"]
COMPLETED_ORDER_STATUS = "COMPLETED"


# ---------------------------------------------------------------------------
# Perfil do produtor
# ---------------------------------------------------------------------------

def get_producer_profile(user_id):
    try:
        return ProducerProfile.objects.get(user_id=user_id)
    except ProducerProfile.DoesNotExist:
        return None


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _month_floor(dt):
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _shift_month(dt, delta_months):
    month_index = dt.month - 1 + delta_months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    return dt.replace(year=year, month=month, day=1)


def _to_decimal(value):
    return value if value is not None else ZERO


def _format_qty(value):
    decimal_value = Decimal(str(value or 0)).quantize(Decimal("0.001"))
    formatted = format(decimal_value, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


def _stock_state(stock):
    """
    Estado visual do stock:
    - critical: available_quantity <= safety_stock
    - normal: available_quantity > safety_stock e real_surplus < surplus_threshold
    - excess: real_surplus >= surplus_threshold
    """
    current_quantity = stock.current_quantity if stock else ZERO
    safety_stock = stock.safety_stock if stock else ZERO
    reserved_quantity = stock.reserved_quantity if stock else ZERO
    surplus_threshold = stock.surplus_threshold if stock and stock.surplus_threshold is not None else ZERO
    available_quantity = current_quantity - reserved_quantity

    real_surplus = max(available_quantity - safety_stock, ZERO)
    publishable_quantity = real_surplus

    if available_quantity <= safety_stock:
        return {
            "key": "critical",
            "label": "Crítico",
            "row_class": "inv-row--critical",
            "pill_class": "inv-status inv-status--critical",
            "text_class": "inv-value inv-value--critical",
            "publishable_quantity": ZERO,
            "surplus_threshold": surplus_threshold,
            "real_surplus": real_surplus,
            "action_type": "recommend",
            "action_label": "Comprar",
            "action_icon": "cart",
            "action_url": "/recomendacoes/",
        }

    if real_surplus >= surplus_threshold:
        return {
            "key": "excess",
            "label": "Excedente",
            "row_class": "inv-row--excess",
            "pill_class": "inv-status inv-status--excess",
            "text_class": "inv-value inv-value--excess",
            "publishable_quantity": publishable_quantity,
            "surplus_threshold": surplus_threshold,
            "real_surplus": real_surplus,
            "action_type": "publish",
            "action_label": "Publicar",
            "action_icon": "storefront",
            "action_url": "/marketplace/",
        }

    return {
        "key": "normal",
        "label": "Normal",
        "row_class": "",
        "pill_class": "inv-status inv-status--normal",
        "text_class": "inv-value",
        "publishable_quantity": ZERO,
        "surplus_threshold": surplus_threshold,
        "real_surplus": real_surplus,
        "action_type": "marketplace",
        "action_label": "Marketplace",
        "action_icon": "shop",
        "action_url": "/marketplace/",
    }


# ---------------------------------------------------------------------------
# Produtos do produtor / inventário operacional
# ---------------------------------------------------------------------------
ZERO = Decimal("0")


def _normalize_text(value):
    return " ".join((value or "").split()).strip()


def _normalize_optional_text(value):
    if value is None:
        return None
    normalized = _normalize_text(value)
    return normalized or None


def _build_unique_slug(base_slug):
    slug = base_slug or "produto"
    candidate = slug
    counter = 2

    while Product.objects.filter(slug=candidate).exists():
        candidate = f"{slug}-{counter}"
        counter += 1

    return candidate


def _build_category_groups(rows):
    grouped = {}

    for row in rows:
        category_name = (
            getattr(getattr(row.get("product"), "category", None), "name", None)
            or "Sem categoria"
        ).strip()
        normalized_name = category_name or "Sem categoria"

        key = normalized_name.lower()
        if key not in grouped:
            grouped[key] = {
                "name": normalized_name,
                "rows": [],
            }

        grouped[key]["rows"].append(row)

    ordered_groups = sorted(
        grouped.values(),
        key=lambda group: group["name"].lower(),
    )

    for group in ordered_groups:
        group["count"] = len(group["rows"])

    return ordered_groups


def _ensure_stock_for_product(
    producer,
    product,
    initial_quantity,
    safety_stock,
    surplus_threshold,
    user,
):
    """
    Garante o registo de stock para produtor+produto.
    Se o stock ainda não existir, cria-o.
    Se existir e estiver a zero, pode aplicar stock inicial.
    """
    initial_quantity = initial_quantity or ZERO
    safety_stock = safety_stock or ZERO
    surplus_threshold = surplus_threshold or ZERO

    stock, stock_created = Stock.objects.get_or_create(
        producer=producer,
        product=product,
        defaults={
            "current_quantity": initial_quantity,
            "reserved_quantity": ZERO,
            "safety_stock": safety_stock,
            "surplus_threshold": surplus_threshold,
            "updated_by": user,
            "last_updated_at": timezone.now(),
        },
    )

    if stock_created:
        if initial_quantity > ZERO:
            StockMovement.objects.create(
                stock=stock,
                movement_type=StockMovementType.IMPORT,
                quantity_delta=initial_quantity,
                reference_type="MANUAL",
                notes="Stock inicial definido ao adicionar produto.",
                performed_by=user,
            )
        return stock

    changed_fields = []

    if stock.safety_stock != safety_stock:
        stock.safety_stock = safety_stock
        changed_fields.append("safety_stock")

    current_surplus_threshold = getattr(stock, "surplus_threshold", ZERO)
    if current_surplus_threshold is None:
        current_surplus_threshold = ZERO

    if current_surplus_threshold != surplus_threshold:
        stock.surplus_threshold = surplus_threshold
        changed_fields.append("surplus_threshold")

    if stock.current_quantity == ZERO and initial_quantity > ZERO:
        stock.current_quantity = initial_quantity
        changed_fields.append("current_quantity")

        StockMovement.objects.create(
            stock=stock,
            movement_type=StockMovementType.IMPORT,
            quantity_delta=initial_quantity,
            reference_type="MANUAL",
            notes="Stock inicial definido ao associar produto existente.",
            performed_by=user,
        )

    if changed_fields:
        stock.updated_by = user
        stock.last_updated_at = timezone.now()
        changed_fields.extend(["updated_by", "last_updated_at", "updated_at"])
        stock.save(update_fields=changed_fields)

    return stock


def get_available_products_to_add(producer):
    """
    Devolve produtos do catálogo que o produtor ainda NÃO tem ativos.
    Produtos já associados mas inativos podem voltar a aparecer para reativação.
    """
    active_product_ids = ProducerProduct.objects.filter(
        producer=producer,
        is_active=True,
    ).values_list("product_id", flat=True)

    return (
        Product.objects
        .filter(is_active=True)
        .exclude(id__in=active_product_ids)
        .select_related("category")
        .order_by("category__name", "name")
    )


def get_stock_dashboard(producer, q="", sort="name"):
    valid_sort_options = {"name", "stock_desc", "stock_asc", "state"}
    sort = (sort or "name").strip().lower()
    if sort not in valid_sort_options:
        sort = "name"

    producer_products_qs = (
        ProducerProduct.objects
        .filter(producer=producer, is_active=True)
        .select_related("product", "product__category")
        .order_by("product__name")
    )

    if q:
        producer_products_qs = producer_products_qs.filter(
            Q(product__name__icontains=q)
            | Q(product__slug__icontains=q)
            | Q(product__category__name__icontains=q)
            | Q(product__unit__icontains=q)
        )

    producer_products = list(producer_products_qs)

    product_ids = [pp.product_id for pp in producer_products]
    stocks_by_product_id = {
        stock.product_id: stock
        for stock in Stock.objects.filter(
            producer=producer,
            product_id__in=product_ids,
        ).select_related("product", "product__category")
    }

    rows = []
    critical_count = 0
    excess_count = 0

    for pp in producer_products:
        stock = stocks_by_product_id.get(pp.product_id)
        state = _stock_state(stock)

        if state["key"] == "critical":
            critical_count += 1
        elif state["key"] == "excess":
            excess_count += 1

        rows.append({
            "producer_product": pp,
            "product": pp.product,
            "product_id": pp.product_id,
            "stock": stock,
            "state": state,
        })

    def _row_stock_value(row):
        if row["stock"] and row["stock"].current_quantity is not None:
            return row["stock"].current_quantity
        return ZERO

    if sort == "stock_desc":
        rows.sort(
            key=lambda row: (_row_stock_value(row), row["product"].name.lower()),
            reverse=True,
        )
    elif sort == "stock_asc":
        rows.sort(key=lambda row: (_row_stock_value(row), row["product"].name.lower()))
    elif sort == "state":
        state_priority = {"critical": 0, "normal": 1, "excess": 2}
        rows.sort(
            key=lambda row: (
                state_priority.get(row["state"]["key"], 99),
                -_row_stock_value(row),
                row["product"].name.lower(),
            )
        )
    else:
        rows.sort(key=lambda row: row["product"].name.lower())

    category_groups = _build_category_groups(rows)

    return {
        "rows": rows,
        "category_groups": category_groups,
        "stock_total_count": len(rows),
        "critical_count": critical_count,
        "excess_count": excess_count,
        "q": q,
        "sort": sort,
    }


def get_deactivated_products_dashboard(producer, q=""):
    producer_products_qs = (
        ProducerProduct.objects
        .filter(producer=producer, is_active=False)
        .select_related("product", "product__category")
        .order_by("-updated_at", "product__name")
    )

    if q:
        producer_products_qs = producer_products_qs.filter(
            Q(product__name__icontains=q)
            | Q(product__slug__icontains=q)
            | Q(product__category__name__icontains=q)
            | Q(product__unit__icontains=q)
        )

    rows = []
    for link in producer_products_qs:
        stock = Stock.objects.filter(
            producer=producer,
            product=link.product,
        ).select_related("product", "product__category").first()

        rows.append({
            "producer_product": link,
            "product": link.product,
            "stock": stock,
        })

    category_groups = _build_category_groups(rows)

    return {
        "rows": rows,
        "category_groups": category_groups,
        "deactivated_total_count": len(rows),
        "q": q,
    }


@transaction.atomic
def add_product_to_producer(
    producer,
    product_id,
    initial_quantity,
    safety_stock,
    surplus_threshold,
    user,
    producer_description=None,
):
    """
    Associa um produto do catálogo ao produtor e garante stock.
    Se já existia associação inativa, reativa-a.
    """
    product = Product.objects.get(id=product_id, is_active=True)
    has_producer_description_input = producer_description is not None
    normalized_producer_description = _normalize_optional_text(producer_description)

    defaults = {"is_active": True}
    if has_producer_description_input:
        defaults["producer_description"] = normalized_producer_description

    producer_product, pp_created = ProducerProduct.objects.get_or_create(
        producer=producer,
        product=product,
        defaults=defaults,
    )

    link_created = pp_created
    changed_fields = []
    if not pp_created:
        if not producer_product.is_active:
            producer_product.is_active = True
            changed_fields.append("is_active")
            link_created = True

        if (
            has_producer_description_input
            and producer_product.producer_description != normalized_producer_description
        ):
            producer_product.producer_description = normalized_producer_description
            changed_fields.append("producer_description")

    if changed_fields:
        producer_product.updated_at = timezone.now()
        producer_product.save(update_fields=changed_fields + ["updated_at"])

    stock = _ensure_stock_for_product(
        producer=producer,
        product=product,
        initial_quantity=initial_quantity,
        safety_stock=safety_stock,
        surplus_threshold=surplus_threshold,
        user=user,
    )

    return producer_product, stock, False, link_created

@transaction.atomic
def create_custom_product_for_producer(
    producer,
    category,
    name,
    unit,
    initial_quantity,
    safety_stock,
    surplus_threshold,
    user,
    producer_description=None,
):
    """
    Cria um novo produto no catálogo (se não existir) e associa-o ao produtor.
    Se o produto já existir pelo slug, usa o existente em vez de duplicar.

    - Dados globais: nome/categoria/unidade no Product.
    - Dado específico do produtor: descrição em ProducerProduct.producer_description.
    """
    if not category or not isinstance(category, ProductCategory):
        raise ValidationError("Seleciona uma categoria válida.")

    name = _normalize_text(name)
    unit = _normalize_text(unit)

    has_producer_description_input = producer_description is not None
    normalized_producer_description = _normalize_optional_text(producer_description)

    if not name:
        raise ValidationError("Indica o nome do produto.")

    if not unit:
        raise ValidationError("Indica a unidade do produto.")

    base_slug = slugify(name)
    if not base_slug:
        raise ValidationError("Não foi possível gerar um identificador válido para o produto.")

    existing_product = Product.objects.filter(slug=base_slug).first()

    product_created = False
    if existing_product:
        if not existing_product.is_active:
            raise ValidationError(
                f"Já existe um produto com o nome '{existing_product.name}', mas está inativo."
            )
        product = existing_product
    else:
        product = Product.objects.create(
            category=category,
            name=name,
            slug=_build_unique_slug(base_slug),
            unit=unit,
            description=None,
            is_active=True,
        )
        product_created = True

    pp_defaults = {"is_active": True}
    if has_producer_description_input:
        pp_defaults["producer_description"] = normalized_producer_description

    producer_product, pp_created = ProducerProduct.objects.get_or_create(
        producer=producer,
        product=product,
        defaults=pp_defaults,
    )

    link_created = pp_created
    changed_fields = []
    if not pp_created:
        if not producer_product.is_active:
            producer_product.is_active = True
            changed_fields.append("is_active")
            link_created = True

        if (
            has_producer_description_input
            and producer_product.producer_description != normalized_producer_description
        ):
            producer_product.producer_description = normalized_producer_description
            changed_fields.append("producer_description")

    if changed_fields:
        producer_product.updated_at = timezone.now()
        producer_product.save(update_fields=changed_fields + ["updated_at"])

    stock = _ensure_stock_for_product(
        producer=producer,
        product=product,
        initial_quantity=initial_quantity,
        safety_stock=safety_stock,
        surplus_threshold=surplus_threshold,
        user=user,
    )

    return producer_product, stock, product_created, link_created


@transaction.atomic
def remove_product_from_producer(producer, producer_product_id):
    try:
        producer_product = ProducerProduct.objects.select_related("product").get(
            id=producer_product_id,
            producer=producer,
            is_active=True,
        )
    except ProducerProduct.DoesNotExist:
        return False, "Produto não encontrado."

    producer_product.is_active = False
    producer_product.updated_at = timezone.now()
    producer_product.save(update_fields=["is_active", "updated_at"])

    return True, None


@transaction.atomic
def reactivate_product_from_producer(producer, producer_product_id):
    try:
        producer_product = ProducerProduct.objects.select_related("product").get(
            id=producer_product_id,
            producer=producer,
            is_active=False,
        )
    except ProducerProduct.DoesNotExist:
        return False, "Produto desativado não encontrado."

    producer_product.is_active = True
    producer_product.updated_at = timezone.now()
    producer_product.save(update_fields=["is_active", "updated_at"])

    return True, None


def get_stock_for_product(producer, product_id):
    try:
        return Stock.objects.select_related("product", "product__category").get(
            producer=producer,
            product_id=product_id,
        )
    except Stock.DoesNotExist:
        return None


def get_stock_state(stock):
    return _stock_state(stock)


def _forecast_saleable_quantity(forecast):
    forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    available = forecast_quantity - reserved_quantity
    return max(available, ZERO)


def get_product_forecasts(producer, product_id):
    forecasts = list(
        ProductionForecast.objects
        .filter(producer=producer, product_id=product_id)
        .order_by("-period_start", "-created_at")
    )

    if not forecasts:
        return []

    forecast_ids = [forecast.id for forecast in forecasts]
    listings = (
        MarketplaceListing.objects
        .filter(
            producer=producer,
            product_id=product_id,
            forecast_id__in=forecast_ids,
        )
        .order_by("-published_at", "-created_at")
    )

    active_listing_by_forecast = {}
    latest_listing_by_forecast = {}
    open_published_by_forecast = defaultdict(lambda: ZERO)
    for listing in listings:
        if listing.forecast_id not in latest_listing_by_forecast:
            latest_listing_by_forecast[listing.forecast_id] = listing
        if (
            listing.status == ListingStatus.ACTIVE
            and listing.forecast_id not in active_listing_by_forecast
        ):
            active_listing_by_forecast[listing.forecast_id] = listing
        if listing.status in {ListingStatus.ACTIVE, ListingStatus.RESERVED}:
            open_published_by_forecast[listing.forecast_id] += Decimal(
                str(listing.quantity_available or 0)
            )

    rows = []
    for forecast in forecasts:
        forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
        reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
        available_quantity = forecast_quantity - reserved_quantity
        open_published_quantity = open_published_by_forecast.get(forecast.id, ZERO)
        saleable_quantity = max(available_quantity - open_published_quantity, ZERO)

        linked_listing = (
            active_listing_by_forecast.get(forecast.id)
            or latest_listing_by_forecast.get(forecast.id)
        )

        marketplace_status_label = "Inativa"
        marketplace_status_class = "inv-status inv-status--normal"
        if linked_listing:
            if linked_listing.status == ListingStatus.ACTIVE:
                marketplace_status_label = "Ativa"
                marketplace_status_class = "inv-status inv-status--excess"
            elif linked_listing.status == ListingStatus.RESERVED:
                marketplace_status_label = "Reservada"
            elif linked_listing.status == ListingStatus.CANCELLED:
                marketplace_status_label = "Desativada"
            elif linked_listing.status == ListingStatus.EXPIRED:
                marketplace_status_label = "Expirada"
            elif linked_listing.status == ListingStatus.CLOSED:
                marketplace_status_label = "Fechada"
            elif hasattr(linked_listing, "get_status_display"):
                marketplace_status_label = linked_listing.get_status_display()
        elif forecast.is_marketplace_enabled and saleable_quantity > ZERO:
            marketplace_status_label = "Pronta para publicar"

        rows.append({
            "forecast": forecast,
            "forecast_quantity": forecast_quantity,
            "reserved_quantity": reserved_quantity,
            "forecast_available": available_quantity,
            "forecast_saleable": saleable_quantity,
            "open_published_quantity": open_published_quantity,
            "linked_listing": linked_listing,
            "marketplace_status_label": marketplace_status_label,
            "marketplace_status_class": marketplace_status_class,
        })

    return rows


@transaction.atomic
def save_product_forecast(
    *,
    producer,
    product,
    forecast_quantity,
    period_start=None,
    period_end=None,
    is_marketplace_enabled=False,
    user=None,
    forecast_id=None,
):
    quantity = Decimal(str(forecast_quantity or 0))
    if quantity <= ZERO:
        raise ValidationError("A quantidade prevista deve ser superior a zero.")

    if period_start and period_end and period_end < period_start:
        raise ValidationError("O período final não pode ser anterior ao período inicial.")

    created = False
    if forecast_id:
        try:
            forecast = ProductionForecast.objects.select_for_update().get(
                id=forecast_id,
                producer=producer,
                product=product,
            )
        except ProductionForecast.DoesNotExist:
            raise ValidationError("Previsão não encontrada para este produto.")
    else:
        forecast = ProductionForecast(
            producer=producer,
            product=product,
            reserved_quantity=ZERO,
            source_system=ForecastSourceSystem.MANUAL,
        )
        created = True

    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    if quantity < reserved_quantity:
        raise ValidationError(
            (
                "A quantidade prevista não pode ser inferior à já reservada "
                f"({reserved_quantity})."
            )
        )

    forecast.forecast_quantity = quantity
    forecast.period_start = period_start
    forecast.period_end = period_end
    forecast.is_marketplace_enabled = bool(is_marketplace_enabled)
    if getattr(forecast, "updated_at", None) is not None:
        forecast.updated_at = timezone.now()

    saleable_quantity = max(quantity - reserved_quantity, ZERO)
    if forecast.is_marketplace_enabled and saleable_quantity <= ZERO:
        raise ValidationError(
            "Só pode ativar no marketplace quando existir quantidade disponível para pré-venda."
        )

    if created:
        forecast.save()
    else:
        update_fields = [
            "forecast_quantity",
            "period_start",
            "period_end",
            "is_marketplace_enabled",
            "updated_at",
        ]
        forecast.save(update_fields=update_fields)

    return forecast, created


def get_stock_movements(stock, limit=20):
    return (
        StockMovement.objects
        .filter(stock=stock)
        .select_related("performed_by")
        .order_by("-created_at")[:limit]
    )

def _user_display_name(user):
    if not user:
        return "Sistema"

    full_name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
    return full_name or getattr(user, "email", "Sistema")


def get_stock_activity_feed(stock, limit=20):
    feed = []

    movements = (
        StockMovement.objects
        .filter(stock=stock)
        .select_related("performed_by")
        .order_by("-created_at")[:limit]
    )

    for mv in movements:
        delta = Decimal(str(mv.quantity_delta or 0))
        if delta > 0:
            impact_label = f"+{_format_qty(delta)} {stock.product.unit}"
            impact_class = "is-positive"
        elif delta < 0:
            impact_label = f"{_format_qty(delta)} {stock.product.unit}"
            impact_class = "is-negative"
        else:
            impact_label = "Sem impacto direto"
            impact_class = "is-neutral"

        feed.append({
            "created_at": mv.created_at,
            "type_label": mv.get_movement_type_display(),
            "impact_label": impact_label,
            "impact_class": impact_class,
            "notes": mv.notes or "—",
            "actor_name": _user_display_name(mv.performed_by),
            "source": "movement",
        })

    history_qs = (
        OrderStatusHistory.objects
        .filter(
            order__items__seller_producer=stock.producer,
            order__items__product=stock.product,
        )
        .select_related("changed_by", "order")
        .prefetch_related("order__items__listing")
        .order_by("-created_at")
    )

    seen_ids = set()

    for event in history_qs:
        if event.id in seen_ids:
            continue
        seen_ids.add(event.id)

        related_items = [
            item for item in event.order.items.all()
            if (
                item.seller_producer_id == stock.producer_id
                and item.product_id == stock.product_id
                and getattr(getattr(item, "listing", None), "stock_id", None) == stock.id
            )
        ]
        if not related_items:
            continue

        qty = sum(Decimal(str(item.quantity or 0)) for item in related_items)
        qty = qty.quantize(Decimal("0.001"))

        if event.status == OrderStatus.PENDING:
            impact_label = f"{_format_qty(qty)} {stock.product.unit} solicitados"
            impact_class = "is-neutral"
        elif event.status == OrderStatus.CONFIRMED:
            impact_label = f"+{_format_qty(qty)} {stock.product.unit} reservados"
            impact_class = "is-warning"
        elif event.status == OrderStatus.IN_PROGRESS:
            impact_label = "Pedido em preparação"
            impact_class = "is-info"
        elif event.status == OrderStatus.DELIVERING:
            impact_label = "Pedido em entrega"
            impact_class = "is-info"
        elif event.status == OrderStatus.COMPLETED:
            impact_label = f"-{_format_qty(qty)} {stock.product.unit} debitados"
            impact_class = "is-negative"
        elif event.status == OrderStatus.CANCELLED:
            had_reservation_before = event.order.status_history.filter(
                created_at__lt=event.created_at,
                status__in=[OrderStatus.CONFIRMED, OrderStatus.IN_PROGRESS, OrderStatus.DELIVERING],
            ).exists()

            if had_reservation_before:
                impact_label = f"-{_format_qty(qty)} {stock.product.unit} reserva libertada"
            else:
                impact_label = "Pedido cancelado sem reserva"
            impact_class = "is-neutral"
        else:
            impact_label = "Sem impacto direto"
            impact_class = "is-neutral"

        feed.append({
            "created_at": event.created_at,
            "type_label": f"Encomenda #{event.order.order_number} — {event.get_status_display()}",
            "impact_label": impact_label,
            "impact_class": impact_class,
            "notes": event.notes or "—",
            "actor_name": _user_display_name(event.changed_by),
            "source": "order",
        })

    feed.sort(key=lambda item: item["created_at"], reverse=True)
    return feed[:limit]

@transaction.atomic
def update_stock(
    stock,
    new_quantity,
    safety_stock,
    surplus_threshold,
    movement_type,
    user,
    notes="",
):
    new_quantity = new_quantity or ZERO
    safety_stock = safety_stock or ZERO
    surplus_threshold = surplus_threshold or ZERO

    if new_quantity < ZERO:
        raise ValidationError("A quantidade não pode ser negativa.")

    if new_quantity < stock.reserved_quantity:
        raise ValidationError(
            (
                "A nova quantidade não pode ser inferior à quantidade reservada. "
                f"Atualmente tens {stock.reserved_quantity} reservada."
            )
        )

    quantity_delta = new_quantity - stock.current_quantity
    current_surplus_threshold = getattr(stock, "surplus_threshold", ZERO)
    if current_surplus_threshold is None:
        current_surplus_threshold = ZERO

    threshold_changed = (
        safety_stock != stock.safety_stock
        or surplus_threshold != current_surplus_threshold
    )

    if quantity_delta == ZERO and not threshold_changed:
        raise ValidationError("Não foi detetada nenhuma alteração no stock.")

    stock.current_quantity = new_quantity
    stock.safety_stock = safety_stock
    stock.surplus_threshold = surplus_threshold
    stock.updated_by = user
    stock.last_updated_at = timezone.now()
    update_fields = [
        "current_quantity",
        "safety_stock",
        "surplus_threshold",
        "updated_by",
        "last_updated_at",
        "updated_at",
    ]
    stock.save(update_fields=update_fields)

    movement = None
    if quantity_delta != ZERO:
        movement = StockMovement.objects.create(
            stock=stock,
            movement_type=movement_type,
            quantity_delta=quantity_delta,
            notes=notes or None,
            performed_by=user,
        )

    return movement


# ---------------------------------------------------------------------------
# Compras
# ---------------------------------------------------------------------------

def get_purchase_dashboard(producer):
    now = timezone.now()

    last_4_weeks_start = now - timedelta(days=28)
    current_period_start = now - timedelta(days=30)
    previous_period_start = now - timedelta(days=60)
    chart_start = _shift_month(_month_floor(now), -5)

    completed_orders = Order.objects.filter(
        buyer_producer=producer,
        status=COMPLETED_ORDER_STATUS,
    )

    total_last_4w = _to_decimal(
        completed_orders.filter(completed_at__gte=last_4_weeks_start).aggregate(
            total=Sum("total_amount")
        )["total"]
    )

    completed_count_last_4w = completed_orders.filter(
        completed_at__gte=last_4_weeks_start
    ).count()

    avg_weekly = (total_last_4w / Decimal("4")) if total_last_4w else ZERO

    current_total = _to_decimal(
        completed_orders.filter(completed_at__gte=current_period_start).aggregate(
            total=Sum("total_amount")
        )["total"]
    )

    previous_total = _to_decimal(
        completed_orders.filter(
            completed_at__gte=previous_period_start,
            completed_at__lt=current_period_start,
        ).aggregate(total=Sum("total_amount"))["total"]
    )

    if previous_total > ZERO:
        trend_pct = ((current_total - previous_total) / previous_total) * Decimal("100")
        trend_pct = trend_pct.quantize(Decimal("0.1"))
    elif current_total > ZERO:
        trend_pct = Decimal("100.0")
    else:
        trend_pct = Decimal("0.0")

    if trend_pct > 0:
        trend_direction = "up"
        trend_label = "acima do período anterior"
    elif trend_pct < 0:
        trend_direction = "down"
        trend_label = "abaixo do período anterior"
    else:
        trend_direction = "flat"
        trend_label = "igual ao período anterior"

    in_progress_count = Order.objects.filter(
        buyer_producer=producer,
        status__in=IN_PROGRESS_ORDER_STATUSES,
    ).count()

    monthly_totals_qs = (
        completed_orders
        .filter(completed_at__gte=chart_start)
        .annotate(month=TruncMonth("completed_at"))
        .values("month")
        .annotate(total=Sum("total_amount"))
        .order_by("month")
    )

    totals_by_month = {
        item["month"].date().replace(day=1): _to_decimal(item["total"])
        for item in monthly_totals_qs
    }

    chart_points = []
    for offset in range(-5, 1):
        month_dt = _shift_month(_month_floor(now), offset)
        month_key = month_dt.date().replace(day=1)
        total = totals_by_month.get(month_key, ZERO)
        chart_points.append({
            "label": month_dt.strftime("%b"),
            "total": total,
        })

    max_total = max((point["total"] for point in chart_points), default=ZERO)
    for point in chart_points:
        if max_total > ZERO:
            point["height"] = max(12, int((point["total"] / max_total) * 100))
        else:
            point["height"] = 12

    recent_orders = (
        Order.objects
        .filter(buyer_producer=producer)
        .order_by("-created_at")[:6]
    )

    top_products = (
        OrderItem.objects
        .filter(
            order__buyer_producer=producer,
            order__status=COMPLETED_ORDER_STATUS,
            order__completed_at__gte=chart_start,
        )
        .values("product__name", "product__unit")
        .annotate(total_quantity=Sum("quantity"))
        .order_by("-total_quantity")[:6]
    )

    return {
        "purchase_total_last_4w": total_last_4w,
        "purchase_completed_count_last_4w": completed_count_last_4w,
        "purchase_avg_weekly": avg_weekly,
        "purchase_trend_pct": trend_pct,
        "purchase_trend_direction": trend_direction,
        "purchase_trend_label": trend_label,
        "purchase_in_progress_count": in_progress_count,
        "purchase_chart_points": chart_points,
        "recent_orders": recent_orders,
        "top_products": top_products,
    }

def get_recent_orders_for_export(producer, limit=50):
    recent_orders = (
        Order.objects
        .filter(buyer_producer=producer)
        .order_by("-created_at")[:limit]
    )

    export_total = _to_decimal(
        Order.objects.filter(buyer_producer=producer).aggregate(
            total=Sum("total_amount")
        )["total"]
    )

    return {
        "recent_orders": recent_orders,
        "export_total": export_total,
    }

