from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.recommendations.services import (
    RECOMMENDATION_DIRECTION_BALANCED,
    RECOMMENDATION_DIRECTION_BUY,
    RECOMMENDATION_DIRECTION_SELL,
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
