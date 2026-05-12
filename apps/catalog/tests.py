from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.catalog.models import Product, ProductCategory
from apps.catalog.services import (
    CatalogValidationError,
    build_unique_product_slug,
    can_delete_category,
    create_product,
    delete_category,
    normalize_text,
    normalize_unit,
    product_snapshot,
    update_product,
)


class CatalogModelTests(SimpleTestCase):
    def test_category_string_is_name(self):
        category = ProductCategory(name="Fruta", slug="fruta")

        self.assertEqual(str(category), "Fruta")

    def test_product_string_is_name(self):
        product = Product(name="Pera Rocha", slug="pera-rocha", unit="kg")

        self.assertEqual(str(product), "Pera Rocha")


class CatalogNormalizationTests(SimpleTestCase):
    def test_normalize_text_collapses_whitespace(self):
        self.assertEqual(normalize_text("  Pera   Rocha  "), "Pera Rocha")

    def test_normalize_unit_handles_known_aliases(self):
        self.assertEqual(normalize_unit("KG"), "kg")
        self.assertEqual(normalize_unit("quilogramas"), "kg")
        self.assertEqual(normalize_unit("Unidades"), "un")
        self.assertEqual(normalize_unit("Caixas"), "caixa")

    def test_normalize_unit_keeps_unknown_units_lowercase(self):
        self.assertEqual(normalize_unit("  Molho  Grande "), "molho grande")


class CatalogServiceTests(SimpleTestCase):
    @patch("apps.catalog.services.Product")
    def test_build_unique_product_slug_adds_suffix(self, product_model_mock):
        first_qs = MagicMock()
        first_qs.exists.return_value = True
        second_qs = MagicMock()
        second_qs.exists.return_value = False
        product_model_mock.objects.filter.side_effect = [first_qs, second_qs]

        self.assertEqual(build_unique_product_slug("pera-rocha"), "pera-rocha-2")

    @patch("apps.catalog.services.Product")
    def test_create_product_rejects_duplicate_case_insensitive_name(self, product_model_mock):
        category = ProductCategory(name="Fruta", slug="fruta", is_active=True)
        product_model_mock.objects.filter.return_value.first.return_value = Product(
            name="Pera Rocha",
            slug="pera-rocha",
            unit="kg",
        )

        with self.assertRaises(CatalogValidationError) as ctx:
            create_product(
                category=category,
                name="pera rocha",
                unit="KG",
                description="",
                is_active=True,
            )

        self.assertEqual(ctx.exception.field, "name")
        product_model_mock.objects.create.assert_not_called()

    @patch("apps.catalog.services.build_unique_product_slug", return_value="maca-gala")
    @patch("apps.catalog.services.Product")
    def test_create_product_normalizes_unit_and_description(self, product_model_mock, slug_mock):
        category = ProductCategory(id="11111111-1111-1111-1111-111111111111", name="Fruta", slug="fruta", is_active=True)
        product_model_mock.objects.filter.return_value.first.return_value = None
        created_product = Product(name="Maçã Gala", slug="maca-gala", unit="kg")
        product_model_mock.objects.create.return_value = created_product

        result = create_product(
            category=category,
            name="  Maçã   Gala ",
            unit="Quilogramas",
            description="  Boa   para venda ",
            is_active=True,
        )

        self.assertIs(result, created_product)
        product_model_mock.objects.create.assert_called_once()
        create_kwargs = product_model_mock.objects.create.call_args.kwargs
        self.assertEqual(create_kwargs["name"], "Maçã Gala")
        self.assertEqual(create_kwargs["unit"], "kg")
        self.assertEqual(create_kwargs["description"], "Boa para venda")

    @patch("apps.catalog.services.build_unique_product_slug", return_value="maca-gala")
    @patch("apps.catalog.services.Product")
    def test_update_product_changes_slug_when_name_changes(self, product_model_mock, slug_mock):
        category = ProductCategory(id="11111111-1111-1111-1111-111111111111", name="Fruta", slug="fruta", is_active=True)
        product = Product(
            id="22222222-2222-2222-2222-222222222222",
            category=category,
            name="Maçã",
            slug="maca",
            unit="kg",
            description=None,
            is_active=True,
        )
        product_model_mock.objects.filter.return_value.exclude.return_value.first.return_value = None
        product.save = MagicMock()

        updated, changed_fields = update_product(
            product=product,
            category=category,
            name="Maçã Gala",
            unit="KG",
            description="",
            is_active=True,
        )

        self.assertIs(updated, product)
        self.assertIn("name", changed_fields)
        self.assertIn("slug", changed_fields)
        self.assertEqual(product.slug, "maca-gala")
        product.save.assert_called_once_with(update_fields=changed_fields + ["updated_at"])

    def test_product_snapshot_is_plain_audit_data(self):
        category = SimpleNamespace(id="cat-1", name="Fruta")
        product = SimpleNamespace(
            id="prod-1",
            name="Pera Rocha",
            slug="pera-rocha",
            category_id="cat-1",
            category=category,
            unit="kg",
            description=None,
            is_active=True,
            created_at=None,
            updated_at=None,
        )

        self.assertEqual(
            product_snapshot(product),
            {
                "id": "prod-1",
                "name": "Pera Rocha",
                "slug": "pera-rocha",
                "category_id": "cat-1",
                "category_name": "Fruta",
                "unit": "kg",
                "description": None,
                "is_active": True,
                "created_at": None,
                "updated_at": None,
            },
        )

    @patch("apps.catalog.services.Product")
    def test_can_delete_category_when_no_active_inventory_usage(self, product_model_mock):
        products_qs = MagicMock()
        usage_qs = MagicMock()
        products_qs.filter.return_value = usage_qs
        usage_qs.values.return_value.distinct.return_value.count.return_value = 0
        product_model_mock.objects.filter.return_value = products_qs

        self.assertTrue(can_delete_category(ProductCategory(name="Fruta")))

    @patch("apps.catalog.services.Product")
    def test_delete_category_blocks_active_inventory_usage(self, product_model_mock):
        products_qs = MagicMock()
        usage_qs = MagicMock()
        products_qs.filter.return_value = usage_qs
        usage_qs.values.return_value.distinct.return_value.count.return_value = 1
        product_model_mock.objects.filter.return_value = products_qs
        category = ProductCategory(name="Fruta")
        category.delete = MagicMock()

        with self.assertRaises(CatalogValidationError):
            delete_category(category)

        category.delete.assert_not_called()
