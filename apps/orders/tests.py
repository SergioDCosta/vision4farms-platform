from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.inventory.models import StockMovementType
from apps.marketplace.models import MarketplaceListing
from apps.needs.models import NeedResponseStatus
from apps.orders.forms import BuyerCancelOrderForm
from apps.orders.models import Order, OrderItem, OrderItemStatus, OrderStatus
from apps.orders.services import (
    OrderServiceError,
    _quantity_label,
    _consume_stock_reservation,
    _notify_order_purchase_created,
    _reconcile_listing_reservation,
    _reconcile_listings_against_stock_capacity,
    _register_buyer_order_inbound,
    _update_stock_reserved,
    buyer_cancel_order,
    build_presale_timeline_context,
    confirm_order_receipt,
    create_order_from_listing,
    get_order_source_label,
    is_order_from_need_response,
    is_order_forecast_only,
    reconcile_order_status,
    seller_update_order_status,
)


class OrderQuantityLabelTests(SimpleTestCase):
    def test_quantity_label_trims_unneeded_decimal_places(self):
        self.assertEqual(_quantity_label("200.000", "kg"), "200 kg")


class OrderStatusReconciliationTests(SimpleTestCase):
    @patch("apps.orders.services._create_status_history")
    @patch("apps.orders.services._set_order_status")
    def test_public_reconciliation_api_updates_status_and_history(self, set_status_mock, history_mock):
        order = SimpleNamespace(id="order-1", status=OrderStatus.PENDING)

        changed = reconcile_order_status.__wrapped__(
            order,
            expected_status=OrderStatus.CONFIRMED,
        )

        self.assertTrue(changed)
        set_status_mock.assert_called_once_with(order, OrderStatus.CONFIRMED)
        history_mock.assert_called_once()


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

    def test_accepting_need_response_links_order_item_to_need_and_recalculates_coverage(self):
        buyer = SimpleNamespace(id="buyer-1")
        seller = SimpleNamespace(id="seller-1")
        product = SimpleNamespace(id="product-1", name="Batata")
        need = SimpleNamespace(id="need-1")
        listing = SimpleNamespace(
            id="listing-1",
            need_id=need.id,
            need_response_status=NeedResponseStatus.PENDING,
            stock_id="stock-1",
            forecast_id=None,
            unit_price=Decimal("2.00"),
            product=product,
            product_id=product.id,
            producer=seller,
            producer_id=seller.id,
        )
        order_group = SimpleNamespace(id="group-1")
        order = SimpleNamespace(
            id="order-1",
            order_number=1,
            buyer_producer_id=buyer.id,
            status=OrderStatus.PENDING,
            total_amount=Decimal("20.00"),
            source_type="MARKETPLACE",
        )
        item_manager = MagicMock()
        item_manager.filter.return_value.exists.return_value = False
        create = getattr(create_order_from_listing, "__wrapped__", create_order_from_listing)

        with (
            patch("apps.orders.services._lock_listing_for_order", return_value=listing),
            patch("apps.orders.services._validate_listing_can_be_ordered"),
            patch("apps.orders.services._validate_listing_source_xor"),
            patch("apps.orders.services._create_order_group_with_retry", return_value=order_group),
            patch("apps.orders.services._create_order_with_retry", return_value=order),
            patch("apps.orders.services._map_delivery_method_from_listing", return_value="PICKUP"),
            patch("apps.orders.services.OrderItem.objects", item_manager),
            patch("apps.orders.services._reconcile_listing_reservation"),
            patch("apps.orders.services._sync_need_response_statuses_for_listing_ids"),
            patch("apps.orders.services._create_status_history"),
            patch("apps.orders.services.log_audit_event"),
            patch("apps.orders.services._notify_order_purchase_created"),
            patch("apps.orders.services.recalculate_needs_for_order") as recalculate_needs,
            patch("apps.orders.services._sync_alerts_for_producers"),
        ):
            create(
                buyer_producer=buyer,
                listing=listing,
                quantity="10",
                acting_user=None,
                need=need,
            )

        self.assertIs(item_manager.create.call_args.kwargs["need"], need)
        recalculate_needs.assert_called_once_with(order, acting_user=None)


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

    def test_buyer_cannot_cancel_completed_order(self):
        buyer = SimpleNamespace(id="buyer-1")
        order = SimpleNamespace(
            id="order-1",
            buyer_producer_id=buyer.id,
            status=OrderStatus.COMPLETED,
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


class MarketplaceOrderLifecycleTests(SimpleTestCase):
    def _make_listing(self, *, available=Decimal("100"), reserved=Decimal("0")):
        return SimpleNamespace(
            id="listing-1",
            stock_id="stock-1",
            forecast_id=None,
            status="ACTIVE",
            quantity_available=available,
            quantity_reserved=reserved,
            product=SimpleNamespace(unit="kg"),
            updated_at=None,
            save=MagicMock(),
        )

    def test_pending_order_reserves_listing_and_seller_stock_immediately(self):
        listing = self._make_listing()
        listing_manager = MagicMock()
        listing_manager.select_for_update.return_value.get.return_value = listing

        with (
            patch("apps.orders.services.MarketplaceListing.objects", listing_manager),
            patch("apps.orders.services._expected_reserved_quantity_for_listing", return_value=Decimal("30.000")),
            patch("apps.orders.services._update_stock_reserved") as update_stock_reserved,
            patch("apps.orders.services.Stock.objects") as stock_manager,
            patch("apps.orders.services._log_listing_status_if_changed"),
        ):
            stock = SimpleNamespace(id="stock-1")
            stock_manager.select_for_update.return_value.get.return_value = stock
            _reconcile_listing_reservation("listing-1", acting_user=None)

        self.assertEqual(listing.quantity_available, Decimal("70.000"))
        self.assertEqual(listing.quantity_reserved, Decimal("30.000"))
        update_stock_reserved.assert_called_once_with(stock, Decimal("30.000"), None)

    def test_reconciling_confirmation_does_not_duplicate_existing_reservation(self):
        listing = self._make_listing(available=Decimal("70.000"), reserved=Decimal("30.000"))
        listing_manager = MagicMock()
        listing_manager.select_for_update.return_value.get.return_value = listing

        with (
            patch("apps.orders.services.MarketplaceListing.objects", listing_manager),
            patch("apps.orders.services._expected_reserved_quantity_for_listing", return_value=Decimal("30.000")),
            patch("apps.orders.services._update_stock_reserved") as update_stock_reserved,
        ):
            _reconcile_listing_reservation("listing-1", acting_user=None)

        listing.save.assert_not_called()
        update_stock_reserved.assert_not_called()
        self.assertEqual(listing.quantity_reserved, Decimal("30.000"))

    def test_seller_confirmation_changes_state_and_records_history_without_new_reservation(self):
        seller = SimpleNamespace(id="seller-1")
        order = SimpleNamespace(
            id="order-1",
            status=OrderStatus.PENDING,
            buyer_producer=SimpleNamespace(id="buyer-1"),
        )
        item = SimpleNamespace(
            listing_id="listing-1",
            item_status=OrderItemStatus.PENDING,
            updated_at=None,
            save=MagicMock(),
        )
        order_manager = MagicMock()
        order_manager.select_for_update.return_value.get.return_value = order
        item_manager = MagicMock()
        item_manager.select_related.return_value.filter.return_value = [item]
        confirm = getattr(seller_update_order_status, "__wrapped__", seller_update_order_status)

        with (
            patch("apps.orders.services.Order.objects", order_manager),
            patch("apps.orders.services.OrderItem.objects", item_manager),
            patch("apps.orders.services._reconcile_listing_reservation") as reconcile,
            patch("apps.orders.services._sync_need_response_statuses_for_listing_ids"),
            patch("apps.orders.services._recalculate_order_status"),
            patch("apps.orders.services._create_status_history") as status_history,
            patch("apps.orders.services._notify_order_status_changed_to_buyer"),
            patch("apps.orders.services.recalculate_needs_for_order"),
            patch("apps.orders.services._sync_alerts_for_producers"),
            patch("apps.orders.services._log_order_status_change"),
        ):
            confirm(
                order=order,
                seller_producer=seller,
                new_status=OrderStatus.CONFIRMED,
                acting_user=None,
            )

        self.assertEqual(item.item_status, OrderItemStatus.CONFIRMED)
        item.save.assert_called_once()
        reconcile.assert_called_once_with("listing-1", None, strict=False)
        status_history.assert_called_once()

    def test_seller_marks_delivery_without_consuming_physical_stock(self):
        seller = SimpleNamespace(id="seller-1")
        order = SimpleNamespace(
            id="order-1",
            status=OrderStatus.IN_PROGRESS,
            buyer_producer=SimpleNamespace(id="buyer-1"),
        )
        item = SimpleNamespace(
            listing_id="listing-1",
            item_status=OrderItemStatus.CONFIRMED,
            updated_at=None,
            save=MagicMock(),
        )
        order_manager = MagicMock()
        order_manager.select_for_update.return_value.get.return_value = order
        item_manager = MagicMock()
        item_manager.select_related.return_value.filter.return_value = [item]
        deliver = getattr(seller_update_order_status, "__wrapped__", seller_update_order_status)

        with (
            patch("apps.orders.services.Order.objects", order_manager),
            patch("apps.orders.services.OrderItem.objects", item_manager),
            patch("apps.orders.services.OrderStatusHistory.objects.filter") as history_filter,
            patch("apps.orders.services._consume_listing_reservation") as consume_reservation,
            patch("apps.orders.services._recalculate_order_status"),
            patch("apps.orders.services._create_status_history") as status_history,
            patch("apps.orders.services._notify_order_status_changed_to_buyer"),
            patch("apps.orders.services.recalculate_needs_for_order"),
            patch("apps.orders.services._sync_alerts_for_producers"),
            patch("apps.orders.services._log_order_status_change"),
        ):
            history_filter.return_value.exists.return_value = True
            deliver(
                order=order,
                seller_producer=seller,
                new_status=OrderStatus.DELIVERING,
                acting_user=None,
            )

        self.assertEqual(item.item_status, OrderItemStatus.IN_DELIVERY)
        item.save.assert_called_once()
        consume_reservation.assert_not_called()
        status_history.assert_called_once()

    def test_seller_cancellation_releases_reservation_and_records_reason_for_buyer(self):
        seller = SimpleNamespace(id="seller-1")
        buyer = SimpleNamespace(id="buyer-1")
        order = SimpleNamespace(
            id="order-1",
            status=OrderStatus.CONFIRMED,
            buyer_producer=buyer,
        )
        item = SimpleNamespace(
            listing_id="listing-1",
            item_status=OrderItemStatus.CONFIRMED,
            updated_at=None,
            save=MagicMock(),
        )
        order_manager = MagicMock()
        order_manager.select_for_update.return_value.get.return_value = order
        item_manager = MagicMock()
        item_manager.select_related.return_value.filter.return_value = [item]
        cancel = getattr(seller_update_order_status, "__wrapped__", seller_update_order_status)

        def mark_cancelled(current_order):
            current_order.status = OrderStatus.CANCELLED
            return current_order

        with (
            patch("apps.orders.services.Order.objects", order_manager),
            patch("apps.orders.services.OrderItem.objects", item_manager),
            patch("apps.orders.services._reconcile_listing_reservation") as reconcile,
            patch("apps.orders.services._sync_need_response_statuses_for_listing_ids"),
            patch("apps.orders.services._recalculate_order_status", side_effect=mark_cancelled),
            patch("apps.orders.services._create_status_history") as status_history,
            patch("apps.orders.services._notify_order_status_changed_to_buyer") as notify_buyer,
            patch("apps.orders.services.recalculate_needs_for_order"),
            patch("apps.orders.services._sync_alerts_for_producers"),
            patch("apps.orders.services._log_order_status_change"),
        ):
            cancel(
                order=order,
                seller_producer=seller,
                new_status=OrderStatus.CANCELLED,
                acting_user=None,
                notes="Motivo: Sem stock disponível",
            )

        self.assertEqual(item.item_status, OrderItemStatus.CANCELLED)
        reconcile.assert_called_once_with("listing-1", None)
        self.assertIn("Motivo: Sem stock disponível", status_history.call_args.kwargs["notes"])
        notify_buyer.assert_called_once()
        self.assertEqual(notify_buyer.call_args.kwargs["status"], OrderStatus.CANCELLED)

    def test_seller_cannot_cancel_completed_order(self):
        seller = SimpleNamespace(id="seller-1")
        order = SimpleNamespace(
            id="order-1",
            status=OrderStatus.COMPLETED,
            buyer_producer=SimpleNamespace(id="buyer-1"),
        )
        item = SimpleNamespace(item_status=OrderItemStatus.COMPLETED)
        order_manager = MagicMock()
        order_manager.select_for_update.return_value.get.return_value = order
        item_manager = MagicMock()
        item_manager.select_related.return_value.filter.return_value = [item]
        cancel = getattr(seller_update_order_status, "__wrapped__", seller_update_order_status)

        with (
            patch("apps.orders.services.Order.objects", order_manager),
            patch("apps.orders.services.OrderItem.objects", item_manager),
            self.assertRaisesMessage(OrderServiceError, "já não pode ser alterada"),
        ):
            cancel(
                order=order,
                seller_producer=seller,
                new_status=OrderStatus.CANCELLED,
                acting_user=None,
            )

    def test_buyer_receipt_consumes_seller_reservation_and_registers_inbound_stock(self):
        buyer = SimpleNamespace(id="buyer-1")
        seller = SimpleNamespace(id="seller-1")
        product = SimpleNamespace(id="product-1", name="Batata")
        order = SimpleNamespace(
            id="order-1",
            order_number=10,
            status=OrderStatus.DELIVERING,
            buyer_producer=buyer,
            buyer_producer_id=buyer.id,
            total_amount=Decimal("30.00"),
            source_type="MARKETPLACE",
        )
        item = SimpleNamespace(
            listing_id="listing-1",
            product=product,
            quantity=Decimal("30.000"),
            item_status=OrderItemStatus.IN_DELIVERY,
            seller_producer=seller,
            updated_at=None,
            save=MagicMock(),
        )
        order_manager = MagicMock()
        order_manager.select_for_update.return_value.get.return_value = order
        item_manager = MagicMock()
        item_manager.select_related.return_value.filter.return_value.exclude.return_value = [item]
        complete = getattr(confirm_order_receipt, "__wrapped__", confirm_order_receipt)

        def mark_completed(current_order, status):
            current_order.status = status

        with (
            patch("apps.orders.services.Order.objects", order_manager),
            patch("apps.orders.services.OrderItem.objects", item_manager),
            patch("apps.orders.services._consume_listing_reservation") as consume_reservation,
            patch("apps.orders.services._register_buyer_order_inbound") as register_inbound,
            patch("apps.orders.services._sync_external_demands_for_product_change"),
            patch("apps.orders.services._set_order_status", side_effect=mark_completed),
            patch("apps.orders.services._create_status_history") as status_history,
            patch("apps.orders.services._log_order_status_change"),
            patch("apps.orders.services.log_audit_event"),
            patch("apps.orders.services._notify_order_completed_to_seller"),
            patch("apps.orders.services._sync_need_response_statuses_for_listing_ids"),
            patch("apps.orders.services.recalculate_needs_for_order"),
            patch("apps.orders.services._sync_alerts_for_producers"),
        ):
            complete(order=order, acting_user=None)

        self.assertEqual(item.item_status, OrderItemStatus.COMPLETED)
        consume_reservation.assert_called_once_with(
            "listing-1",
            Decimal("30.000"),
            None,
            order=order,
        )
        register_inbound.assert_called_once_with(
            buyer_producer=buyer,
            order=order,
            product=product,
            quantity=Decimal("30.000"),
            acting_user=None,
        )
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        status_history.assert_called_once()

    def test_consuming_seller_stock_creates_order_out_movement(self):
        stock = SimpleNamespace(
            id="stock-1",
            product_id="product-1",
            current_quantity=Decimal("100.000"),
            reserved_quantity=Decimal("30.000"),
            save=MagicMock(),
        )
        order = SimpleNamespace(id="order-1", order_number=10)
        movement = SimpleNamespace(
            id="move-out",
            movement_type=StockMovementType.ORDER_OUT,
            quantity_delta=Decimal("-30.000"),
            notes="Saída.",
        )

        with (
            patch("apps.orders.services.StockMovement.objects.create", return_value=movement) as create_movement,
            patch("apps.orders.services.log_audit_event"),
            patch("apps.orders.services._reconcile_listings_against_stock_capacity") as reconcile,
        ):
            _consume_stock_reservation(stock, Decimal("30.000"), None, order=order)

        self.assertEqual(stock.current_quantity, Decimal("70.000"))
        self.assertEqual(stock.reserved_quantity, Decimal("0.000"))
        self.assertEqual(create_movement.call_args.kwargs["movement_type"], StockMovementType.ORDER_OUT)
        self.assertEqual(create_movement.call_args.kwargs["quantity_delta"], Decimal("-30.000"))
        reconcile.assert_called_once_with(stock, acting_user=None)

    def test_registering_buyer_receipt_creates_order_in_movement(self):
        buyer = SimpleNamespace(id="buyer-1")
        product = SimpleNamespace(id="product-1", name="Batata")
        stock = SimpleNamespace(
            id="buyer-stock-1",
            product_id=product.id,
            current_quantity=Decimal("5.000"),
            save=MagicMock(),
        )
        order = SimpleNamespace(id="order-1", order_number=10)
        movement = SimpleNamespace(
            id="move-in",
            movement_type=StockMovementType.ORDER_IN,
            quantity_delta=Decimal("30.000"),
            notes="Entrada.",
        )

        with (
            patch("apps.orders.services._ensure_buyer_product_link"),
            patch("apps.orders.services._ensure_buyer_stock", return_value=stock),
            patch("apps.orders.services.StockMovement.objects.create", return_value=movement) as create_movement,
            patch("apps.orders.services.log_audit_event"),
        ):
            _register_buyer_order_inbound(
                buyer_producer=buyer,
                order=order,
                product=product,
                quantity=Decimal("30.000"),
                acting_user=None,
            )

        self.assertEqual(stock.current_quantity, Decimal("35.000"))
        self.assertEqual(create_movement.call_args.kwargs["movement_type"], StockMovementType.ORDER_IN)
        self.assertEqual(create_movement.call_args.kwargs["quantity_delta"], Decimal("30.000"))
