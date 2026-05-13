from django.db.models import Count, Q

from apps.catalog.models import Product, ProductCategory


def get_admin_products_queryset(q=""):
    products = (
        Product.objects.select_related("category")
        .annotate(
            active_producers_count=Count(
                "producer_links",
                filter=Q(producer_links__is_active=True),
                distinct=True,
            ),
            producers_count=Count("producer_links", distinct=True),
        )
        .order_by("name")
    )

    if q:
        products = products.filter(
            Q(name__icontains=q)
            | Q(slug__icontains=q)
            | Q(unit__icontains=q)
            | Q(category__name__icontains=q)
        )

    return products


def get_admin_categories_queryset(q=""):
    categories = (
        ProductCategory.objects.annotate(
            products_count=Count("products", distinct=True),
            active_inventory_usage_count=Count(
                "products__producer_links__producer_id",
                filter=Q(products__producer_links__is_active=True),
                distinct=True,
            ),
        )
        .order_by("name")
    )

    if q:
        categories = categories.filter(Q(name__icontains=q) | Q(slug__icontains=q))

    return categories
