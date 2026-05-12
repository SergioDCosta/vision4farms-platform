from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.catalog.services import CatalogValidationError
from apps.inventory.services import create_custom_product_for_producer


class InventoryCatalogIntegrationTests(SimpleTestCase):
    databases = {"default"}

    @patch("apps.inventory.services._ensure_stock_for_product")
    @patch("apps.inventory.services.ProducerProduct")
    @patch("apps.inventory.services.get_or_create_product_for_inventory")
    def test_custom_product_uses_catalog_service_and_creates_link(
        self,
        get_or_create_product_mock,
        producer_product_model_mock,
        ensure_stock_mock,
    ):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1")
        stock = SimpleNamespace(id="stock-1")
        link = SimpleNamespace(
            is_active=True,
            producer_description=None,
            updated_at=None,
            save=MagicMock(),
        )
        get_or_create_product_mock.return_value = (product, True)
        producer_product_model_mock.objects.get_or_create.return_value = (link, True)
        ensure_stock_mock.return_value = stock

        result_link, result_stock, product_created, link_created = create_custom_product_for_producer(
            producer=producer,
            category=SimpleNamespace(id="category-1"),
            name="Pera Rocha",
            unit="KG",
            initial_quantity=10,
            safety_stock=2,
            surplus_threshold=5,
            user=SimpleNamespace(id="user-1"),
            producer_description="  Produto   local ",
        )

        self.assertIs(result_link, link)
        self.assertIs(result_stock, stock)
        self.assertTrue(product_created)
        self.assertTrue(link_created)
        get_or_create_product_mock.assert_called_once()
        producer_product_model_mock.objects.get_or_create.assert_called_once()
        ensure_stock_mock.assert_called_once()

    @patch("apps.inventory.services.get_or_create_product_for_inventory")
    def test_custom_product_preserves_catalog_validation_message(self, get_or_create_product_mock):
        get_or_create_product_mock.side_effect = CatalogValidationError(
            "name",
            "Já existe um produto com o nome 'Pera Rocha', mas está inativo.",
        )

        with self.assertRaisesMessage(ValidationError, "mas está inativo"):
            create_custom_product_for_producer(
                producer=SimpleNamespace(id="producer-1"),
                category=SimpleNamespace(id="category-1"),
                name="Pera Rocha",
                unit="kg",
                initial_quantity=0,
                safety_stock=0,
                surplus_threshold=0,
                user=SimpleNamespace(id="user-1"),
            )
