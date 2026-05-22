from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from datetime import datetime

from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from apps.catalog.services import CatalogValidationError
from apps.inventory.forms import CreateCustomProductForm, UpdateStockForm
from apps.inventory import views
from apps.inventory.services import (
    create_custom_product_for_producer,
    get_stock_state,
    producer_has_active_inventory_products,
    _period_bounds,
    _period_chart_segments,
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
                user=SimpleNamespace(id="user-1"),
            )

    @patch("apps.inventory.services.ProducerProduct")
    def test_active_inventory_product_flag_uses_active_producer_products(self, producer_product_mock):
        filter_mock = producer_product_mock.objects.filter
        filter_mock.return_value.exists.return_value = True
        producer = SimpleNamespace(id="producer-1")

        self.assertTrue(producer_has_active_inventory_products(producer))
        filter_mock.assert_called_once_with(producer=producer, is_active=True)


class InventoryStockStateTests(SimpleTestCase):
    def test_stock_equal_to_safety_stock_is_warning_not_critical(self):
        stock = SimpleNamespace(
            current_quantity=10,
            reserved_quantity=0,
            safety_stock=10,
        )

        state = get_stock_state(stock)

        self.assertEqual(state["key"], "warning")
        self.assertEqual(state["label"], "Perto dos compromissos")
        self.assertEqual(state["pill_class"], "inv-status inv-status--warning")

    def test_stock_below_safety_stock_is_critical(self):
        stock = SimpleNamespace(
            current_quantity=9,
            reserved_quantity=0,
            safety_stock=10,
        )

        self.assertEqual(get_stock_state(stock)["key"], "critical")


class InventoryStockFormTests(SimpleTestCase):
    def test_update_stock_form_accepts_decimal_quantities(self):
        form = UpdateStockForm(
            data={
                "new_quantity": "20.5",
                "safety_stock": "10.25",
                "movement_type": "MANUAL_ADJUSTMENT",
                "notes": "",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(str(form.cleaned_data["new_quantity"]), "20.5")
        self.assertEqual(str(form.cleaned_data["safety_stock"]), "10.25")


class InventoryCommercialReportTests(SimpleTestCase):
    def test_annual_report_uses_12_monthly_points(self):
        now = timezone.make_aware(datetime(2026, 5, 20, 12, 0, 0))
        bounds = _period_bounds(period="annual", year="2026", month="", now=now)
        segments = _period_chart_segments(bounds)

        self.assertEqual(bounds["period"], "annual")
        self.assertEqual(bounds["year"], 2026)
        self.assertEqual(len(segments), 12)
        self.assertEqual(segments[0]["start"].month, 1)
        self.assertEqual(segments[-1]["start"].month, 12)

    def test_monthly_report_uses_weekly_points(self):
        now = timezone.make_aware(datetime(2026, 5, 20, 12, 0, 0))
        bounds = _period_bounds(period="monthly", year="2026", month="5", now=now)
        segments = _period_chart_segments(bounds)

        self.assertEqual(bounds["period"], "monthly")
        self.assertEqual(bounds["month"], 5)
        self.assertEqual(segments[0]["label"], "1-7")
        self.assertTrue(len(segments) >= 4)
        self.assertEqual(segments[-1]["end"].month, 6)


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
            "warning_count": 0,
            "excess_count": 0,
        }
        render_mock.return_value = SimpleNamespace(status_code=200)

        views.meus_produtos.__wrapped__(request)

        context = render_mock.call_args.args[2]
        self.assertFalse(context["has_active_inventory_products"])

    @patch("apps.inventory.views.messages")
    @patch("apps.inventory.views.services.remove_product_from_producer")
    @patch("apps.inventory.views._get_producer_or_redirect")
    def test_remove_product_rejects_external_next_url(
        self,
        get_producer_mock,
        remove_product_mock,
        messages_mock,
    ):
        producer = SimpleNamespace(id="producer-1")
        request = self.factory.post(
            "/inventario/produtos/prod-1/remover/",
            data={"next": "https://evil.example/phish"},
            HTTP_HOST="testserver",
        )
        request.current_user = SimpleNamespace(id="user-1")
        get_producer_mock.return_value = producer
        remove_product_mock.return_value = (True, None)

        response = views.remover_produto.__wrapped__(request, "producer-product-1")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/inventario/produtos/?tab=desativados")
        messages_mock.success.assert_called_once()

    @patch("apps.inventory.views.messages")
    @patch("apps.inventory.views.services.reactivate_product_from_producer")
    @patch("apps.inventory.views._get_producer_or_redirect")
    def test_reactivate_product_allows_local_next_url(
        self,
        get_producer_mock,
        reactivate_product_mock,
        messages_mock,
    ):
        producer = SimpleNamespace(id="producer-1")
        request = self.factory.post(
            "/inventario/produtos/prod-1/reativar/",
            data={"next": "/inventario/stock/product-1/"},
            HTTP_HOST="testserver",
        )
        request.current_user = SimpleNamespace(id="user-1")
        get_producer_mock.return_value = producer
        reactivate_product_mock.return_value = (True, None)

        response = views.reativar_produto.__wrapped__(request, "producer-product-1")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/inventario/stock/product-1/")
        messages_mock.success.assert_called_once()
