from django.test import SimpleTestCase

from apps.catalog.models import Product, ProductCategory


class CatalogModelTests(SimpleTestCase):
    def test_category_string_is_name(self):
        category = ProductCategory(name="Fruta", slug="fruta")

        self.assertEqual(str(category), "Fruta")

    def test_product_string_is_name(self):
        product = Product(name="Pera Rocha", slug="pera-rocha", unit="kg")

        self.assertEqual(str(product), "Pera Rocha")
