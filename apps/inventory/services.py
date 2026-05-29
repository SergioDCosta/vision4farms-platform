from datetime import datetime, timedelta
from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from apps.catalog.models import Product
from apps.common.audit import log_audit_event
from apps.catalog.services import (
    CatalogValidationError,
    get_or_create_product_for_inventory,
    normalize_optional_text,
)
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
from apps.orders.models import (
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatusHistory,
    OrderStatus,
)


ZERO = Decimal("0.00")
STOCK_WARNING_MARGIN_RATIO = Decimal("0.10")
COMMERCIAL_IN_PROGRESS_ORDER_STATUSES = ["PENDING", "CONFIRMED", "IN_PROGRESS", "DELIVERING"]
COMPLETED_ORDER_STATUS = "COMPLETED"
PRODUCTION_ENTRY_MOVEMENT_TYPES = [
    StockMovementType.IMPORT,
    StockMovementType.CORRECTION,
    StockMovementType.MANUAL_ADJUSTMENT,
]
MONTH_LABELS_PT = [
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
]
MONTH_SHORT_LABELS_PT = [
    "Jan",
    "Fev",
    "Mar",
    "Abr",
    "Mai",
    "Jun",
    "Jul",
    "Ago",
    "Set",
    "Out",
    "Nov",
    "Dez",
]


def _audit_qty(value):
    return str(Decimal(str(value or 0)).quantize(Decimal("0.001")))


def _stock_audit_values(stock):
    return {
        "stock_id": str(stock.id),
        "producer_id": str(stock.producer_id),
        "product_id": str(stock.product_id),
        "product_name": getattr(getattr(stock, "product", None), "name", None),
        "current_quantity": _audit_qty(stock.current_quantity),
        "reserved_quantity": _audit_qty(stock.reserved_quantity),
        "safety_stock": _audit_qty(stock.safety_stock),
    }


def _forecast_audit_values(forecast):
    return {
        "forecast_id": str(forecast.id),
        "producer_id": str(forecast.producer_id),
        "product_id": str(forecast.product_id),
        "product_name": getattr(getattr(forecast, "product", None), "name", None),
        "forecast_quantity": _audit_qty(forecast.forecast_quantity),
        "reserved_quantity": _audit_qty(forecast.reserved_quantity),
        "period_start": str(forecast.period_start) if forecast.period_start else None,
        "period_end": str(forecast.period_end) if forecast.period_end else None,
        "is_marketplace_enabled": bool(forecast.is_marketplace_enabled),
    }


def _log_stock_movement(movement, *, actor=None):
    log_audit_event(
        actor=actor,
        action="STOCK_MOVEMENT_CREATED",
        entity_type="stock_movements",
        entity_id=movement.id,
        notes=movement.notes or "Movimento de stock registado.",
        new_values={
            "stock_id": str(movement.stock_id),
            "product_id": str(movement.stock.product_id),
            "product_name": getattr(getattr(movement.stock, "product", None), "name", None),
            "movement_type": movement.movement_type,
            "quantity_delta": _audit_qty(movement.quantity_delta),
            "reference_type": movement.reference_type,
            "reference_id": str(movement.reference_id) if movement.reference_id else None,
        },
    )


# ---------------------------------------------------------------------------
# Perfil do produtor
# ---------------------------------------------------------------------------

def get_producer_profile(user_id):
    try:
        return ProducerProfile.objects.get(user_id=user_id)
    except ProducerProfile.DoesNotExist:
        return None


def producer_has_active_inventory_products(producer):
    return ProducerProduct.objects.filter(
        producer=producer,
        is_active=True,
    ).exists()


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


def _aware_datetime(year, month, day):
    return timezone.make_aware(
        datetime(year, month, day),
        timezone.get_current_timezone(),
    )


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_decimal(value):
    return value if value is not None else ZERO


def _format_qty(value):
    decimal_value = Decimal(str(value or 0)).quantize(Decimal("0.001"))
    formatted = format(decimal_value, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


def _progress_percent(value, target):
    value = Decimal(str(value or 0))
    target = Decimal(str(target or 0))
    if target <= ZERO:
        return 0
    percent = (value / target) * Decimal("100")
    percent = max(Decimal("0"), min(percent, Decimal("100")))
    return int(percent.quantize(Decimal("1")))


def _quantize_stock_quantity(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))


def _commitment_warning_margin(total_external_demand):
    total_external_demand = _quantize_stock_quantity(total_external_demand)
    if total_external_demand <= ZERO:
        return ZERO
    return (total_external_demand * STOCK_WARNING_MARGIN_RATIO).quantize(Decimal("0.001"))


def calculate_inventory_commitment_state(producer, product, stock=None, *, exclude_listing_id=None):
    """
    Fonte central para avaliar stock face a pedidos externos.

    Quando existem pedidos externos, reutiliza o plano temporal de necessidades:
    stock disponível atual + produção prevista disponível até cada data.
    Sem pedidos externos, mantém um fallback operacional simples baseado no
    stock disponível atual.
    """
    if stock is None and producer and product:
        stock = Stock.objects.filter(producer=producer, product=product).first()

    current_quantity = _quantize_stock_quantity(getattr(stock, "current_quantity", 0))
    reserved_quantity = _quantize_stock_quantity(getattr(stock, "reserved_quantity", 0))
    available_stock_now = _quantize_stock_quantity(max(current_quantity - reserved_quantity, ZERO))
    stored_commitments = _quantize_stock_quantity(getattr(stock, "safety_stock", 0))

    from apps.needs.services import calculate_external_demand_plan

    plan = calculate_external_demand_plan(
        producer=producer,
        product=product,
        exclude_listing_id=exclude_listing_id,
    )
    rows = list(plan.get("rows") or [])
    has_external_demands = bool(rows) or _quantize_stock_quantity(plan.get("total_external_demand")) > ZERO

    if has_external_demands:
        total_external_demand = _quantize_stock_quantity(plan.get("total_external_demand"))
        available_stock_now = _quantize_stock_quantity(plan.get("available_stock_now"))
        useful_forecast_total = _quantize_stock_quantity(plan.get("total_forecast_relevant"))
        max_deficit = _quantize_stock_quantity(plan.get("max_deficit"))
        first_deficit_date = plan.get("first_deficit_date")

        margins = [
            _quantize_stock_quantity(
                _quantize_stock_quantity(row.get("capacity_until_date"))
                - _quantize_stock_quantity(row.get("demand_until_date"))
            )
            for row in rows
        ]
        minimum_remaining_capacity = min(margins) if margins else available_stock_now
        temporal_sellable_quantity = _quantize_stock_quantity(max(minimum_remaining_capacity, ZERO))
        warning_margin = _commitment_warning_margin(total_external_demand)

        if max_deficit > ZERO:
            state_key = "critical"
            state_label = "Défice temporal"
            state_message = (
                "O stock atual e a produção prevista útil não chegam a tempo "
                "dos pedidos externos."
            )
            explanation = "A produção prevista registada não cobre o défice até à primeira data crítica."
        elif temporal_sellable_quantity <= warning_margin:
            state_key = "warning"
            state_label = "Coberto no limite"
            state_message = "Pedidos cobertos, mas com pouca margem disponível."
            explanation = "O stock atual e a produção prevista chegam a tempo, mas a margem para venda é curta."
        elif temporal_sellable_quantity > ZERO:
            state_key = "excess"
            state_label = "Coberto"
            state_message = "Pedidos cobertos com stock atual e produção prevista disponível a tempo."
            explanation = "Existe margem temporal que pode ser vendida sem comprometer os pedidos externos registados."
        else:
            state_key = "normal"
            state_label = "Coberto"
            state_message = "Pedidos cobertos com stock atual e produção prevista disponível a tempo."
            explanation = "Não existe défice temporal calculado para os pedidos externos."

        return {
            "has_external_demands": True,
            "total_external_demand": total_external_demand,
            "available_stock_now": available_stock_now,
            "useful_forecast_total": useful_forecast_total,
            "max_deficit": max_deficit,
            "first_deficit_date": first_deficit_date,
            "minimum_remaining_capacity": _quantize_stock_quantity(minimum_remaining_capacity),
            "temporal_sellable_quantity": temporal_sellable_quantity,
            "state_key": state_key,
            "state_label": state_label,
            "state_message": state_message,
            "explanation": explanation,
            "plan": plan,
            "safety_stock": total_external_demand,
            "current_quantity": current_quantity,
            "reserved_quantity": reserved_quantity,
        }

    if available_stock_now > ZERO:
        state_key = "excess"
        state_label = "Disponível"
        state_message = "Sem compromissos externos ativos para este produto."
        explanation = "Todo o stock disponível pode ser avaliado para venda ou uso interno."
    else:
        state_key = "normal"
        state_label = "Normal"
        state_message = "Sem compromissos externos ativos."
        explanation = "Não existem pedidos externos em aberto para este produto."

    return {
        "has_external_demands": False,
        "total_external_demand": Decimal("0.000"),
        "available_stock_now": available_stock_now,
        "useful_forecast_total": Decimal("0.000"),
        "max_deficit": Decimal("0.000"),
        "first_deficit_date": None,
        "minimum_remaining_capacity": available_stock_now,
        "temporal_sellable_quantity": available_stock_now,
        "state_key": state_key,
        "state_label": state_label,
        "state_message": state_message,
        "explanation": explanation,
        "plan": plan,
        "safety_stock": stored_commitments,
        "current_quantity": current_quantity,
        "reserved_quantity": reserved_quantity,
    }


def _stock_state(stock, commitment_state=None):
    """
    Estado visual do stock:
    - critical: available_quantity < safety_stock
    - warning: safety_stock <= available_quantity <= safety_stock + 10%
    - excess: available_quantity > safety_stock + 10%
    - normal: sem compromissos externos definidos e sem excedente operacional
    """
    current_quantity = _quantize_stock_quantity(
        commitment_state.get("current_quantity") if commitment_state else getattr(stock, "current_quantity", 0)
    )
    safety_stock = _quantize_stock_quantity(
        commitment_state.get("total_external_demand") if commitment_state else getattr(stock, "safety_stock", 0)
    )
    reserved_quantity = _quantize_stock_quantity(
        commitment_state.get("reserved_quantity") if commitment_state else getattr(stock, "reserved_quantity", 0)
    )
    available_quantity = _quantize_stock_quantity(
        commitment_state.get("available_stock_now") if commitment_state else current_quantity - reserved_quantity
    )

    if commitment_state:
        real_surplus = _quantize_stock_quantity(commitment_state.get("temporal_sellable_quantity"))
        deficit_quantity = _quantize_stock_quantity(commitment_state.get("max_deficit"))
        publishable_quantity = real_surplus
        temporal_capacity_quantity = _quantize_stock_quantity(
            available_quantity + _quantize_stock_quantity(commitment_state.get("useful_forecast_total"))
        )
    else:
        real_surplus = max(available_quantity - safety_stock, ZERO)
        deficit_quantity = max((safety_stock + reserved_quantity) - current_quantity, ZERO)
        publishable_quantity = real_surplus
        temporal_capacity_quantity = available_quantity
    warning_margin_quantity = (
        (safety_stock * STOCK_WARNING_MARGIN_RATIO).quantize(Decimal("0.001"))
        if safety_stock > ZERO
        else ZERO
    )
    warning_upper_quantity = safety_stock + warning_margin_quantity

    state_base = {
        "available_quantity": available_quantity,
        "safety_stock": safety_stock,
        "has_safety_stock": safety_stock > ZERO,
        "reserved_quantity": reserved_quantity,
        "current_quantity": current_quantity,
        "warning_margin_quantity": warning_margin_quantity,
        "warning_upper_quantity": warning_upper_quantity,
        "publishable_quantity": publishable_quantity,
        "real_surplus": real_surplus,
        "deficit_quantity": deficit_quantity,
        "temporal_capacity_quantity": temporal_capacity_quantity,
        "useful_forecast_total": _quantize_stock_quantity(
            commitment_state.get("useful_forecast_total") if commitment_state else 0
        ),
        "minimum_remaining_capacity": _quantize_stock_quantity(
            commitment_state.get("minimum_remaining_capacity") if commitment_state else real_surplus
        ),
        "first_deficit_date": commitment_state.get("first_deficit_date") if commitment_state else None,
        "has_external_demands": bool(commitment_state and commitment_state.get("has_external_demands")),
        "state_message": commitment_state.get("state_message") if commitment_state else "",
        "explanation": commitment_state.get("explanation") if commitment_state else "",
        "safety_progress_percent": _progress_percent(available_quantity, safety_stock),
        "temporal_coverage_progress_percent": _progress_percent(temporal_capacity_quantity, safety_stock),
        "warning_progress_percent": _progress_percent(available_quantity, warning_upper_quantity),
        "reserved_progress_percent": _progress_percent(reserved_quantity, current_quantity),
    }

    if commitment_state:
        key = commitment_state.get("state_key") or "normal"
        if key == "critical":
            return state_base | {
                "key": "critical",
                "label": commitment_state.get("state_label") or "Crítico",
                "row_class": "inv-row--critical",
                "pill_class": "inv-status inv-status--critical",
                "text_class": "inv-value inv-value--critical",
                "publishable_quantity": ZERO,
                "action_type": "recommend",
                "action_label": "Comprar",
                "action_icon": "cart",
                "action_url": "/recomendacoes/",
            }
        if key == "warning":
            return state_base | {
                "key": "warning",
                "label": commitment_state.get("state_label") or "Coberto no limite",
                "row_class": "inv-row--warning",
                "pill_class": "inv-status inv-status--warning",
                "text_class": "inv-value inv-value--warning",
                "publishable_quantity": ZERO,
                "action_type": "monitor",
                "action_label": "Acompanhar",
                "action_icon": "exclamation-triangle",
                "action_url": "/recomendacoes/",
            }
        if key == "excess":
            return state_base | {
                "key": "excess",
                "label": commitment_state.get("state_label") or "Disponível",
                "row_class": "inv-row--excess",
                "pill_class": "inv-status inv-status--excess",
                "text_class": "inv-value inv-value--excess",
                "action_type": "publish",
                "action_label": "Publicar",
                "action_icon": "storefront",
                "action_url": "/marketplace/",
            }
        return state_base | {
            "key": "normal",
            "label": commitment_state.get("state_label") or "Normal",
            "row_class": "",
            "pill_class": "inv-status inv-status--normal",
            "text_class": "inv-value",
            "action_type": "marketplace",
            "action_label": "Marketplace",
            "action_icon": "shop",
            "action_url": "/marketplace/",
        }

    if available_quantity < safety_stock:
        return state_base | {
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

    if safety_stock > ZERO and available_quantity <= warning_upper_quantity:
        return state_base | {
            "key": "warning",
            "label": "Perto dos compromissos",
            "row_class": "inv-row--warning",
            "pill_class": "inv-status inv-status--warning",
            "text_class": "inv-value inv-value--warning",
            "publishable_quantity": ZERO,
            "action_type": "monitor",
            "action_label": "Acompanhar",
            "action_icon": "exclamation-triangle",
            "action_url": "/recomendacoes/",
        }

    if real_surplus > ZERO:
        return state_base | {
            "key": "excess",
            "label": "Excedente",
            "row_class": "inv-row--excess",
            "pill_class": "inv-status inv-status--excess",
            "text_class": "inv-value inv-value--excess",
            "action_type": "publish",
            "action_label": "Publicar",
            "action_icon": "storefront",
            "action_url": "/marketplace/",
        }

    return state_base | {
        "key": "normal",
        "label": "Normal",
        "row_class": "",
        "pill_class": "inv-status inv-status--normal",
        "text_class": "inv-value",
        "action_type": "marketplace",
        "action_label": "Marketplace",
        "action_icon": "shop",
        "action_url": "/marketplace/",
    }


# ---------------------------------------------------------------------------
# Produtos do produtor / inventário operacional
# ---------------------------------------------------------------------------
ZERO = Decimal("0")


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
    user,
):
    """
    Garante o registo de stock para produtor+produto.
    Se o stock ainda não existir, cria-o.
    Se existir e estiver a zero, pode aplicar stock inicial.
    """
    initial_quantity = initial_quantity or ZERO
    safety_stock = safety_stock or ZERO

    stock, stock_created = Stock.objects.get_or_create(
        producer=producer,
        product=product,
        defaults={
            "current_quantity": initial_quantity,
            "reserved_quantity": ZERO,
            "safety_stock": safety_stock,
            "updated_by": user,
            "last_updated_at": timezone.now(),
        },
    )

    if stock_created:
        log_audit_event(
            actor=user,
            action="STOCK_CREATED",
            entity_type="stocks",
            entity_id=stock.id,
            notes="Stock criado ao associar produto ao inventário.",
            new_values=_stock_audit_values(stock),
        )
        if initial_quantity > ZERO:
            movement = StockMovement.objects.create(
                stock=stock,
                movement_type=StockMovementType.IMPORT,
                quantity_delta=initial_quantity,
                reference_type="MANUAL",
                notes="Stock inicial definido ao adicionar produto.",
                performed_by=user,
            )
            _log_stock_movement(movement, actor=user)
        return stock

    previous_values = _stock_audit_values(stock)
    changed_fields = []

    if stock.safety_stock != safety_stock:
        stock.safety_stock = safety_stock
        changed_fields.append("safety_stock")

    if stock.current_quantity == ZERO and initial_quantity > ZERO:
        stock.current_quantity = initial_quantity
        changed_fields.append("current_quantity")

        movement = StockMovement.objects.create(
            stock=stock,
            movement_type=StockMovementType.IMPORT,
            quantity_delta=initial_quantity,
            reference_type="MANUAL",
            notes="Stock inicial definido ao associar produto existente.",
            performed_by=user,
        )
        _log_stock_movement(movement, actor=user)

    if changed_fields:
        stock.updated_by = user
        stock.last_updated_at = timezone.now()
        changed_fields.extend(["updated_by", "last_updated_at", "updated_at"])
        stock.save(update_fields=changed_fields)
        log_audit_event(
            actor=user,
            action="STOCK_UPDATED",
            entity_type="stocks",
            entity_id=stock.id,
            notes="Stock atualizado ao associar produto existente.",
            old_values=previous_values,
            new_values=_stock_audit_values(stock),
        )

    return stock


def get_available_products_to_add(producer):
    """
    Devolve produtos ativos do catálogo para o ecrã de associação.
    Inclui também produtos já ligados ao produtor para permitir feedback visual
    (ex.: "já no inventário").
    """
    products = list(
        Product.objects
        .filter(is_active=True)
        .select_related("category")
        .order_by("category__name", "name")
    )

    if not products:
        return products

    links_by_product_id = {
        link.product_id: link
        for link in ProducerProduct.objects.filter(
            producer=producer,
            product_id__in=[product.id for product in products],
        )
    }

    for product in products:
        link = links_by_product_id.get(product.id)
        product.producer_link = link
        product.is_already_in_inventory = bool(link and link.is_active)
        product.is_inactive_in_inventory = bool(link and not link.is_active)

    return products


def get_stock_dashboard(producer, q="", sort="name", incoming_forecast_by_product=None):
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
    warning_count = 0
    excess_count = 0

    for pp in producer_products:
        stock = stocks_by_product_id.get(pp.product_id)
        commitment_state = calculate_inventory_commitment_state(
            producer,
            pp.product,
            stock=stock,
        )
        state = _stock_state(stock, commitment_state=commitment_state)
        incoming_entry = {}
        if incoming_forecast_by_product:
            incoming_entry = (
                incoming_forecast_by_product.get(str(pp.product_id))
                or incoming_forecast_by_product.get(pp.product_id)
                or {}
            )
        incoming_qty = Decimal(str(incoming_entry.get("incoming_qty") or 0))

        if state["key"] == "critical":
            critical_count += 1
        elif state["key"] == "warning":
            warning_count += 1
        elif state["key"] == "excess":
            excess_count += 1

        rows.append({
            "producer_product": pp,
            "product": pp.product,
            "product_id": pp.product_id,
            "stock": stock,
            "state": state,
            "commitment_state": commitment_state,
            "incoming_forecast_qty": incoming_qty,
            "incoming_forecast_period_start": incoming_entry.get("period_start_min"),
            "incoming_forecast_period_end": incoming_entry.get("period_end_max"),
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
        state_priority = {"critical": 0, "warning": 1, "normal": 2, "excess": 3}
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
        "warning_count": warning_count,
        "excess_count": excess_count,
        "q": q,
        "sort": sort,
    }


def build_incoming_forecast_purchase_context(incoming_projection, limit=6):
    incoming_projection = incoming_projection or {}
    products = list(incoming_projection.get("products") or [])
    total_incoming_qty = Decimal(str(incoming_projection.get("total_incoming_qty") or 0))

    return {
        "incoming_forecast_total_qty": total_incoming_qty,
        "incoming_forecast_product_count": len(products),
        "incoming_forecast_products": products[:limit],
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
    user,
    producer_description=None,
):
    """
    Associa um produto do catálogo ao produtor e garante stock.
    Se já existia associação inativa, reativa-a.
    """
    product = Product.objects.get(id=product_id, is_active=True)
    has_producer_description_input = producer_description is not None
    normalized_producer_description = normalize_optional_text(producer_description)

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
        user=user,
    )

    return producer_product, stock, False, link_created

@transaction.atomic
def create_custom_product_for_producer(
    producer,
    category,
    name,
    initial_quantity,
    safety_stock,
    user,
    producer_description=None,
):
    """
    Cria um novo produto no catálogo (se não existir) e associa-o ao produtor.
    Se o produto já existir pelo slug, usa o existente em vez de duplicar.

    - Dados globais: nome/categoria no Product; a unidade operacional é sempre kg.
    - Dado específico do produtor: descrição em ProducerProduct.producer_description.
    """
    has_producer_description_input = producer_description is not None
    normalized_producer_description = normalize_optional_text(producer_description)

    try:
        product, product_created = get_or_create_product_for_inventory(
            category=category,
            name=name,
        )
    except CatalogValidationError as exc:
        raise ValidationError(exc.message) from exc

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


def get_stock_state(stock, commitment_state=None):
    return _stock_state(stock, commitment_state=commitment_state)


def _forecast_saleable_quantity(forecast):
    forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    available = forecast_quantity - reserved_quantity
    return max(available, ZERO)


def get_product_forecasts(producer, product_id):
    forecasts = list(
        ProductionForecast.objects
        .filter(producer=producer, product_id=product_id, forecast_quantity__gt=ZERO)
        .order_by("period_start", "period_end", "-created_at")
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

    today = timezone.localdate()
    rows = []
    for forecast in forecasts:
        forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
        reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
        available_quantity = forecast_quantity - reserved_quantity
        open_published_quantity = Decimal(str(open_published_by_forecast.get(forecast.id, ZERO))).quantize(Decimal("0.001"))
        # "Disponível pré-venda" no card reflete o que ainda está disponível
        # nos anúncios do marketplace associados a esta previsão.
        saleable_quantity = Decimal(str(max(open_published_quantity, ZERO))).quantize(Decimal("0.001"))
        publish_ready_quantity = Decimal(str(max(available_quantity - open_published_quantity, ZERO))).quantize(Decimal("0.001"))
        period_start_local = (
            timezone.localtime(forecast.period_start)
            if getattr(forecast, "period_start", None) and timezone.is_aware(forecast.period_start)
            else getattr(forecast, "period_start", None)
        )
        assimilable_quantity = Decimal(str(max(available_quantity, ZERO))).quantize(Decimal("0.001"))
        can_assimilate_now = bool(
            period_start_local and today >= period_start_local.date() and assimilable_quantity > ZERO
        )

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
        elif forecast.is_marketplace_enabled and publish_ready_quantity > ZERO:
            marketplace_status_label = "Pronta para publicar"

        rows.append({
            "forecast": forecast,
            "forecast_quantity": forecast_quantity,
            "reserved_quantity": reserved_quantity,
            "forecast_available": available_quantity,
            "forecast_saleable": saleable_quantity,
            "assimilable_quantity": assimilable_quantity,
            "open_published_quantity": open_published_quantity,
            "linked_listing": linked_listing,
            "marketplace_status_label": marketplace_status_label,
            "marketplace_status_class": marketplace_status_class,
            "can_assimilate_now": can_assimilate_now,
        })

    return rows


def _forecast_periods_overlap(*, start_a, end_a, start_b, end_b):
    if not start_a or not end_a or not start_b or not end_b:
        return True
    return start_a <= end_b and end_a >= start_b


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

    if not period_start or not period_end:
        raise ValidationError("Indica o início e o fim do período da previsão.")

    if period_start and period_end and period_end < period_start:
        raise ValidationError("O período final não pode ser anterior ao período inicial.")

    existing_forecasts_qs = (
        ProductionForecast.objects
        .select_for_update()
        .filter(producer=producer, product=product)
    )
    existing_forecasts = list(
        existing_forecasts_qs.order_by("period_start", "period_end", "-created_at")
    )

    created = False
    if forecast_id:
        forecast = next(
            (row for row in existing_forecasts if str(row.id) == str(forecast_id)),
            None,
        )
        if not forecast:
            raise ValidationError("Previsão não encontrada para este produto.")
        old_values = _forecast_audit_values(forecast)
    else:
        forecast = ProductionForecast(
            producer=producer,
            product=product,
            reserved_quantity=ZERO,
            source_system=ForecastSourceSystem.MANUAL,
        )
        created = True
        old_values = None

    other_forecasts = [
        row for row in existing_forecasts
        if not getattr(forecast, "id", None) or row.id != forecast.id
    ]

    for other in other_forecasts:
        if not other.period_start or not other.period_end:
            raise ValidationError(
                "Existe uma previsão antiga sem período completo. Edite/limpe essa previsão antes de criar novas."
            )
        if _forecast_periods_overlap(
            start_a=period_start,
            end_a=period_end,
            start_b=other.period_start,
            end_b=other.period_end,
        ):
            overlap_start = timezone.localtime(other.period_start) if timezone.is_aware(other.period_start) else other.period_start
            overlap_end = timezone.localtime(other.period_end) if timezone.is_aware(other.period_end) else other.period_end
            raise ValidationError(
                (
                    "O intervalo desta previsão sobrepõe-se a uma previsão já existente "
                    f"({overlap_start.strftime('%d/%m/%Y')} - {overlap_end.strftime('%d/%m/%Y')})."
                )
            )

    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    open_published_quantity = ZERO
    if getattr(forecast, "id", None):
        open_published_quantity = Decimal(
            str(
                MarketplaceListing.objects.filter(
                    forecast_id=forecast.id,
                    status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
                ).aggregate(total=Sum("quantity_available"))["total"]
                or ZERO
            )
        )

    minimum_allowed_quantity = reserved_quantity + open_published_quantity
    if quantity < minimum_allowed_quantity:
        raise ValidationError(
            (
                "A quantidade prevista não pode ser inferior à quantidade já comprometida "
                f"({minimum_allowed_quantity})."
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

    log_audit_event(
        actor=user,
        action="FORECAST_CREATED" if created else "FORECAST_UPDATED",
        entity_type="production_forecasts",
        entity_id=forecast.id,
        notes="Produção futura registada." if created else "Produção futura atualizada.",
        old_values=old_values,
        new_values=_forecast_audit_values(forecast),
    )
    return forecast, created


@transaction.atomic
def delete_product_forecast(*, producer, product, forecast_id, user=None):
    forecast = (
        ProductionForecast.objects
        .select_for_update()
        .filter(
            id=forecast_id,
            producer=producer,
            product=product,
        )
        .first()
    )
    if not forecast:
        raise ValidationError("Previsão não encontrada para este produto.")

    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    if reserved_quantity > ZERO:
        raise ValidationError(
            "Esta previsão não pode ser eliminada porque já tem quantidade reservada em encomendas."
        )

    has_open_listings = MarketplaceListing.objects.filter(
        forecast_id=forecast.id,
        status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
    ).exists()
    if has_open_listings:
        raise ValidationError(
            "Esta previsão não pode ser eliminada enquanto tiver anúncios ativos/reservados associados."
        )

    previous_values = _forecast_audit_values(forecast)
    forecast.delete()
    log_audit_event(
        actor=user,
        action="FORECAST_DELETED",
        entity_type="production_forecasts",
        entity_id=forecast_id,
        notes="Produção futura removida.",
        old_values=previous_values,
    )
    return True


@transaction.atomic
def assimilate_product_forecast_to_stock(*, producer, product, forecast_id, user):
    forecast = (
        ProductionForecast.objects
        .select_for_update()
        .filter(
            id=forecast_id,
            producer=producer,
            product=product,
        )
        .first()
    )
    if not forecast:
        raise ValidationError("Previsão não encontrada para este produto.")

    if not forecast.period_start:
        raise ValidationError("Esta previsão não tem data de início válida para assimilação.")

    period_start_local = (
        timezone.localtime(forecast.period_start)
        if timezone.is_aware(forecast.period_start)
        else forecast.period_start
    )
    today = timezone.localdate()
    if period_start_local.date() > today:
        raise ValidationError(
            "Esta previsão ainda não pode ser assimilada: a data de início ainda não chegou."
        )

    forecast_quantity = Decimal(str(forecast.forecast_quantity or 0))
    reserved_quantity = Decimal(str(forecast.reserved_quantity or 0))
    quantity_to_assimilate = Decimal(str(max(forecast_quantity - reserved_quantity, ZERO))).quantize(Decimal("0.001"))
    if quantity_to_assimilate <= ZERO:
        raise ValidationError(
            "Não existe quantidade disponível para assumir no stock atual nesta previsão."
        )

    open_listings = list(
        MarketplaceListing.objects
        .select_for_update()
        .filter(
            forecast_id=forecast.id,
            status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
        )
        .only("id", "quantity_available", "quantity_reserved", "status", "updated_at")
    )

    now = timezone.now()
    for listing in open_listings:
        previous_status = listing.status
        listing.quantity_available = Decimal("0.000")
        if Decimal(str(listing.quantity_reserved or 0)) > ZERO:
            listing.status = ListingStatus.RESERVED
        else:
            listing.status = ListingStatus.CLOSED
        listing.updated_at = now
        listing.save(update_fields=["quantity_available", "status", "updated_at"])
        if previous_status != listing.status:
            log_audit_event(
                actor=user,
                action="LISTING_STATUS_CHANGED",
                entity_type="marketplace_listings",
                entity_id=listing.id,
                notes="Anúncio de pré-venda encerrado ao assumir a produção no stock atual.",
                old_values={"status": previous_status},
                new_values={"status": listing.status, "quantity_available": "0.000"},
            )

    stock = (
        Stock.objects
        .select_for_update()
        .filter(producer=producer, product=product)
        .first()
    )
    if not stock:
        raise ValidationError("Stock não encontrado para este produto.")

    stock_old_values = _stock_audit_values(stock)
    forecast_old_values = _forecast_audit_values(forecast)
    stock.current_quantity = Decimal(str(stock.current_quantity or 0)) + quantity_to_assimilate
    stock.updated_by = user
    stock.last_updated_at = now
    stock.updated_at = now
    stock.save(update_fields=["current_quantity", "updated_by", "last_updated_at", "updated_at"])
    log_audit_event(
        actor=user,
        action="STOCK_UPDATED",
        entity_type="stocks",
        entity_id=stock.id,
        notes="Stock acrescido pela assimilação de produção futura.",
        old_values=stock_old_values,
        new_values=_stock_audit_values(stock),
    )

    period_label_start = period_start_local.strftime("%d/%m/%Y")
    period_end_local = (
        timezone.localtime(forecast.period_end)
        if getattr(forecast, "period_end", None) and timezone.is_aware(forecast.period_end)
        else getattr(forecast, "period_end", None)
    )
    period_label_end = period_end_local.strftime("%d/%m/%Y") if period_end_local else "—"

    movement = StockMovement.objects.create(
        stock=stock,
        movement_type=StockMovementType.IMPORT,
        quantity_delta=quantity_to_assimilate,
        reference_type="FORECAST",
        reference_id=forecast.id,
        notes=(
            "Entrada por produção futura assumida no stock atual "
            f"(período {period_label_start} - {period_label_end})."
        ),
        performed_by=user,
    )
    _log_stock_movement(movement, actor=user)

    forecast.forecast_quantity = Decimal(str(max(forecast_quantity - quantity_to_assimilate, ZERO))).quantize(Decimal("0.001"))
    if forecast.forecast_quantity <= ZERO:
        forecast.is_marketplace_enabled = False
    forecast.updated_at = now
    forecast.save(update_fields=["forecast_quantity", "is_marketplace_enabled", "updated_at"])
    log_audit_event(
        actor=user,
        action="FORECAST_ASSIMILATED",
        entity_type="production_forecasts",
        entity_id=forecast.id,
        notes="Produção futura assumida como entrada no stock atual.",
        old_values=forecast_old_values,
        new_values=_forecast_audit_values(forecast) | {
            "quantity_delta": _audit_qty(quantity_to_assimilate),
            "stock_id": str(stock.id),
        },
    )
    return quantity_to_assimilate


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


def _split_note_order_token(notes, order_number):
    note_text = notes or "—"
    if not order_number:
        return {
            "notes": note_text,
            "note_prefix": note_text,
            "note_token": None,
            "note_suffix": "",
        }

    token = f"#{order_number}"
    if token not in note_text:
        return {
            "notes": note_text,
            "note_prefix": note_text,
            "note_token": None,
            "note_suffix": "",
        }

    prefix, suffix = note_text.split(token, 1)
    return {
        "notes": note_text,
        "note_prefix": prefix,
        "note_token": token,
        "note_suffix": suffix,
    }


def get_stock_activity_feed(stock, limit=20):
    feed = []

    movements = (
        StockMovement.objects
        .filter(stock=stock)
        .select_related("performed_by")
        .order_by("-created_at")[:limit]
    )
    movement_order_ids = {
        str(mv.reference_id)
        for mv in movements
        if mv.reference_type == "ORDER" and mv.reference_id
    }
    movement_orders_by_id = {
        str(order.id): order
        for order in Order.objects.filter(id__in=movement_order_ids).only("id", "order_number")
    }

    for mv in movements:
        linked_order = None
        if mv.reference_type == "ORDER" and mv.reference_id:
            linked_order = movement_orders_by_id.get(str(mv.reference_id))
        linked_order_id = str(linked_order.id) if linked_order else None
        linked_order_number = linked_order.order_number if linked_order else None
        note_parts = _split_note_order_token(mv.notes, linked_order_number)

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

        type_label = (
            "Entrega a cliente externo"
            if mv.reference_type == "EXTERNAL_DEMAND"
            else mv.get_movement_type_display()
        )
        feed.append({
            "created_at": mv.created_at,
            "type_label": type_label,
            "impact_label": impact_label,
            "impact_class": impact_class,
            "notes": note_parts["notes"],
            "note_prefix": note_parts["note_prefix"],
            "note_token": note_parts["note_token"],
            "note_suffix": note_parts["note_suffix"],
            "order_id": linked_order_id,
            "order_number": linked_order_number,
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

        note_parts = _split_note_order_token(event.notes, event.order.order_number)

        feed.append({
            "created_at": event.created_at,
            "type_label": f"Encomenda #{event.order.order_number} — {event.get_status_display()}",
            "impact_label": impact_label,
            "impact_class": impact_class,
            "notes": note_parts["notes"],
            "note_prefix": note_parts["note_prefix"],
            "note_token": note_parts["note_token"],
            "note_suffix": note_parts["note_suffix"],
            "order_id": str(event.order.id),
            "order_number": event.order.order_number,
            "actor_name": _user_display_name(event.changed_by),
            "source": "order",
        })

    feed.sort(key=lambda item: item["created_at"], reverse=True)
    return feed[:limit]

class ListingsBlockStockReductionError(ValidationError):
    """Raised when a stock reduction would leave active listings without coverage."""

    def __init__(self, blocking):
        self.blocking = blocking
        super().__init__(
            "A nova quantidade não chega para cobrir os anúncios ativos deste produto."
        )


def get_listings_blocking_stock_decrease(stock, new_quantity):
    """
    Identifica anúncios ACTIVE/RESERVED associados a este stock que não cabem
    no novo valor proposto.

    Devolve dict com:
      - total_published: Σ quantity_available dos listings ativos
      - reserved_quantity: stock.reserved_quantity atual
      - min_required: reserved_quantity + total_published (mínimo para satisfazer
        todos os compromissos atuais)
      - deficit: max(0, min_required - new_quantity)
      - affected_listings: lista [{"listing", "quantity_available"}] ordenada
        por quantity_available decrescente
    """
    new_quantity = _quantize_stock_quantity(new_quantity)
    reserved_quantity = _quantize_stock_quantity(getattr(stock, "reserved_quantity", 0))

    if not stock:
        return {
            "total_published": ZERO,
            "reserved_quantity": reserved_quantity,
            "min_required": reserved_quantity,
            "deficit": ZERO,
            "affected_listings": [],
        }

    listings = list(
        MarketplaceListing.objects
        .filter(
            stock=stock,
            status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
            quantity_available__gt=0,
        )
        .select_related("product", "need", "need__producer")
        .order_by("-quantity_available", "-created_at")
    )

    total_published = _quantize_stock_quantity(
        sum((Decimal(str(l.quantity_available or 0)) for l in listings), Decimal("0"))
    )
    min_required = _quantize_stock_quantity(reserved_quantity + total_published)
    deficit = _quantize_stock_quantity(max(min_required - new_quantity, ZERO))

    return {
        "total_published": total_published,
        "reserved_quantity": reserved_quantity,
        "min_required": min_required,
        "deficit": deficit,
        "affected_listings": [
            {
                "listing": listing,
                "quantity_available": _quantize_stock_quantity(listing.quantity_available),
            }
            for listing in listings
        ],
    }


@transaction.atomic
def reduce_listings_to_fit_stock(
    stock,
    new_quantity,
    *,
    mode,
    listing_ids_to_cancel=None,
    acting_user=None,
):
    """
    Ajusta os anúncios ativos deste stock para caberem em ``new_quantity``.

    Modos:
      - "proportional": reduz quantity_available de todos os anúncios ativos
        proporcionalmente até a soma + reserved caber em new_quantity.
      - "cancel_selected": cancela completamente os anúncios em
        ``listing_ids_to_cancel`` (via ``retire_listing``). Não toca nos
        restantes — chama o caller se ainda houver deficit.

    Não mexe em ``quantity_reserved`` dos anúncios — encomendas pendentes
    mantêm-se. Status do anúncio passa a ``CLOSED`` se ficar com 0 disponível
    e sem reservas.
    """
    from apps.marketplace.services import retire_listing, _listing_audit_values

    new_quantity = _quantize_stock_quantity(new_quantity)
    reserved_quantity = _quantize_stock_quantity(getattr(stock, "reserved_quantity", 0))
    listings = list(
        MarketplaceListing.objects
        .select_for_update()
        .filter(
            stock=stock,
            status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
            quantity_available__gt=0,
        )
        .select_related("product")
        .order_by("-quantity_available", "-created_at")
    )

    if mode not in {"proportional", "cancel_selected"}:
        raise ValueError(f"Modo de reconciliação desconhecido: {mode}")

    cancelled_ids = []
    if mode == "cancel_selected":
        target_ids = {str(lid) for lid in (listing_ids_to_cancel or [])}
        remaining = []
        for listing in listings:
            if str(listing.id) in target_ids:
                retire_listing(listing=listing, acting_user=acting_user)
                cancelled_ids.append(str(listing.id))
            else:
                remaining.append(listing)
        listings = remaining

    target_available_total = _quantize_stock_quantity(
        max(new_quantity - reserved_quantity, ZERO)
    )
    current_available_total = _quantize_stock_quantity(
        sum((Decimal(str(l.quantity_available or 0)) for l in listings), Decimal("0"))
    )

    reduced_log = []
    if current_available_total <= ZERO or current_available_total <= target_available_total:
        return {"cancelled": cancelled_ids, "reduced": reduced_log}

    ratio = (
        Decimal("0") if target_available_total <= ZERO
        else target_available_total / current_available_total
    )
    running_total = Decimal("0.000")
    for index, listing in enumerate(listings):
        old_values = _listing_audit_values(listing)
        old_qty = _quantize_stock_quantity(listing.quantity_available)
        if target_available_total <= ZERO:
            new_qty = Decimal("0.000")
        elif index == len(listings) - 1:
            new_qty = _quantize_stock_quantity(target_available_total - running_total)
        else:
            new_qty = _quantize_stock_quantity(old_qty * ratio)
        new_qty = max(new_qty, Decimal("0.000"))
        running_total = _quantize_stock_quantity(running_total + new_qty)

        if new_qty == old_qty:
            continue

        listing.quantity_available = new_qty
        listing.updated_at = timezone.now()
        update_fields = ["quantity_available", "updated_at"]
        if (
            new_qty <= ZERO
            and _quantize_stock_quantity(listing.quantity_reserved) <= ZERO
            and listing.status == ListingStatus.ACTIVE
        ):
            listing.status = ListingStatus.CLOSED
            update_fields.append("status")
        listing.save(update_fields=update_fields)
        log_audit_event(
            actor=acting_user,
            action="LISTING_AUTO_RECONCILED",
            entity_type="marketplace_listings",
            entity_id=listing.id,
            notes="Anúncio reduzido para caber no novo stock disponível.",
            old_values=old_values,
            new_values=_listing_audit_values(listing),
        )
        reduced_log.append({"listing_id": str(listing.id), "from": str(old_qty), "to": str(new_qty)})

    return {"cancelled": cancelled_ids, "reduced": reduced_log}


@transaction.atomic
def update_stock(
    stock,
    new_quantity,
    safety_stock,
    movement_type,
    user,
    notes="",
    *,
    allow_listing_reconciliation=False,
):
    new_quantity = new_quantity or ZERO
    safety_stock = safety_stock or ZERO

    if new_quantity < ZERO:
        raise ValidationError("A quantidade não pode ser negativa.")

    if new_quantity < stock.reserved_quantity:
        raise ValidationError(
            (
                "A nova quantidade não pode ser inferior à quantidade reservada. "
                f"Atualmente tens {stock.reserved_quantity} reservada."
            )
        )

    if (
        not allow_listing_reconciliation
        and new_quantity < Decimal(str(stock.current_quantity or 0))
    ):
        blocking = get_listings_blocking_stock_decrease(stock, new_quantity)
        if blocking["deficit"] > ZERO:
            raise ListingsBlockStockReductionError(blocking)

    quantity_delta = new_quantity - stock.current_quantity

    threshold_changed = safety_stock != stock.safety_stock

    if quantity_delta == ZERO and not threshold_changed:
        raise ValidationError("Não foi detetada nenhuma alteração no stock.")

    previous_values = _stock_audit_values(stock)
    stock.current_quantity = new_quantity
    stock.safety_stock = safety_stock
    stock.updated_by = user
    stock.last_updated_at = timezone.now()
    update_fields = [
        "current_quantity",
        "safety_stock",
        "updated_by",
        "last_updated_at",
        "updated_at",
    ]
    stock.save(update_fields=update_fields)
    log_audit_event(
        actor=user,
        action="STOCK_UPDATED",
        entity_type="stocks",
        entity_id=stock.id,
        notes="Quantidade de stock atualizada manualmente.",
        old_values=previous_values,
        new_values=_stock_audit_values(stock),
    )

    movement = None
    if quantity_delta != ZERO:
        movement = StockMovement.objects.create(
            stock=stock,
            movement_type=movement_type,
            quantity_delta=quantity_delta,
            notes=notes or None,
            performed_by=user,
        )
        _log_stock_movement(movement, actor=user)

    return movement


# ---------------------------------------------------------------------------
# Compras, vendas e produção
# ---------------------------------------------------------------------------

def _period_bounds(*, period="annual", year=None, month=None, now=None):
    now = now or timezone.now()
    current_year = now.year
    selected_year = _safe_int(year, current_year)
    if selected_year < 2000 or selected_year > current_year + 1:
        selected_year = current_year

    selected_month = _safe_int(month, now.month)
    if selected_month < 1 or selected_month > 12:
        selected_month = now.month

    selected_period = (period or "annual").strip().lower()
    if selected_period not in {"annual", "monthly"}:
        selected_period = "annual"

    if selected_period == "monthly":
        start = _aware_datetime(selected_year, selected_month, 1)
        end = _shift_month(start, 1)
        previous_start = _shift_month(start, -1)
        previous_end = start
        label = f"{MONTH_LABELS_PT[selected_month - 1]} {selected_year}"
    else:
        start = _aware_datetime(selected_year, 1, 1)
        end = _aware_datetime(selected_year + 1, 1, 1)
        previous_start = _aware_datetime(selected_year - 1, 1, 1)
        previous_end = start
        label = str(selected_year)

    return {
        "period": selected_period,
        "year": selected_year,
        "month": selected_month,
        "start": start,
        "end": end,
        "previous_start": previous_start,
        "previous_end": previous_end,
        "label": label,
    }


def _period_chart_segments(bounds):
    if bounds["period"] == "annual":
        segments = []
        for month in range(1, 13):
            start = _aware_datetime(bounds["year"], month, 1)
            end = _shift_month(start, 1)
            segments.append({
                "label": MONTH_SHORT_LABELS_PT[month - 1],
                "start": start,
                "end": end,
            })
        return segments

    segments = []
    start = bounds["start"]
    end = bounds["end"]
    cursor = start
    while cursor < end:
        segment_end = min(cursor + timedelta(days=7), end)
        segments.append({
            "label": f"{cursor.day}-{(segment_end - timedelta(days=1)).day}",
            "start": cursor,
            "end": segment_end,
        })
        cursor = segment_end
    return segments


def _trend(current, previous):
    current = _to_decimal(current)
    previous = _to_decimal(previous)
    if previous > ZERO:
        pct = ((current - previous) / previous * Decimal("100")).quantize(Decimal("0.1"))
    elif current > ZERO:
        pct = Decimal("100.0")
    else:
        pct = Decimal("0.0")

    if pct > ZERO:
        return {"pct": pct, "direction": "up", "label": "acima do período anterior"}
    if pct < ZERO:
        return {"pct": pct, "direction": "down", "label": "abaixo do período anterior"}
    return {"pct": pct, "direction": "flat", "label": "igual ao período anterior"}


def _producer_name(producer):
    if not producer:
        return "Produtor"
    if getattr(producer, "display_name", None):
        return producer.display_name
    if getattr(producer, "company_name", None):
        return producer.company_name
    user = getattr(producer, "user", None)
    if user:
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        return full_name or user.email or "Produtor"
    return "Produtor"


def _build_order_items_label(items, *, limit=3):
    labels = []
    for item in list(items)[:limit]:
        labels.append(f"{_format_qty(item.quantity)} {item.product.unit} {item.product.name}")
    extra_count = max(len(items) - limit, 0)
    if extra_count:
        labels.append(f"+{extra_count} produto{'' if extra_count == 1 else 's'}")
    return " · ".join(labels) if labels else "Sem itens ativos"


def _purchase_total(producer, start, end):
    return _to_decimal(
        Order.objects.filter(
            buyer_producer=producer,
            status=COMPLETED_ORDER_STATUS,
            completed_at__gte=start,
            completed_at__lt=end,
        ).aggregate(total=Sum("total_amount"))["total"]
    )


def _sales_total(producer, start, end):
    return _to_decimal(
        OrderItem.objects.filter(
            seller_producer=producer,
            order__status=COMPLETED_ORDER_STATUS,
            item_status=OrderItemStatus.COMPLETED,
            order__completed_at__gte=start,
            order__completed_at__lt=end,
        ).aggregate(total=Sum("subtotal"))["total"]
    )


def _production_total(producer, start, end):
    return _to_decimal(
        StockMovement.objects.filter(
            stock__producer=producer,
            movement_type__in=PRODUCTION_ENTRY_MOVEMENT_TYPES,
            quantity_delta__gt=ZERO,
            created_at__gte=start,
            created_at__lt=end,
        ).aggregate(total=Sum("quantity_delta"))["total"]
    )


def _build_commercial_chart(producer, segments):
    points = []
    for segment in segments:
        points.append({
            "label": segment["label"],
            "purchase_total": _purchase_total(producer, segment["start"], segment["end"]),
            "sales_total": _sales_total(producer, segment["start"], segment["end"]),
        })
    return points


def _build_production_chart(producer, segments):
    points = []
    for segment in segments:
        points.append({
            "label": segment["label"],
            "quantity": _production_total(producer, segment["start"], segment["end"]),
        })
    return points


def _build_purchase_history_rows(producer, start, end, limit=8):
    orders = (
        Order.objects
        .filter(buyer_producer=producer, created_at__gte=start, created_at__lt=end)
        .prefetch_related("items__product", "items__seller_producer__user")
        .order_by("-created_at")[:limit]
    )

    rows = []
    for order in orders:
        active_items = [item for item in order.items.all() if item.item_status != OrderItemStatus.CANCELLED]
        sellers = []
        seen_sellers = set()
        for item in active_items:
            seller_id = getattr(item, "seller_producer_id", None)
            if seller_id in seen_sellers:
                continue
            seen_sellers.add(seller_id)
            sellers.append(_producer_name(item.seller_producer))

        rows.append({
            "order": order,
            "detail_url": f"/encomendas/{order.id}/",
            "title": f"Encomenda #{order.order_number}",
            "meta": f"{order.created_at.strftime('%d/%m/%Y %H:%M')} · {order.get_status_display()}",
            "items_label": _build_order_items_label(active_items),
            "counterparty_label": ", ".join(sellers[:2]) if sellers else "Vendedor não identificado",
            "value": _to_decimal(order.total_amount),
        })
    return rows


def _build_sales_history_rows(producer, start, end, limit=8):
    orders = (
        Order.objects
        .filter(items__seller_producer=producer, created_at__gte=start, created_at__lt=end)
        .select_related("buyer_producer__user")
        .prefetch_related("items__product", "items__seller_producer")
        .distinct()
        .order_by("-created_at")[:limit]
    )

    rows = []
    for order in orders:
        seller_items = [
            item for item in order.items.all()
            if item.seller_producer_id == producer.id and item.item_status != OrderItemStatus.CANCELLED
        ]
        value = sum((Decimal(str(item.subtotal or 0)) for item in seller_items), ZERO)
        rows.append({
            "order": order,
            "detail_url": f"/encomendas/{order.id}/",
            "title": f"Encomenda #{order.order_number}",
            "meta": f"{order.created_at.strftime('%d/%m/%Y %H:%M')} · {order.get_status_display()}",
            "items_label": _build_order_items_label(seller_items),
            "counterparty_label": _producer_name(order.buyer_producer),
            "value": value,
        })
    return rows


def get_purchase_dashboard(producer, *, period="annual", year=None, month=None):
    bounds = _period_bounds(period=period, year=year, month=month)
    start = bounds["start"]
    end = bounds["end"]
    previous_start = bounds["previous_start"]
    previous_end = bounds["previous_end"]
    segments = _period_chart_segments(bounds)

    purchase_total = _purchase_total(producer, start, end)
    sales_total = _sales_total(producer, start, end)
    production_total = _production_total(producer, start, end)
    previous_purchase_total = _purchase_total(producer, previous_start, previous_end)
    previous_sales_total = _sales_total(producer, previous_start, previous_end)
    previous_production_total = _production_total(producer, previous_start, previous_end)

    purchase_completed_count = Order.objects.filter(
        buyer_producer=producer,
        status=COMPLETED_ORDER_STATUS,
        completed_at__gte=start,
        completed_at__lt=end,
    ).count()
    sales_completed_count = (
        OrderItem.objects
        .filter(
            seller_producer=producer,
            order__status=COMPLETED_ORDER_STATUS,
            item_status=OrderItemStatus.COMPLETED,
            order__completed_at__gte=start,
            order__completed_at__lt=end,
        )
        .values("order_id")
        .distinct()
        .count()
    )
    purchase_in_progress_count = Order.objects.filter(
        buyer_producer=producer,
        status__in=COMMERCIAL_IN_PROGRESS_ORDER_STATUSES,
    ).count()
    sales_in_progress_count = (
        OrderItem.objects
        .filter(
            seller_producer=producer,
            order__status__in=COMMERCIAL_IN_PROGRESS_ORDER_STATUSES,
        )
        .exclude(item_status__in=[OrderItemStatus.CANCELLED, OrderItemStatus.COMPLETED])
        .values("order_id")
        .distinct()
        .count()
    )

    production_qs = StockMovement.objects.filter(
        stock__producer=producer,
        movement_type__in=PRODUCTION_ENTRY_MOVEMENT_TYPES,
        quantity_delta__gt=ZERO,
        created_at__gte=start,
        created_at__lt=end,
    )
    production_product_count = production_qs.values("stock__product_id").distinct().count()
    top_production_product = (
        production_qs
        .values("stock__product__name", "stock__product__unit")
        .annotate(total_quantity=Sum("quantity_delta"))
        .order_by("-total_quantity")
        .first()
    )

    commercial_points = _build_commercial_chart(producer, segments)
    production_points = _build_production_chart(producer, segments)
    commercial_chart_data = {
        "labels": [point["label"] for point in commercial_points],
        "purchases": [float(point["purchase_total"]) for point in commercial_points],
        "sales": [float(point["sales_total"]) for point in commercial_points],
    }
    production_chart_data = {
        "labels": [point["label"] for point in production_points],
        "quantities": [float(point["quantity"]) for point in production_points],
    }

    top_purchased_products = (
        OrderItem.objects
        .filter(
            order__buyer_producer=producer,
            order__status=COMPLETED_ORDER_STATUS,
            order__completed_at__gte=start,
            order__completed_at__lt=end,
        )
        .exclude(item_status=OrderItemStatus.CANCELLED)
        .values("product__name", "product__unit")
        .annotate(total_quantity=Sum("quantity"), total_amount=Sum("subtotal"))
        .order_by("-total_quantity")[:6]
    )
    top_sold_products = (
        OrderItem.objects
        .filter(
            seller_producer=producer,
            order__status=COMPLETED_ORDER_STATUS,
            item_status=OrderItemStatus.COMPLETED,
            order__completed_at__gte=start,
            order__completed_at__lt=end,
        )
        .values("product__name", "product__unit")
        .annotate(total_quantity=Sum("quantity"), total_amount=Sum("subtotal"))
        .order_by("-total_quantity")[:6]
    )
    production_product_rows = (
        production_qs
        .values("stock__product__name", "stock__product__unit")
        .annotate(
            total_quantity=Sum("quantity_delta"),
            movement_count=Count("id"),
            last_movement_at=Max("created_at"),
        )
        .order_by("-total_quantity")[:8]
    )
    production_product_chart_data = {
        "labels": [row["stock__product__name"] for row in production_product_rows],
        "quantities": [float(row["total_quantity"] or 0) for row in production_product_rows],
    }

    return {
        "commercial_period": bounds["period"],
        "commercial_year": bounds["year"],
        "commercial_month": bounds["month"],
        "commercial_period_label": bounds["label"],
        "commercial_year_options": list(range(timezone.now().year + 1, timezone.now().year - 6, -1)),
        "commercial_month_options": [
            {"value": idx, "label": MONTH_LABELS_PT[idx - 1]}
            for idx in range(1, 13)
        ],
        "purchase_total_period": purchase_total,
        "purchase_completed_count_period": purchase_completed_count,
        "purchase_trend": _trend(purchase_total, previous_purchase_total),
        "sales_total_period": sales_total,
        "sales_completed_count_period": sales_completed_count,
        "sales_trend": _trend(sales_total, previous_sales_total),
        "commercial_balance": sales_total - purchase_total,
        "purchase_in_progress_count": purchase_in_progress_count,
        "sales_in_progress_count": sales_in_progress_count,
        "production_total_period": production_total,
        "production_product_count": production_product_count,
        "production_trend": _trend(production_total, previous_production_total),
        "top_production_product": top_production_product,
        "commercial_chart_data": commercial_chart_data,
        "production_chart_data": production_chart_data,
        "production_product_chart_data": production_product_chart_data,
        "purchase_chart_points": commercial_points,
        "recent_orders": _build_purchase_history_rows(producer, start, end),
        "recent_sales_rows": _build_sales_history_rows(producer, start, end),
        "top_products": top_purchased_products,
        "top_sold_products": top_sold_products,
        "production_product_rows": production_product_rows,
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
