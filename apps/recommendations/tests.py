from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from apps.accounts.models import AccountStatus, UserRole
from apps.needs.models import NeedSourceSystem, NeedStatus
from apps.needs.services import DuplicateActiveNeedError
from apps.recommendations.services import (
    RECOMMENDATION_DIRECTION_BALANCED,
    RECOMMENDATION_DIRECTION_BUY,
    RECOMMENDATION_DIRECTION_SELL,
    build_recommendation_inventory_rows,
    calculate_current_deficit,
    generate_recommendation,
)
from apps.recommendations.views import _build_sell_recommendation_context


class RecommendationStockDirectionTests(SimpleTestCase):
    def _metrics_for_stock(self, *, current, reserved, safety):
        stock = SimpleNamespace(
            current_quantity=Decimal(str(current)),
            reserved_quantity=Decimal(str(reserved)),
            safety_stock=Decimal(str(safety)),
        )
        available = Decimal(str(current)) - Decimal(str(reserved))
        buy_quantity = max(Decimal(str(safety)) - available, Decimal("0"))
        sell_quantity = max(available - Decimal(str(safety)), Decimal("0"))
        state_key = "critical" if buy_quantity > 0 else "excess" if sell_quantity > 0 else "normal"
        with (
            patch("apps.recommendations.services.Stock") as stock_model,
            patch(
                "apps.recommendations.services.calculate_inventory_commitment_state",
                return_value={
                    "total_external_demand": Decimal(str(safety)),
                    "reserved_quantity": Decimal(str(reserved)),
                    "current_quantity": Decimal(str(current)),
                    "available_stock_now": available,
                    "max_deficit": buy_quantity,
                    "temporal_sellable_quantity": sell_quantity,
                    "useful_forecast_total": Decimal("0.000"),
                    "state_key": state_key,
                },
            ),
        ):
            stock_model.objects.filter.return_value.first.return_value = stock
            return calculate_current_deficit(
                SimpleNamespace(id="producer-1"),
                SimpleNamespace(id="product-1"),
            )

    def test_surplus_stock_recommends_sale_down_to_safety_stock(self):
        metrics = self._metrics_for_stock(current=300, reserved=0, safety=100)

        self.assertEqual(metrics["recommendation_direction"], RECOMMENDATION_DIRECTION_SELL)
        self.assertEqual(metrics["sell_quantity"], Decimal("200.000"))
        self.assertEqual(metrics["suggested_quantity"], Decimal("200.000"))

    def test_low_stock_recommends_purchase_up_to_safety_stock(self):
        metrics = self._metrics_for_stock(current=50, reserved=0, safety=100)

        self.assertEqual(metrics["recommendation_direction"], RECOMMENDATION_DIRECTION_BUY)
        self.assertEqual(metrics["buy_quantity"], Decimal("50.000"))
        self.assertEqual(metrics["suggested_quantity"], Decimal("50.000"))

    def test_equal_stock_is_balanced(self):
        metrics = self._metrics_for_stock(current=100, reserved=0, safety=100)

        self.assertEqual(metrics["recommendation_direction"], RECOMMENDATION_DIRECTION_BALANCED)
        self.assertEqual(metrics["suggested_quantity"], Decimal("0.000"))

    def test_reserved_quantity_reduces_available_stock(self):
        sell_metrics = self._metrics_for_stock(current=300, reserved=50, safety=100)
        buy_metrics = self._metrics_for_stock(current=120, reserved=50, safety=100)

        self.assertEqual(sell_metrics["recommendation_direction"], RECOMMENDATION_DIRECTION_SELL)
        self.assertEqual(sell_metrics["sell_quantity"], Decimal("150.000"))
        self.assertEqual(buy_metrics["recommendation_direction"], RECOMMENDATION_DIRECTION_BUY)
        self.assertEqual(buy_metrics["buy_quantity"], Decimal("30.000"))

    def test_inventory_rows_split_buy_sell_and_balanced_products(self):
        products = [
            SimpleNamespace(id="buy-product", name="Alface", unit="kg"),
            SimpleNamespace(id="sell-product", name="Tomate", unit="kg"),
            SimpleNamespace(id="balanced-product", name="Pera", unit="kg"),
        ]
        stocks = [
            SimpleNamespace(
                product_id="buy-product",
                current_quantity=Decimal("50.000"),
                reserved_quantity=Decimal("0.000"),
                safety_stock=Decimal("100.000"),
            ),
            SimpleNamespace(
                product_id="sell-product",
                current_quantity=Decimal("300.000"),
                reserved_quantity=Decimal("0.000"),
                safety_stock=Decimal("100.000"),
            ),
            SimpleNamespace(
                product_id="balanced-product",
                current_quantity=Decimal("100.000"),
                reserved_quantity=Decimal("0.000"),
                safety_stock=Decimal("100.000"),
            ),
        ]

        commitment_by_product = {
            "buy-product": {
                "total_external_demand": Decimal("100.000"),
                "reserved_quantity": Decimal("0.000"),
                "current_quantity": Decimal("50.000"),
                "available_stock_now": Decimal("50.000"),
                "max_deficit": Decimal("50.000"),
                "temporal_sellable_quantity": Decimal("0.000"),
                "useful_forecast_total": Decimal("0.000"),
                "state_key": "critical",
            },
            "sell-product": {
                "total_external_demand": Decimal("100.000"),
                "reserved_quantity": Decimal("0.000"),
                "current_quantity": Decimal("300.000"),
                "available_stock_now": Decimal("300.000"),
                "max_deficit": Decimal("0.000"),
                "temporal_sellable_quantity": Decimal("200.000"),
                "useful_forecast_total": Decimal("0.000"),
                "state_key": "excess",
            },
            "balanced-product": {
                "total_external_demand": Decimal("100.000"),
                "reserved_quantity": Decimal("0.000"),
                "current_quantity": Decimal("100.000"),
                "available_stock_now": Decimal("100.000"),
                "max_deficit": Decimal("0.000"),
                "temporal_sellable_quantity": Decimal("0.000"),
                "useful_forecast_total": Decimal("0.000"),
                "state_key": "normal",
            },
        }

        def commitment_side_effect(producer, product, stock=None):
            return commitment_by_product[str(product.id)]

        with (
            patch("apps.recommendations.services.Stock") as stock_model,
            patch(
                "apps.recommendations.services.calculate_inventory_commitment_state",
                side_effect=commitment_side_effect,
            ),
        ):
            stock_model.objects.filter.return_value.only.return_value = stocks
            rows = build_recommendation_inventory_rows(
                SimpleNamespace(id="producer-1"),
                products,
            )

        self.assertEqual(rows["buy_rows"][0]["product_id"], "buy-product")
        self.assertEqual(rows["buy_rows"][0]["buy_quantity"], Decimal("50.000"))
        self.assertEqual(rows["sell_rows"][0]["product_id"], "sell-product")
        self.assertEqual(rows["sell_rows"][0]["sell_quantity"], Decimal("200.000"))
        self.assertEqual(rows["balanced_rows"][0]["product_id"], "balanced-product")

    def test_confirmed_coverage_of_customer_demand_reduces_purchase_recommendation(self):
        need = SimpleNamespace(
            source_system=NeedSourceSystem.CUSTOMER_DEMAND,
            status=NeedStatus.OPEN,
            needed_by_date=None,
            is_marketplace_published=True,
        )
        stock = SimpleNamespace()
        state = {
            "total_external_demand": Decimal("100.000"),
            "reserved_quantity": Decimal("0.000"),
            "current_quantity": Decimal("0.000"),
            "available_stock_now": Decimal("0.000"),
            "max_deficit": Decimal("100.000"),
            "temporal_sellable_quantity": Decimal("0.000"),
            "useful_forecast_total": Decimal("0.000"),
            "first_deficit_date": date(2026, 6, 1),
            "state_key": "critical",
            "plan": {"generated_need": need},
        }

        with (
            patch("apps.recommendations.services.Stock") as stock_model,
            patch("apps.recommendations.services.calculate_inventory_commitment_state", return_value=state),
            patch("apps.recommendations.services.calculate_need_coverage", return_value={"planned_qty": Decimal("40.000")}),
        ):
            stock_model.objects.filter.return_value.first.return_value = stock
            metrics = calculate_current_deficit(
                SimpleNamespace(id="producer-1"),
                SimpleNamespace(id="product-1"),
            )

        self.assertIs(metrics["customer_demand_need"], need)
        self.assertEqual(metrics["physical_deficit_quantity"], Decimal("100.000"))
        self.assertEqual(metrics["planned_need_quantity"], Decimal("40.000"))
        self.assertEqual(metrics["buy_quantity"], Decimal("60.000"))
        self.assertEqual(metrics["recommendation_direction"], RECOMMENDATION_DIRECTION_BUY)

    def test_covered_customer_demand_does_not_generate_duplicate_purchase(self):
        need = SimpleNamespace(
            source_system=NeedSourceSystem.CUSTOMER_DEMAND,
            status=NeedStatus.COVERED,
            needed_by_date=None,
            is_marketplace_published=False,
        )
        state = {
            "total_external_demand": Decimal("100.000"),
            "reserved_quantity": Decimal("0.000"),
            "current_quantity": Decimal("0.000"),
            "available_stock_now": Decimal("0.000"),
            "max_deficit": Decimal("100.000"),
            "temporal_sellable_quantity": Decimal("0.000"),
            "useful_forecast_total": Decimal("0.000"),
            "first_deficit_date": date(2026, 6, 1),
            "state_key": "critical",
            "plan": {"generated_need": need},
        }

        with (
            patch("apps.recommendations.services.Stock") as stock_model,
            patch("apps.recommendations.services.calculate_inventory_commitment_state", return_value=state),
            patch("apps.recommendations.services.calculate_need_coverage", return_value={"planned_qty": Decimal("100.000")}),
        ):
            stock_model.objects.filter.return_value.first.return_value = SimpleNamespace()
            metrics = calculate_current_deficit(
                SimpleNamespace(id="producer-1"),
                SimpleNamespace(id="product-1"),
            )

        self.assertEqual(metrics["buy_quantity"], Decimal("0.000"))
        self.assertEqual(metrics["recommendation_direction"], RECOMMENDATION_DIRECTION_BALANCED)


class RecommendationDeadlineTests(SimpleTestCase):
    def test_late_forecast_is_not_selected_and_purchase_keeps_automatic_need_link(self):
        producer = SimpleNamespace(id="producer-1")
        seller = SimpleNamespace(id="seller-1")
        product = SimpleNamespace(id="product-1", name="Batata", unit="kg")
        need = SimpleNamespace(id="need-1")
        listing = SimpleNamespace(
            forecast_id="forecast-1",
            forecast=SimpleNamespace(period_start=date(2026, 6, 15), period_end=date(2026, 6, 30)),
            quantity_available=Decimal("50.000"),
            producer=seller,
            product=product,
            unit_price=Decimal("1.00"),
        )
        recommendation = SimpleNamespace(id="recommendation-1")
        generate = getattr(generate_recommendation, "__wrapped__", generate_recommendation)

        with (
            patch("apps.recommendations.services._get_candidate_listings", return_value=[listing]),
            patch("apps.recommendations.services.Recommendation.objects.create", return_value=recommendation) as create,
            patch("apps.recommendations.services.RecommendationItem.objects.create") as create_item,
        ):
            result = generate(
                producer=producer,
                product=product,
                requested_quantity=Decimal("25.000"),
                deadline_date=date(2026, 6, 1),
                need=need,
            )

        self.assertIs(result, recommendation)
        self.assertIs(create.call_args.kwargs["need"], need)
        self.assertEqual(create.call_args.kwargs["deficit_quantity"], Decimal("25.000"))
        create_item.assert_not_called()


class RecommendationNeedCreationViewTests(SimpleTestCase):
    def _request(self):
        request = RequestFactory().post("/recomendacoes/recommendation-1/necessidade/")
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
        )
        return request

    def test_create_need_does_not_update_existing_active_need_silently(self):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1", name="Alface", unit="kg")
        recommendation = SimpleNamespace(
            id=uuid4(),
            producer=producer,
            product=product,
            need_id=None,
            deficit_quantity=Decimal("20.000"),
        )
        existing_need = SimpleNamespace(id=uuid4())

        with (
            patch("apps.recommendations.views._get_current_producer", return_value=producer),
            patch("apps.recommendations.views.get_object_or_404", return_value=recommendation),
            patch("apps.recommendations.views._build_step_2_context", return_value={"wizard_step": 2}),
            patch("apps.recommendations.views._render_wizard", return_value=HttpResponse("ok")),
            patch("apps.recommendations.views.create_need", side_effect=DuplicateActiveNeedError(existing_need)) as create,
            patch("apps.recommendations.views.update_need") as update,
        ):
            from apps.recommendations.views import recommendations_create_need_view

            response = recommendations_create_need_view(self._request(), recommendation.id)

        self.assertEqual(response.status_code, 200)
        create.assert_called_once()
        update.assert_not_called()
        self.assertIsNone(recommendation.need_id)

    def test_existing_recommendation_need_uses_explicit_update(self):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1", name="Alface", unit="kg")
        need = SimpleNamespace(id=uuid4(), source_system=NeedSourceSystem.VISION4FARMS)
        recommendation = SimpleNamespace(
            id=uuid4(),
            producer=producer,
            product=product,
            need=need,
            need_id=need.id,
            deadline_date=None,
            deficit_quantity=Decimal("20.000"),
            updated_at=None,
            save=lambda update_fields=None: None,
        )

        with (
            patch("apps.recommendations.views._get_current_producer", return_value=producer),
            patch("apps.recommendations.views.get_object_or_404", return_value=recommendation),
            patch("apps.recommendations.views._build_step_2_context", return_value={"wizard_step": 2}),
            patch("apps.recommendations.views._render_wizard", return_value=HttpResponse("ok")),
            patch("apps.recommendations.views.update_need", return_value=(need, {}, True)) as update,
            patch("apps.recommendations.views.create_need") as create,
        ):
            from apps.recommendations.views import recommendations_create_need_view

            response = recommendations_create_need_view(self._request(), recommendation.id)

        self.assertEqual(response.status_code, 200)
        update.assert_called_once()
        create.assert_not_called()

    def test_customer_demand_need_is_published_instead_of_manually_updated(self):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1", name="Alface", unit="kg")
        need = SimpleNamespace(id=uuid4(), source_system=NeedSourceSystem.CUSTOMER_DEMAND)
        recommendation = SimpleNamespace(
            id=uuid4(),
            producer=producer,
            product=product,
            need=need,
            need_id=need.id,
            deadline_date=None,
            deficit_quantity=Decimal("20.000"),
        )

        with (
            patch("apps.recommendations.views._get_current_producer", return_value=producer),
            patch("apps.recommendations.views.get_object_or_404", return_value=recommendation),
            patch("apps.recommendations.views._build_step_2_context", return_value={"wizard_step": 2}),
            patch("apps.recommendations.views._render_wizard", return_value=HttpResponse("ok")),
            patch("apps.recommendations.views.publish_need_to_marketplace", return_value=(need, True)) as publish,
            patch("apps.recommendations.views.update_need") as update,
            patch("apps.recommendations.views.create_need") as create,
            patch("apps.recommendations.views._sync_alerts_after_need_change"),
        ):
            from apps.recommendations.views import recommendations_create_need_view

            response = recommendations_create_need_view(self._request(), recommendation.id)

        self.assertEqual(response.status_code, 200)
        publish.assert_called_once()
        update.assert_not_called()
        create.assert_not_called()


class RecommendationSaleActionTests(SimpleTestCase):
    def test_sell_step_links_to_active_listing_management_when_already_published(self):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1", name="Alface", unit="kg")
        listing = SimpleNamespace(id=uuid4(), quantity_available=Decimal("12.000"))
        queryset = MagicMock()
        queryset.filter.return_value.first.return_value = listing

        with (
            patch("apps.recommendations.views.list_marketplace_public_needs", return_value=[]),
            patch("apps.recommendations.views.get_my_listings", return_value=queryset),
            patch("apps.recommendations.views.Stock") as stock_model,
        ):
            stock_model.objects.filter.return_value.first.return_value = None
            context = _build_sell_recommendation_context(
                producer=producer,
                product=product,
                requested_quantity=Decimal("20.000"),
                metrics={},
            )

        self.assertIs(context["active_listing"], listing)
        self.assertEqual(context["manage_listing_url"], f"/marketplace/meus/{listing.id}/")
