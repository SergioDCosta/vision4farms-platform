from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from apps.accounts.models import AccountStatus, UserRole
from apps.needs.services import DuplicateActiveNeedError
from apps.recommendations.services import (
    RECOMMENDATION_DIRECTION_BALANCED,
    RECOMMENDATION_DIRECTION_BUY,
    RECOMMENDATION_DIRECTION_SELL,
    build_recommendation_inventory_rows,
    calculate_current_deficit,
)


class RecommendationStockDirectionTests(SimpleTestCase):
    def _metrics_for_stock(self, *, current, reserved, safety):
        stock = SimpleNamespace(
            current_quantity=Decimal(str(current)),
            reserved_quantity=Decimal(str(reserved)),
            safety_stock=Decimal(str(safety)),
        )
        with patch("apps.recommendations.services.Stock") as stock_model:
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

        with patch("apps.recommendations.services.Stock") as stock_model:
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
        need = SimpleNamespace(id=uuid4())
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
