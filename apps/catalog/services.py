from django.core.exceptions import ValidationError
from django.utils.text import slugify

from apps.catalog.models import Product, ProductCategory


UNIT_ALIASES = {
    "kg": "kg",
    "kgs": "kg",
    "quilo": "kg",
    "quilos": "kg",
    "quilograma": "kg",
    "quilogramas": "kg",
    "un": "un",
    "un.": "un",
    "unid": "un",
    "unidade": "un",
    "unidades": "un",
    "caixa": "caixa",
    "caixas": "caixa",
}


class CatalogValidationError(ValidationError):
    def __init__(self, field, message):
        self.field = field
        super().__init__(message)


def normalize_text(value):
    return " ".join((value or "").split()).strip()


def normalize_optional_text(value):
    if value is None:
        return None
    normalized = normalize_text(value)
    return normalized or None


def normalize_unit(value):
    normalized = normalize_text(value).lower()
    return UNIT_ALIASES.get(normalized, normalized)


def build_unique_product_slug(base_slug, exclude_id=None):
    return _build_unique_slug(
        model=Product,
        fallback="produto",
        base_slug=base_slug,
        exclude_id=exclude_id,
    )


def build_unique_category_slug(base_slug, exclude_id=None):
    return _build_unique_slug(
        model=ProductCategory,
        fallback="categoria",
        base_slug=base_slug,
        exclude_id=exclude_id,
    )


def _build_unique_slug(*, model, fallback, base_slug, exclude_id=None):
    slug = base_slug or fallback
    candidate = slug
    counter = 2

    while True:
        qs = model.objects.filter(slug=candidate)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        if not qs.exists():
            return candidate
        candidate = f"{slug}-{counter}"
        counter += 1


def product_snapshot(product):
    return {
        "id": str(product.id),
        "name": product.name,
        "slug": product.slug,
        "category_id": str(product.category_id) if product.category_id else None,
        "category_name": product.category.name if product.category else None,
        "unit": product.unit,
        "description": product.description,
        "is_active": product.is_active,
        "created_at": product.created_at.isoformat() if product.created_at else None,
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
    }


def category_snapshot(category):
    return {
        "id": str(category.id),
        "name": category.name,
        "slug": category.slug,
        "is_active": category.is_active,
        "created_at": category.created_at.isoformat() if category.created_at else None,
        "updated_at": category.updated_at.isoformat() if category.updated_at else None,
    }


def category_usage_counts(category):
    products_qs = Product.objects.filter(category=category)
    return {
        "products_count": products_qs.count(),
        "active_inventory_usage_count": (
            products_qs
            .filter(producer_links__is_active=True)
            .values("producer_links__producer_id")
            .distinct()
            .count()
        ),
    }


def can_delete_category(category):
    usage = category_usage_counts(category)
    return usage["active_inventory_usage_count"] == 0


def delete_category(category):
    usage = category_usage_counts(category)
    if usage["active_inventory_usage_count"] > 0:
        raise CatalogValidationError(
            "category",
            "Esta categoria está a ser usada no inventário de produtores.",
        )
    category.delete()
    return usage


def create_product(*, category, name, unit, description=None, is_active=True):
    name = normalize_text(name)
    unit = normalize_unit(unit)
    description = normalize_optional_text(description)

    _validate_product_fields(category=category, name=name, unit=unit)
    if not getattr(category, "is_active", False):
        raise CatalogValidationError("category", "Seleciona uma categoria ativa.")

    if Product.objects.filter(name__iexact=name).first():
        raise CatalogValidationError("name", "Já existe um produto com esse nome.")

    return Product.objects.create(
        category=category,
        name=name,
        slug=build_unique_product_slug(slugify(name)),
        unit=unit,
        description=description,
        is_active=bool(is_active),
    )


def update_product(*, product, category, name, unit, description=None, is_active=True):
    name = normalize_text(name)
    unit = normalize_unit(unit)
    description = normalize_optional_text(description)

    _validate_product_fields(category=category, name=name, unit=unit)
    if not getattr(category, "is_active", False) and category.id != product.category_id:
        raise CatalogValidationError("category", "Seleciona uma categoria ativa.")

    duplicate = Product.objects.filter(name__iexact=name).exclude(id=product.id).first()
    if duplicate:
        raise CatalogValidationError("name", "Já existe outro produto com esse nome.")

    changed_fields = []
    if product.category_id != category.id:
        product.category = category
        changed_fields.append("category")

    if product.name != name:
        product.name = name
        changed_fields.append("name")
        new_slug = build_unique_product_slug(slugify(name), exclude_id=product.id)
        if product.slug != new_slug:
            product.slug = new_slug
            changed_fields.append("slug")

    if product.unit != unit:
        product.unit = unit
        changed_fields.append("unit")

    if product.description != description:
        product.description = description
        changed_fields.append("description")

    is_active = bool(is_active)
    if product.is_active != is_active:
        product.is_active = is_active
        changed_fields.append("is_active")

    if changed_fields:
        product.save(update_fields=changed_fields + ["updated_at"])

    return product, changed_fields


def create_category(*, name):
    name = normalize_text(name)
    if not name:
        raise CatalogValidationError("name", "Indica o nome da categoria.")
    if ProductCategory.objects.filter(name__iexact=name).first():
        raise CatalogValidationError("name", "Já existe uma categoria com esse nome.")

    return ProductCategory.objects.create(
        name=name,
        slug=build_unique_category_slug(slugify(name)),
        is_active=True,
    )


def update_category(*, category, name):
    name = normalize_text(name)
    if not name:
        raise CatalogValidationError("name", "Indica o nome da categoria.")

    duplicate = ProductCategory.objects.filter(name__iexact=name).exclude(id=category.id).first()
    if duplicate:
        raise CatalogValidationError("name", "Já existe outra categoria com esse nome.")

    changed_fields = []
    if category.name != name:
        category.name = name
        changed_fields.append("name")
        new_slug = build_unique_category_slug(slugify(name), exclude_id=category.id)
        if category.slug != new_slug:
            category.slug = new_slug
            changed_fields.append("slug")

    if changed_fields:
        category.save(update_fields=changed_fields + ["updated_at"])

    return category, changed_fields


def get_or_create_product_for_inventory(*, category, name, unit):
    name = normalize_text(name)
    unit = normalize_unit(unit)
    _validate_product_fields(category=category, name=name, unit=unit)

    if not getattr(category, "is_active", False):
        raise CatalogValidationError("category", "Seleciona uma categoria ativa.")

    existing_product = Product.objects.filter(name__iexact=name).first()
    if existing_product:
        if not existing_product.is_active:
            raise CatalogValidationError(
                "name",
                f"Já existe um produto com o nome '{existing_product.name}', mas está inativo.",
            )
        return existing_product, False

    product = Product.objects.create(
        category=category,
        name=name,
        slug=build_unique_product_slug(slugify(name)),
        unit=unit,
        description=None,
        is_active=True,
    )
    return product, True


def _validate_product_fields(*, category, name, unit):
    if not category or not isinstance(category, ProductCategory):
        raise CatalogValidationError("category", "Seleciona uma categoria válida.")
    if not name:
        raise CatalogValidationError("name", "Indica o nome do produto.")
    if not unit:
        raise CatalogValidationError("unit", "Indica a unidade do produto.")
    if not slugify(name):
        raise CatalogValidationError(
            "name",
            "Não foi possível gerar um identificador válido para o produto.",
        )
