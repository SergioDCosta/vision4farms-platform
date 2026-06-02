from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.catalog.models import Product
from apps.catalog.services import (
    CatalogValidationError,
    get_or_create_product_for_inventory,
    normalize_optional_text,
)
from apps.common.audit import log_audit_event
from apps.inventory.audit import _log_stock_movement, _stock_audit_values
from apps.inventory.commitments import (
    calculate_inventory_commitment_state,
    stock_state as _stock_state,
)
from apps.inventory.constants import ZERO
from apps.inventory.models import (
    ProducerProduct,
    ProducerProfile,
    Stock,
    StockMovement,
    StockMovementType,
)


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
