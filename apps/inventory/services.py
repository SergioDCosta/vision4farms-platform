from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils.text import slugify
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from apps.catalog.models import Product, ProductCategory
from apps.inventory.models import (
    ProducerProduct,
    ProducerProfile,
    Stock,
    StockMovement,
    StockMovementType,
)
from apps.orders.models import Order, OrderItem


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


def _stock_state(stock):
    """
    Estado visual do stock:
    - critical: current_quantity <= minimum_threshold
    - excess: available_quantity > minimum_threshold
    - normal: restante
    """
    current_quantity = stock.current_quantity if stock else ZERO
    minimum_threshold = stock.minimum_threshold if stock else ZERO
    reserved_quantity = stock.reserved_quantity if stock else ZERO
    available_quantity = stock.available_quantity if stock else ZERO

    publishable_quantity = max(available_quantity - minimum_threshold, ZERO)

    if current_quantity <= minimum_threshold:
        return {
            "key": "critical",
            "label": "Crítico",
            "row_class": "inv-row--critical",
            "pill_class": "inv-status inv-status--critical",
            "text_class": "inv-value inv-value--critical",
            "publishable_quantity": ZERO,
            "action_type": "recommend",
            "action_label": "Comprar",
            "action_icon": "cart",
            "action_url": "/recomendacoes/",
        }

    if publishable_quantity > ZERO:
        return {
            "key": "excess",
            "label": "Excedente",
            "row_class": "inv-row--excess",
            "pill_class": "inv-status inv-status--excess",
            "text_class": "inv-value inv-value--excess",
            "publishable_quantity": publishable_quantity,
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


def _build_unique_slug(base_slug):
    slug = base_slug or "produto"
    candidate = slug
    counter = 2

    while Product.objects.filter(slug=candidate).exists():
        candidate = f"{slug}-{counter}"
        counter += 1

    return candidate


def _ensure_stock_for_product(producer, product, initial_quantity, minimum_threshold, user):
    """
    Garante o registo de stock para produtor+produto.
    Se o stock ainda não existir, cria-o.
    Se existir e estiver a zero, pode aplicar stock inicial.
    """
    initial_quantity = initial_quantity or ZERO
    minimum_threshold = minimum_threshold or ZERO

    stock, stock_created = Stock.objects.get_or_create(
        producer=producer,
        product=product,
        defaults={
            "current_quantity": initial_quantity,
            "reserved_quantity": ZERO,
            "minimum_threshold": minimum_threshold,
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

    if stock.minimum_threshold != minimum_threshold:
        stock.minimum_threshold = minimum_threshold
        changed_fields.append("minimum_threshold")

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


def get_stock_dashboard(producer, q=""):
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

    return {
        "rows": rows,
        "stock_total_count": len(rows),
        "critical_count": critical_count,
        "excess_count": excess_count,
        "q": q,
    }


@transaction.atomic
def add_product_to_producer(producer, product_id, initial_quantity, minimum_threshold, user):
    """
    Associa um produto do catálogo ao produtor e garante stock.
    Se já existia associação inativa, reativa-a.
    """
    product = Product.objects.get(id=product_id, is_active=True)

    producer_product, pp_created = ProducerProduct.objects.get_or_create(
        producer=producer,
        product=product,
        defaults={"is_active": True},
    )

    link_created = pp_created
    if not pp_created and not producer_product.is_active:
        producer_product.is_active = True
        producer_product.updated_at = timezone.now()
        producer_product.save(update_fields=["is_active", "updated_at"])
        link_created = True

    stock = _ensure_stock_for_product(
        producer=producer,
        product=product,
        initial_quantity=initial_quantity,
        minimum_threshold=minimum_threshold,
        user=user,
    )

    return producer_product, stock, False, link_created

@transaction.atomic
def create_custom_product_for_producer(
    producer,
    category,
    name,
    unit,
    description,
    initial_quantity,
    minimum_threshold,
    user,
):
    """
    Cria um novo produto no catálogo (se não existir) e associa-o ao produtor.
    Se o produto já existir pelo slug, usa o existente em vez de duplicar.
    """
    if not category or not isinstance(category, ProductCategory):
        raise ValidationError("Seleciona uma categoria válida.")

    name = _normalize_text(name)
    unit = _normalize_text(unit)
    description = (description or "").strip() or None

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
            description=description,
            is_active=True,
        )
        product_created = True

    producer_product, pp_created = ProducerProduct.objects.get_or_create(
        producer=producer,
        product=product,
        defaults={"is_active": True},
    )

    link_created = pp_created
    if not pp_created and not producer_product.is_active:
        producer_product.is_active = True
        producer_product.updated_at = timezone.now()
        producer_product.save(update_fields=["is_active", "updated_at"])
        link_created = True

    stock = _ensure_stock_for_product(
        producer=producer,
        product=product,
        initial_quantity=initial_quantity,
        minimum_threshold=minimum_threshold,
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

    stock = Stock.objects.filter(
        producer=producer,
        product=producer_product.product,
    ).first()

    if stock and (stock.current_quantity > ZERO or stock.reserved_quantity > ZERO):
        return (
            False,
            (
                f"Não é possível remover {producer_product.product.name} "
                "porque ainda existe stock associado."
            ),
        )

    producer_product.is_active = False
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


def get_stock_movements(stock, limit=20):
    return (
        StockMovement.objects
        .filter(stock=stock)
        .select_related("performed_by")
        .order_by("-created_at")[:limit]
    )


@transaction.atomic
def update_stock(stock, new_quantity, minimum_threshold, movement_type, user, notes=""):
    new_quantity = new_quantity or ZERO
    minimum_threshold = minimum_threshold or ZERO

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
    threshold_changed = minimum_threshold != stock.minimum_threshold

    if quantity_delta == ZERO and not threshold_changed:
        raise ValidationError("Não foi detetada nenhuma alteração no stock.")

    stock.current_quantity = new_quantity
    stock.minimum_threshold = minimum_threshold
    stock.updated_by = user
    stock.last_updated_at = timezone.now()
    stock.save(update_fields=[
        "current_quantity",
        "minimum_threshold",
        "updated_by",
        "last_updated_at",
        "updated_at",
    ])

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