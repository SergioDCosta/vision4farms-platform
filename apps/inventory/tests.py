from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from datetime import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from apps.catalog.services import CatalogValidationError
from apps.inventory.forms import CreateCustomProductForm, UpdateStockForm
from apps.inventory import views
from apps.inventory.services import (
    ListingsBlockStockReductionError,
    calculate_inventory_commitment_state,
    create_custom_product_for_producer,
    get_listings_blocking_stock_decrease,
    get_stock_state,
    producer_has_active_inventory_products,
    reduce_listings_to_fit_stock,
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

    @patch("apps.needs.services.calculate_external_demand_plan")
    def test_commitment_state_uses_forecast_cover_before_deadline(self, plan_mock):
        product = SimpleNamespace(id="product-1", name="Batata", unit="kg")
        producer = SimpleNamespace(id="producer-1")
        stock = SimpleNamespace(
            current_quantity=Decimal("500.000"),
            reserved_quantity=Decimal("0.000"),
            safety_stock=Decimal("625.000"),
        )
        plan_mock.return_value = {
            "total_external_demand": Decimal("625.000"),
            "available_stock_now": Decimal("500.000"),
            "total_forecast_relevant": Decimal("300.000"),
            "max_deficit": Decimal("0.000"),
            "first_deficit_date": None,
            "rows": [
                {
                    "delivery_date": "2026-06-01",
                    "demand_until_date": Decimal("125.000"),
                    "capacity_until_date": Decimal("500.000"),
                },
                {
                    "delivery_date": "2026-09-01",
                    "demand_until_date": Decimal("625.000"),
                    "capacity_until_date": Decimal("800.000"),
                },
            ],
        }

        commitment_state = calculate_inventory_commitment_state(producer, product, stock=stock)
        stock_state = get_stock_state(stock, commitment_state=commitment_state)

        self.assertNotEqual(stock_state["key"], "critical")
        self.assertEqual(commitment_state["temporal_sellable_quantity"], Decimal("175.000"))
        self.assertEqual(stock_state["publishable_quantity"], Decimal("175.000"))

    @patch("apps.needs.services.calculate_external_demand_plan")
    def test_commitment_state_is_critical_when_forecast_arrives_too_late(self, plan_mock):
        product = SimpleNamespace(id="product-1", name="Batata", unit="kg")
        producer = SimpleNamespace(id="producer-1")
        stock = SimpleNamespace(
            current_quantity=Decimal("500.000"),
            reserved_quantity=Decimal("0.000"),
            safety_stock=Decimal("625.000"),
        )
        plan_mock.return_value = {
            "total_external_demand": Decimal("625.000"),
            "available_stock_now": Decimal("500.000"),
            "total_forecast_relevant": Decimal("0.000"),
            "max_deficit": Decimal("125.000"),
            "first_deficit_date": "2026-06-01",
            "rows": [
                {
                    "delivery_date": "2026-06-01",
                    "demand_until_date": Decimal("625.000"),
                    "capacity_until_date": Decimal("500.000"),
                },
            ],
        }

        commitment_state = calculate_inventory_commitment_state(producer, product, stock=stock)
        stock_state = get_stock_state(stock, commitment_state=commitment_state)

        self.assertEqual(stock_state["key"], "critical")
        self.assertEqual(stock_state["deficit_quantity"], Decimal("125.000"))
        self.assertEqual(commitment_state["state_label"], "Défice temporal")
        self.assertIn("não chegam a tempo", commitment_state["state_message"])

    @patch("apps.needs.services.calculate_external_demand_plan")
    def test_commitment_state_without_external_demands_is_not_critical(self, plan_mock):
        product = SimpleNamespace(id="product-1", name="Batata", unit="kg")
        producer = SimpleNamespace(id="producer-1")
        stock = SimpleNamespace(
            current_quantity=Decimal("0.000"),
            reserved_quantity=Decimal("0.000"),
            safety_stock=Decimal("100.000"),
        )
        plan_mock.return_value = {
            "total_external_demand": Decimal("0.000"),
            "available_stock_now": Decimal("0.000"),
            "total_forecast_relevant": Decimal("0.000"),
            "max_deficit": Decimal("0.000"),
            "first_deficit_date": None,
            "rows": [],
        }

        commitment_state = calculate_inventory_commitment_state(producer, product, stock=stock)

        self.assertFalse(commitment_state["has_external_demands"])
        self.assertEqual(commitment_state["state_key"], "normal")


class InventoryStockFormTests(SimpleTestCase):
    def test_update_stock_form_accepts_decimal_quantities(self):
        form = UpdateStockForm(
            data={
                "new_quantity": "20.5",
                "movement_type": "MANUAL_ADJUSTMENT",
                "notes": "",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(str(form.cleaned_data["new_quantity"]), "20.5")
        self.assertNotIn("safety_stock", form.fields)


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


class ListingsBlockingStockDecreaseTests(SimpleTestCase):
    def _make_listing(self, listing_id, available, reserved=Decimal("0")):
        return SimpleNamespace(
            id=listing_id,
            quantity_available=available,
            quantity_reserved=reserved,
            created_at=None,
        )

    def _make_stock(self, current=Decimal("100"), reserved=Decimal("0")):
        return SimpleNamespace(
            id="stock-1",
            current_quantity=current,
            reserved_quantity=reserved,
            product=SimpleNamespace(name="Batata", unit="kg"),
        )

    @patch("apps.inventory.services.MarketplaceListing")
    def test_deficit_when_listings_exceed_new_quantity(self, listing_model):
        stock = self._make_stock(current=Decimal("150"), reserved=Decimal("0"))
        listings = [
            self._make_listing("l1", Decimal("100")),
            self._make_listing("l2", Decimal("20")),
        ]
        listing_model.objects.filter.return_value.select_related.return_value.order_by.return_value = listings

        blocking = get_listings_blocking_stock_decrease(stock, Decimal("80"))

        self.assertEqual(blocking["total_published"], Decimal("120.000"))
        self.assertEqual(blocking["min_required"], Decimal("120.000"))
        self.assertEqual(blocking["deficit"], Decimal("40.000"))
        self.assertEqual(len(blocking["affected_listings"]), 2)

    @patch("apps.inventory.services.MarketplaceListing")
    def test_no_deficit_when_new_quantity_covers_published(self, listing_model):
        stock = self._make_stock(current=Decimal("150"), reserved=Decimal("10"))
        listings = [self._make_listing("l1", Decimal("80"))]
        listing_model.objects.filter.return_value.select_related.return_value.order_by.return_value = listings

        blocking = get_listings_blocking_stock_decrease(stock, Decimal("120"))

        self.assertEqual(blocking["deficit"], Decimal("0.000"))
        self.assertEqual(blocking["min_required"], Decimal("90.000"))

    @patch("apps.inventory.services.MarketplaceListing")
    def test_deficit_considers_reserved_quantity(self, listing_model):
        stock = self._make_stock(current=Decimal("100"), reserved=Decimal("60"))
        listings = [self._make_listing("l1", Decimal("30"))]
        listing_model.objects.filter.return_value.select_related.return_value.order_by.return_value = listings

        blocking = get_listings_blocking_stock_decrease(stock, Decimal("50"))

        self.assertEqual(blocking["min_required"], Decimal("90.000"))
        self.assertEqual(blocking["deficit"], Decimal("40.000"))


class ReduceListingsToFitStockTests(SimpleTestCase):
    def _make_listing(self, listing_id, available, reserved=Decimal("0")):
        from apps.marketplace.models import ListingStatus

        listing = MagicMock()
        listing.id = listing_id
        listing.quantity_available = available
        listing.quantity_reserved = reserved
        listing.status = ListingStatus.ACTIVE
        return listing

    def _make_stock(self, current=Decimal("100"), reserved=Decimal("0")):
        return SimpleNamespace(
            id="stock-1",
            current_quantity=current,
            reserved_quantity=reserved,
            product=SimpleNamespace(name="Batata", unit="kg"),
        )

    @patch("apps.inventory.services.log_audit_event")
    @patch("apps.marketplace.services._listing_audit_values", return_value={})
    @patch("apps.marketplace.services.retire_listing")
    @patch("apps.inventory.services.MarketplaceListing")
    def test_proportional_distributes_remaining_capacity(
        self, listing_model, retire_mock, audit_values_mock, audit_event_mock
    ):
        stock = self._make_stock(current=Decimal("100"), reserved=Decimal("0"))
        l1 = self._make_listing("l1", Decimal("80"))
        l2 = self._make_listing("l2", Decimal("20"))
        listing_model.objects.select_for_update.return_value.filter.return_value.select_related.return_value.order_by.return_value = [l1, l2]

        result = reduce_listings_to_fit_stock(
            stock,
            Decimal("50"),
            mode="proportional",
            acting_user=None,
        )

        self.assertEqual(result["cancelled"], [])
        self.assertEqual(len(result["reduced"]), 2)
        self.assertEqual(l1.quantity_available, Decimal("40.000"))
        self.assertEqual(l2.quantity_available, Decimal("10.000"))
        retire_mock.assert_not_called()

    @patch("apps.inventory.services.log_audit_event")
    @patch("apps.marketplace.services._listing_audit_values", return_value={})
    @patch("apps.marketplace.services.retire_listing")
    @patch("apps.inventory.services.MarketplaceListing")
    def test_cancel_selected_retires_chosen_listings(
        self, listing_model, retire_mock, audit_values_mock, audit_event_mock
    ):
        stock = self._make_stock(current=Decimal("100"), reserved=Decimal("0"))
        l1 = self._make_listing("l1", Decimal("80"))
        l2 = self._make_listing("l2", Decimal("20"))
        listing_model.objects.select_for_update.return_value.filter.return_value.select_related.return_value.order_by.return_value = [l1, l2]

        result = reduce_listings_to_fit_stock(
            stock,
            Decimal("50"),
            mode="cancel_selected",
            listing_ids_to_cancel=["l1"],
            acting_user=None,
        )

        self.assertEqual(result["cancelled"], ["l1"])
        retire_mock.assert_called_once()
        # l2 (the remaining listing) should NOT have been reduced — 20 < 50 free capacity
        self.assertEqual(l2.quantity_available, Decimal("20"))

    def test_unknown_mode_raises_value_error(self):
        stock = self._make_stock()
        with patch("apps.inventory.services.MarketplaceListing"):
            with self.assertRaises(ValueError):
                reduce_listings_to_fit_stock(
                    stock, Decimal("50"), mode="bogus", acting_user=None
                )


class UpdateStockListingValidationTests(SimpleTestCase):
    def test_raises_when_listings_block_reduction(self):
        blocking = {
            "total_published": Decimal("100.000"),
            "reserved_quantity": Decimal("0.000"),
            "min_required": Decimal("100.000"),
            "deficit": Decimal("10.000"),
            "affected_listings": [],
        }
        err = ListingsBlockStockReductionError(blocking)
        self.assertIs(err.blocking, blocking)
        self.assertIn("anúncios ativos", str(err))
