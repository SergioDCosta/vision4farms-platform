from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.marketplace.models import MarketplaceListing
from apps.needs.models import NeedResponseStatus
from apps.orders.models import Order, OrderItem, OrderStatus
from apps.orders.services import (
    OrderServiceError,
    _notify_order_purchase_created,
    build_presale_timeline_context,
    create_order_from_listing,
    get_order_source_label,
    is_order_from_need_response,
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

    def test_need_response_order_has_explicit_source_label(self):
        listing = MarketplaceListing()
        listing.forecast_id = None
        listing.stock_id = "stock-1"
        listing.need_id = "need-1"

        item = OrderItem()
        item.listing = listing
        item.need_id = "need-1"
        order = self._build_order_with_listings([listing])
        order._prefetched_objects_cache = {"items": [item]}

        self.assertTrue(is_order_from_need_response(order))
        self.assertFalse(is_order_forecast_only(order))
        self.assertEqual(get_order_source_label(order), "Resposta a necessidade")

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


class NeedResponseOrderTests(SimpleTestCase):
    def test_rejected_need_response_cannot_create_order(self):
        listing = MarketplaceListing()
        listing.producer_id = "seller-1"
        listing.need_id = "need-1"
        listing.need_response_status = NeedResponseStatus.REJECTED

        create = getattr(create_order_from_listing, "__wrapped__", create_order_from_listing)

        with self.assertRaisesMessage(OrderServiceError, "oferta foi rejeitada"):
            create(
                buyer_producer=type("Producer", (), {"id": "buyer-1"})(),
                listing=listing,
                quantity="1",
                acting_user=None,
                need=type("Need", (), {"id": "need-1"})(),
            )

    def test_need_response_purchase_alert_mentions_accepted_offer(self):
        order = SimpleNamespace(id="order-1", order_number=123)
        buyer = SimpleNamespace(id="buyer-1", display_name="Diogo")
        seller = SimpleNamespace(id="seller-1")

        with (
            patch("apps.orders.services.is_order_from_need_response", return_value=True),
            patch("apps.orders.services._build_order_alert_summary", return_value="50.000 kg de Pera Rocha"),
            patch("apps.orders.services._safe_emit_order_interaction_alert") as emit,
        ):
            _notify_order_purchase_created(
                order=order,
                buyer_producer=buyer,
                seller_producer=seller,
                acting_user=None,
            )

        self.assertIn("oferta foi aceite", emit.call_args.kwargs["title"])
        self.assertIn("aceitou a sua oferta privada para uma necessidade", emit.call_args.kwargs["description"])
