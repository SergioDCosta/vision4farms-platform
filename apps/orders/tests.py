from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.marketplace.models import MarketplaceListing
from apps.needs.models import NeedResponseStatus
from apps.orders.forms import BuyerCancelOrderForm
from apps.orders.models import Order, OrderItem, OrderItemStatus, OrderStatus
from apps.orders.services import (
    OrderServiceError,
    _quantity_label,
    _notify_order_purchase_created,
    _reconcile_listings_against_stock_capacity,
    _update_stock_reserved,
    buyer_cancel_order,
    build_presale_timeline_context,
    create_order_from_listing,
    get_order_source_label,
    is_order_from_need_response,
    is_order_forecast_only,
)


class OrderQuantityLabelTests(SimpleTestCase):
    def test_quantity_label_trims_unneeded_decimal_places(self):
        self.assertEqual(_quantity_label("200.000", "kg"), "200 kg")


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


class BuyerOrderCancellationTests(SimpleTestCase):
    def test_buyer_cancel_form_requires_reason(self):
        form = BuyerCancelOrderForm({"cancel_reason": "", "notes": ""})

        self.assertFalse(form.is_valid())
        self.assertIn("cancel_reason", form.errors)

    def test_buyer_cancellation_releases_listings_and_records_reason(self):
        buyer = SimpleNamespace(id="buyer-1")
        seller = SimpleNamespace(id="seller-1")
        order = SimpleNamespace(
            id="order-1",
            buyer_producer_id=buyer.id,
            buyer_producer=buyer,
            status=OrderStatus.CONFIRMED,
        )
        item = SimpleNamespace(
            listing_id="listing-1",
            seller_producer=seller,
            item_status=OrderItemStatus.CONFIRMED,
            save=MagicMock(),
        )
        order_manager = MagicMock()
        order_manager.select_for_update.return_value.get.return_value = order
        items_query = MagicMock()
        items_query.select_related.return_value.filter.return_value.exclude.return_value = [item]
        cancel = getattr(buyer_cancel_order, "__wrapped__", buyer_cancel_order)

        def mark_cancelled(current_order):
            current_order.status = OrderStatus.CANCELLED
            return current_order

        with (
            patch("apps.orders.services.Order.objects", order_manager),
            patch("apps.orders.services.OrderItem.objects", items_query),
            patch("apps.orders.services._reconcile_listing_reservation") as reconcile,
            patch("apps.orders.services._sync_need_response_statuses_for_listing_ids") as sync_responses,
            patch("apps.orders.services._recalculate_order_status", side_effect=mark_cancelled),
            patch("apps.orders.services._create_status_history") as create_history,
            patch("apps.orders.services.recalculate_needs_for_order") as recalculate_needs,
            patch("apps.orders.services._sync_alerts_for_producers"),
            patch("apps.orders.services._log_order_status_change") as audit_status,
        ):
            result = cancel(
                order=order,
                buyer_producer=buyer,
                acting_user=None,
                notes="Cancelada pelo comprador. Motivo: Erro no pedido",
            )

        self.assertIs(result, order)
        self.assertEqual(item.item_status, OrderItemStatus.CANCELLED)
        item.save.assert_called_once()
        reconcile.assert_called_once_with("listing-1", None)
        sync_responses.assert_called_once_with({"listing-1"})
        create_history.assert_called_once()
        self.assertIn("Motivo: Erro no pedido", create_history.call_args.kwargs["notes"])
        recalculate_needs.assert_called_once_with(order, acting_user=None)
        audit_status.assert_called_once()

    def test_buyer_cannot_cancel_order_already_in_delivery(self):
        buyer = SimpleNamespace(id="buyer-1")
        order = SimpleNamespace(
            id="order-1",
            buyer_producer_id=buyer.id,
            status=OrderStatus.DELIVERING,
        )
        order_manager = MagicMock()
        order_manager.select_for_update.return_value.get.return_value = order
        cancel = getattr(buyer_cancel_order, "__wrapped__", buyer_cancel_order)

        with (
            patch("apps.orders.services.Order.objects", order_manager),
            self.assertRaisesMessage(OrderServiceError, "em entrega ou concluída"),
        ):
            cancel(order=order, buyer_producer=buyer, acting_user=None)


class StockReservationCapacityTests(SimpleTestCase):
    def _make_stock(self, current=Decimal("100"), reserved=Decimal("0")):
        return SimpleNamespace(
            id="stock-1",
            product_id="product-1",
            product=SimpleNamespace(name="Batatas", unit="kg"),
            current_quantity=current,
            reserved_quantity=reserved,
            updated_by=None,
            last_updated_at=None,
            updated_at=None,
            save=MagicMock(),
        )

    def test_update_stock_reserved_raises_when_exceeds_current(self):
        stock = self._make_stock(current=Decimal("90"), reserved=Decimal("0"))

        with (
            patch("apps.orders.services.log_audit_event"),
            self.assertRaisesMessage(
                OrderServiceError, "O stock de Batatas já não chega"
            ),
        ):
            _update_stock_reserved(stock, Decimal("100"), acting_user=None)

        stock.save.assert_not_called()

    def test_update_stock_reserved_succeeds_within_capacity(self):
        stock = self._make_stock(current=Decimal("100"), reserved=Decimal("20"))

        with patch("apps.orders.services.log_audit_event") as audit:
            _update_stock_reserved(stock, Decimal("30"), acting_user=None)

        self.assertEqual(stock.reserved_quantity, Decimal("50.000"))
        stock.save.assert_called_once()
        audit.assert_called_once()

    def test_reconcile_listings_against_stock_skips_when_no_deficit(self):
        stock = self._make_stock(current=Decimal("100"), reserved=Decimal("0"))
        with (
            patch(
                "apps.inventory.services.get_listings_blocking_stock_decrease",
                return_value={"deficit": Decimal("0.000")},
            ) as blocking,
            patch(
                "apps.inventory.services.reduce_listings_to_fit_stock"
            ) as reducer,
        ):
            _reconcile_listings_against_stock_capacity(stock, acting_user=None)
        blocking.assert_called_once()
        reducer.assert_not_called()

    def test_reconcile_listings_against_stock_reduces_on_deficit(self):
        stock = self._make_stock(current=Decimal("80"), reserved=Decimal("0"))
        with (
            patch(
                "apps.inventory.services.get_listings_blocking_stock_decrease",
                return_value={"deficit": Decimal("20.000")},
            ),
            patch(
                "apps.inventory.services.reduce_listings_to_fit_stock"
            ) as reducer,
        ):
            _reconcile_listings_against_stock_capacity(stock, acting_user="user-1")
        reducer.assert_called_once()
        kwargs = reducer.call_args.kwargs
        self.assertEqual(kwargs["mode"], "proportional")
        self.assertEqual(kwargs["new_quantity"], Decimal("80"))
        self.assertEqual(kwargs["acting_user"], "user-1")
