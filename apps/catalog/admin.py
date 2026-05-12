from django.contrib import admin

from apps.catalog.models import Product, ProductCategory


@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_at", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    ordering = ("name",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "unit", "is_active", "created_at", "updated_at")
    list_filter = ("is_active", "category")
    search_fields = ("name", "slug", "unit", "category__name")
    ordering = ("name",)
