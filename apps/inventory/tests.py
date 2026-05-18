from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase

from apps.catalog.services import CatalogValidationError
from apps.inventory.forms import CreateCustomProductForm
from apps.inventory import views
from apps.inventory.services import (
    create_custom_product_for_producer,
    producer_has_active_inventory_products,
)


class InventoryCatalogIntegrationTests(SimpleTestCase):
    databases = {"default"}

    def test_custom_product_form_does_not_expose_unit(self):
        self.assertNotIn("unit", CreateCustomProductForm().fields)

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
        get_or_create_product_mock.assert_called_once_with(
            category=SimpleNamespace(id="category-1"),
            name="Pera Rocha",
        )
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
                initial_quantity=0,
                safety_stock=0,
                surplus_threshold=0,
                user=SimpleNamespace(id="user-1"),
            )

    @patch("apps.inventory.services.ProducerProduct")
    def test_active_inventory_product_flag_uses_active_producer_products(self, producer_product_mock):
        filter_mock = producer_product_mock.objects.filter
        filter_mock.return_value.exists.return_value = True
        producer = SimpleNamespace(id="producer-1")

        self.assertTrue(producer_has_active_inventory_products(producer))
        filter_mock.assert_called_once_with(producer=producer, is_active=True)


class InventoryViewContextTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("apps.inventory.views.render")
    @patch("apps.inventory.views.services.get_stock_dashboard")
    @patch("apps.inventory.views.get_buyer_incoming_forecast_projection")
    @patch("apps.inventory.views.services.producer_has_active_inventory_products")
    @patch("apps.inventory.views._get_producer_or_redirect")
    def test_meus_produtos_context_includes_active_product_flag(
        self,
        get_producer_mock,
        active_product_flag_mock,
        incoming_projection_mock,
        stock_dashboard_mock,
        render_mock,
    ):
        producer = SimpleNamespace(id="producer-1")
        request = self.factory.get("/inventario/produtos/?tab=stock")
        request.current_user = SimpleNamespace(id="user-1")
        request.htmx = False
        get_producer_mock.return_value = producer
        active_product_flag_mock.return_value = False
        incoming_projection_mock.return_value = {"by_product": {}}
        stock_dashboard_mock.return_value = {
            "rows": [],
            "category_groups": [],
            "stock_total_count": 0,
            "critical_count": 0,
            "excess_count": 0,
        }
        render_mock.return_value = SimpleNamespace(status_code=200)

        views.meus_produtos.__wrapped__(request)

        context = render_mock.call_args.args[2]
        self.assertFalse(context["has_active_inventory_products"])
