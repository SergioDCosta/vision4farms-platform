from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.marketplace.models import MarketplaceListing
from apps.orders.models import Order, OrderItem, OrderStatus
from apps.orders.services import (
    build_presale_timeline_context,
    get_order_source_label,
    is_order_forecast_only,
)


class PresaleOrderClassificationTests(SimpleTestCase):
    def _build_order_with_listings(self, listings):
        order = Order()
        items = []
        for listing in listings:
            item = OrderItem()
            item.listing = listing
            items.append(item)
        order._prefetched_objects_cache = {"items": items}
        return order

    def test_forecast_only_order_is_detected(self):
        listing = MarketplaceListing()
        listing.forecast_id = "forecast-1"
        listing.stock_id = None
        order = self._build_order_with_listings([listing])

        self.assertTrue(is_order_forecast_only(order))
        self.assertEqual(get_order_source_label(order), "Pré-venda")

    def test_stock_only_order_is_not_presale(self):
        listing = MarketplaceListing()
        listing.forecast_id = None
        listing.stock_id = "stock-1"
        order = self._build_order_with_listings([listing])

        self.assertFalse(is_order_forecast_only(order))
        self.assertEqual(get_order_source_label(order), "Stock atual")

    def test_mixed_order_is_not_presale(self):
        forecast_listing = MarketplaceListing()
        forecast_listing.forecast_id = "forecast-1"
        forecast_listing.stock_id = None

        stock_listing = MarketplaceListing()
        stock_listing.forecast_id = None
        stock_listing.stock_id = "stock-1"

        order = self._build_order_with_listings([forecast_listing, stock_listing])

        self.assertFalse(is_order_forecast_only(order))
        self.assertEqual(get_order_source_label(order), "Origem mista")


class PresaleTimelineTests(SimpleTestCase):
    def _build_order(self, *, status, history_statuses=None):
        history_events = [
            SimpleNamespace(status=history_status)
            for history_status in (history_statuses or [])
        ]
        order = SimpleNamespace(
            status=status,
            status_history=history_events,
        )
        return order

    def _state_for_step(self, steps, key):
        return next(step["state"] for step in steps if step["key"] == key)

    def test_pending_order_marks_only_created_as_current(self):
        order = self._build_order(status=OrderStatus.PENDING)
        timeline = build_presale_timeline_context(order)

        self.assertEqual(timeline["state"], "normal")
        self.assertFalse(timeline["cancelled"])
        self.assertEqual(self._state_for_step(timeline["steps"], "created"), "current")
        self.assertEqual(self._state_for_step(timeline["steps"], "confirmed"), "pending")
        self.assertEqual(self._state_for_step(timeline["steps"], "in_progress"), "pending")
        self.assertEqual(self._state_for_step(timeline["steps"], "delivered"), "pending")

    def test_delivering_order_marks_final_step_as_current(self):
        order = self._build_order(
            status=OrderStatus.DELIVERING,
            history_statuses=[OrderStatus.CONFIRMED, OrderStatus.IN_PROGRESS],
        )
        timeline = build_presale_timeline_context(order)

        self.assertEqual(timeline["state"], "normal")
        self.assertEqual(self._state_for_step(timeline["steps"], "created"), "done")
        self.assertEqual(self._state_for_step(timeline["steps"], "confirmed"), "done")
        self.assertEqual(self._state_for_step(timeline["steps"], "in_progress"), "done")
        self.assertEqual(self._state_for_step(timeline["steps"], "delivered"), "current")

    def test_completed_order_marks_all_steps_done(self):
        order = self._build_order(
            status=OrderStatus.COMPLETED,
            history_statuses=[OrderStatus.CONFIRMED, OrderStatus.IN_PROGRESS, OrderStatus.DELIVERING],
        )
        timeline = build_presale_timeline_context(order)

        self.assertTrue(all(step["state"] == "done" for step in timeline["steps"]))

    def test_cancelled_order_marks_unreached_steps_as_interrupted(self):
        order = self._build_order(
            status=OrderStatus.CANCELLED,
            history_statuses=[OrderStatus.CONFIRMED],
        )
        timeline = build_presale_timeline_context(order)

        self.assertEqual(timeline["state"], "interrupted")
        self.assertTrue(timeline["cancelled"])
        self.assertEqual(self._state_for_step(timeline["steps"], "created"), "done")
        self.assertEqual(self._state_for_step(timeline["steps"], "confirmed"), "done")
        self.assertEqual(self._state_for_step(timeline["steps"], "in_progress"), "interrupted")
        self.assertEqual(self._state_for_step(timeline["steps"], "delivered"), "interrupted")
