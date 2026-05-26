from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import RequestFactory, SimpleTestCase
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountStatus, UserRole
from apps.marketplace.models import ListingStatus
from apps.marketplace.services import LISTING_SOURCE_STOCK
from apps.needs.forms import NeedCreateForm, NeedEditForm, NeedResponseEditForm
from apps.needs.models import ExternalCustomerDemandStatus, NeedResponseStatus, NeedSourceSystem, NeedStatus
from apps.needs.services import (
    calculate_external_demand_plan,
    calculate_need_coverage,
    create_need,
    DuplicateActiveNeedError,
    evaluate_external_demand_conflict_with_listings,
    get_critical_stock_product_ids,
    get_need_response_summaries_for_responder,
    get_active_need_response_for_responder,
    get_public_offered_quantities_by_need,
    ignore_need,
    list_need_responses_for_owner,
    list_need_responses_for_responder,
    list_marketplace_public_needs,
    mark_external_customer_demand_fulfilled,
    publish_need_to_marketplace,
    reject_need_response,
    normalize_needs_search_query,
    sync_need_from_external_demands,
    update_need,
    update_need_response,
    withdraw_need_from_marketplace,
)
from apps.needs.views import build_external_demands_context, build_needs_index_context
from apps.orders.models import OrderItemStatus, OrderStatus


class FakeQuerySet(list):
    def filter(self, *args, **kwargs):
        items = list(self)
        for key, value in kwargs.items():
            if key == "need_id__in":
                allowed = {str(item) for item in value}
                items = [item for item in items if str(getattr(item, "need_id", "")) in allowed]
            elif key == "status":
                items = [item for item in items if getattr(item, "status", None) == value]
            elif key == "need_response_status":
                items = [item for item in items if getattr(item, "need_response_status", None) == value]
            elif key == "quantity_available__gt":
                items = [item for item in items if getattr(item, "quantity_available", Decimal("0")) > value]
            elif key == "order_items__isnull":
                items = [item for item in items if bool(getattr(item, "has_order_items", False)) != value]
            elif key == "order__status__in":
                items = [
                    item for item in items
                    if getattr(getattr(item, "order", None), "status", None) in value
                ]
        return FakeQuerySet(items)

    def exclude(self, **kwargs):
        items = list(self)
        for key, value in kwargs.items():
            if key == "producer":
                items = [item for item in items if getattr(item, "producer_id", None) != value.id]
            elif key == "seller_producer":
                items = [item for item in items if getattr(item, "seller_producer_id", None) != value.id]
            elif key == "item_status__in":
                items = [item for item in items if getattr(item, "item_status", None) not in value]
            elif key == "expires_at__lte":
                items = [
                    item for item in items
                    if not getattr(item, "expires_at", None) or getattr(item, "expires_at") > value
                ]
        return FakeQuerySet(items)

    def only(self, *args):
        return self


class FakeServiceQuerySet(list):
    def select_related(self, *args):
        return self

    def only(self, *args):
        return self

    def filter(self, *args, **kwargs):
        items = list(self)
        for key, value in kwargs.items():
            if key == "producer":
                items = [item for item in items if getattr(item, "producer", None) == value]
            elif key == "product":
                items = [item for item in items if getattr(item, "product", None) == value]
            elif key == "status__in":
                items = [item for item in items if getattr(item, "status", None) in value]
        return FakeServiceQuerySet(items)

    def order_by(self, *fields):
        items = list(self)
        for field in reversed(fields):
            reverse = field.startswith("-")
            attr = field[1:] if reverse else field
            items.sort(key=lambda item: getattr(item, attr, None), reverse=reverse)
        return FakeServiceQuerySet(items)

    def first(self):
        return self[0] if self else None


class FakeServiceManager:
    def __init__(self, items):
        self.items = FakeServiceQuerySet(items)

    def select_related(self, *args):
        return self.items.select_related(*args)

    def filter(self, *args, **kwargs):
        return self.items.filter(*args, **kwargs)


class NeedsRoutingTests(SimpleTestCase):
    def test_needs_index_url_is_public_needs_path(self):
        self.assertEqual(reverse("needs:index"), "/necessidades/")

    def test_need_response_publish_url_is_public_needs_path(self):
        self.assertEqual(reverse("needs:respond"), "/necessidades/responder/")

    def test_need_response_urls_are_public_needs_paths(self):
        listing_id = uuid4()

        self.assertEqual(
            reverse("needs:response_detail", args=[listing_id]),
            f"/necessidades/respostas/{listing_id}/",
        )
        self.assertEqual(
            reverse("needs:response_edit", args=[listing_id]),
            f"/necessidades/respostas/{listing_id}/editar/",
        )
        self.assertEqual(
            reverse("needs:response_reject", args=[listing_id]),
            f"/necessidades/respostas/{listing_id}/rejeitar/",
        )

    def test_need_edit_url_is_public_needs_path(self):
        need_id = uuid4()

        self.assertEqual(
            reverse("needs:edit", args=[need_id]),
            f"/necessidades/{need_id}/editar/",
        )
        self.assertEqual(
            reverse("needs:publish_marketplace", args=[need_id]),
            f"/necessidades/{need_id}/publicar/",
        )
        self.assertEqual(
            reverse("needs:withdraw_marketplace", args=[need_id]),
            f"/necessidades/{need_id}/retirar-marketplace/",
        )

    def test_external_customer_demands_urls_are_public_needs_paths(self):
        demand_id = uuid4()

        self.assertEqual(reverse("needs:external_demands"), "/necessidades/pedidos-clientes/")
        self.assertEqual(reverse("needs:external_demand_create"), "/necessidades/pedidos-clientes/criar/")
        self.assertEqual(
            reverse("needs:external_demand_edit", args=[demand_id]),
            f"/necessidades/pedidos-clientes/{demand_id}/editar/",
        )
        self.assertEqual(
            reverse("needs:external_demand_cancel", args=[demand_id]),
            f"/necessidades/pedidos-clientes/{demand_id}/cancelar/",
        )
        self.assertEqual(
            reverse("needs:external_demand_fulfill", args=[demand_id]),
            f"/necessidades/pedidos-clientes/{demand_id}/cumprir/",
        )


class ExternalDemandPlanningTests(SimpleTestCase):
    def _calculate_plan(self, *, demands, stock, forecasts, producer=None, product=None):
        producer = producer or SimpleNamespace(id="producer-1")
        product = product or SimpleNamespace(id="product-1", name="Batata", unit="kg")

        for demand in demands:
            demand.producer = getattr(demand, "producer", producer)
            demand.product = getattr(demand, "product", product)
        if stock is not None:
            stock.producer = getattr(stock, "producer", producer)
            stock.product = getattr(stock, "product", product)
        for forecast in forecasts:
            forecast.producer = getattr(forecast, "producer", producer)
            forecast.product = getattr(forecast, "product", product)

        with (
            patch("apps.needs.services.ExternalCustomerDemand.objects", FakeServiceManager(demands)),
            patch("apps.needs.services.Stock.objects", FakeServiceManager([stock] if stock else [])),
            patch("apps.needs.services.ProductionForecast.objects", FakeServiceManager(forecasts)),
            patch("apps.needs.services._stock_active_listings_quantity", return_value=Decimal("0.000")),
            patch("apps.needs.services._forecast_active_listings_quantity", return_value=Decimal("0.000")),
            patch("apps.needs.services._get_customer_demand_need_for_product", return_value=None),
        ):
            return calculate_external_demand_plan(producer=producer, product=product)

    def test_external_demand_plan_accumulates_previous_demands_by_delivery_date(self):
        demands = [
            SimpleNamespace(
                requested_quantity=Decimal("125"),
                requested_delivery_date=date(2026, 6, 1),
                status=ExternalCustomerDemandStatus.OPEN,
                created_at=1,
            ),
            SimpleNamespace(
                requested_quantity=Decimal("200"),
                requested_delivery_date=date(2026, 6, 30),
                status=ExternalCustomerDemandStatus.OPEN,
                created_at=2,
            ),
            SimpleNamespace(
                requested_quantity=Decimal("300"),
                requested_delivery_date=date(2026, 7, 1),
                status=ExternalCustomerDemandStatus.COVERED,
                created_at=3,
            ),
            SimpleNamespace(
                requested_quantity=Decimal("300"),
                requested_delivery_date=date(2026, 9, 1),
                status=ExternalCustomerDemandStatus.PARTIALLY_COVERED,
                created_at=4,
            ),
            SimpleNamespace(
                requested_quantity=Decimal("999"),
                requested_delivery_date=date(2026, 6, 1),
                status=ExternalCustomerDemandStatus.CANCELLED,
                created_at=5,
            ),
            SimpleNamespace(
                requested_quantity=Decimal("999"),
                requested_delivery_date=date(2026, 6, 1),
                status=ExternalCustomerDemandStatus.FULFILLED,
                created_at=6,
            ),
        ]
        stock = SimpleNamespace(current_quantity=Decimal("500"), reserved_quantity=Decimal("0"))
        forecasts = [
            SimpleNamespace(
                forecast_quantity=Decimal("350"),
                reserved_quantity=Decimal("50"),
                period_start=date(2026, 8, 14),
                period_end=date(2026, 8, 31),
            ),
            SimpleNamespace(
                forecast_quantity=Decimal("999"),
                reserved_quantity=Decimal("0"),
                period_start=date(2026, 9, 15),
                period_end=date(2026, 10, 1),
            ),
            SimpleNamespace(
                forecast_quantity=Decimal("999"),
                reserved_quantity=Decimal("0"),
                period_start=None,
                period_end=None,
            ),
        ]

        plan = self._calculate_plan(demands=demands, stock=stock, forecasts=forecasts)

        rows = plan["rows"]
        self.assertEqual(plan["total_external_demand"], Decimal("925.000"))
        self.assertEqual(plan["available_stock_now"], Decimal("500.000"))
        self.assertEqual(plan["total_forecast_relevant"], Decimal("300.000"))
        self.assertEqual(plan["max_deficit"], Decimal("125.000"))
        self.assertEqual(plan["first_deficit_date"], date(2026, 7, 1))

        expected = [
            (date(2026, 6, 1), Decimal("125.000"), Decimal("0.000"), Decimal("500.000"), Decimal("375.000"), Decimal("0.000")),
            (date(2026, 6, 30), Decimal("325.000"), Decimal("0.000"), Decimal("500.000"), Decimal("175.000"), Decimal("0.000")),
            (date(2026, 7, 1), Decimal("625.000"), Decimal("0.000"), Decimal("500.000"), Decimal("0.000"), Decimal("125.000")),
            (date(2026, 9, 1), Decimal("925.000"), Decimal("300.000"), Decimal("800.000"), Decimal("0.000"), Decimal("125.000")),
        ]
        actual = [
            (
                row["delivery_date"],
                row["demand_until_date"],
                row["forecast_until_date"],
                row["capacity_until_date"],
                row["remaining_capacity_until_date"],
                row["deficit_until_date"],
            )
            for row in rows
        ]
        self.assertEqual(actual, expected)

    def test_external_demand_plan_uses_period_start_fallback_and_ignores_invalid_forecasts(self):
        demand = SimpleNamespace(
            requested_quantity=Decimal("100"),
            requested_delivery_date=date(2026, 6, 30),
            status=ExternalCustomerDemandStatus.OPEN,
            created_at=1,
        )
        stock = SimpleNamespace(current_quantity=Decimal("0"), reserved_quantity=Decimal("0"))
        forecasts = [
            SimpleNamespace(
                forecast_quantity=Decimal("80"),
                reserved_quantity=Decimal("20"),
                period_start=date(2026, 6, 30),
                period_end=None,
            ),
            SimpleNamespace(
                forecast_quantity=Decimal("100"),
                reserved_quantity=Decimal("0"),
                period_start=date(2026, 7, 1),
                period_end=None,
            ),
            SimpleNamespace(
                forecast_quantity=Decimal("100"),
                reserved_quantity=Decimal("0"),
                period_start=None,
                period_end=None,
            ),
        ]

        plan = self._calculate_plan(demands=[demand], stock=stock, forecasts=forecasts)
        row = plan["rows"][0]

        self.assertEqual(row["forecast_until_date"], Decimal("60.000"))
        self.assertEqual(row["capacity_until_date"], Decimal("60.000"))
        self.assertEqual(row["remaining_capacity_until_date"], Decimal("0.000"))
        self.assertEqual(row["deficit_until_date"], Decimal("40.000"))


class ExternalDemandLifecycleTests(SimpleTestCase):
    def test_sync_lock_only_selects_customer_demand_need(self):
        from apps.needs.services import _lock_need_for_customer_demand_sync

        queryset = MagicMock()
        manager = MagicMock()
        manager.select_for_update.return_value = queryset
        queryset.filter.return_value = queryset
        queryset.exclude.return_value = queryset
        queryset.order_by.return_value = queryset
        queryset.first.return_value = None

        with patch("apps.needs.services.Need.objects", manager):
            result = _lock_need_for_customer_demand_sync(
                producer=SimpleNamespace(id="producer-1"),
                product=SimpleNamespace(id="product-1"),
            )

        self.assertIsNone(result)
        filter_kwargs = queryset.filter.call_args.kwargs
        self.assertEqual(filter_kwargs["producer"].id, "producer-1")
        self.assertEqual(filter_kwargs["product"].id, "product-1")
        self.assertEqual(filter_kwargs["source_system"], NeedSourceSystem.CUSTOMER_DEMAND)

    def _fulfillable_demand(self, *, status=ExternalCustomerDemandStatus.OPEN):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1", name="Batata", unit="kg")
        demand = SimpleNamespace(id="demand-1")
        locked = SimpleNamespace(
            id="demand-1",
            producer_id="producer-1",
            product_id="product-1",
            product=product,
            requested_quantity=Decimal("20"),
            requested_delivery_date=date(2026, 6, 1),
            status=status,
            source_system="MANUAL",
            client_name="Cliente",
            generated_need_id=None,
            fulfilled_at=None,
            updated_by=None,
            updated_at=None,
            save=MagicMock(),
        )
        return producer, product, demand, locked

    def test_mark_external_demand_fulfilled_reduces_stock_creates_movement_and_recalculates(self):
        producer, product, demand, locked = self._fulfillable_demand()
        stock = SimpleNamespace(
            id="stock-1",
            current_quantity=Decimal("50.000"),
            reserved_quantity=Decimal("10.000"),
            updated_by=None,
            last_updated_at=None,
            updated_at=None,
            save=MagicMock(),
        )
        movement = SimpleNamespace(id="movement-1", notes="Saída externa")
        manager = MagicMock()
        manager.select_for_update.return_value.select_related.return_value.get.return_value = locked
        stock_manager = MagicMock()
        stock_manager.select_for_update.return_value.filter.return_value.first.return_value = stock
        movement_manager = MagicMock()
        movement_manager.filter.return_value.first.return_value = None
        movement_manager.create.return_value = movement
        fulfill = getattr(mark_external_customer_demand_fulfilled, "__wrapped__", mark_external_customer_demand_fulfilled)

        with (
            patch("apps.needs.services.ExternalCustomerDemand.objects", manager),
            patch("apps.needs.services.Stock.objects", stock_manager),
            patch("apps.needs.services.StockMovement.objects", movement_manager),
            patch("apps.needs.services.log_audit_event"),
            patch("apps.needs.services.sync_external_customer_demand_state_for_product") as sync,
            patch("apps.inventory.services.get_listings_blocking_stock_decrease", return_value={"deficit": Decimal("0.000")}),
        ):
            result, changed = fulfill(demand=demand, producer=producer, updated_by=None)

        self.assertTrue(changed)
        self.assertIs(result, locked)
        self.assertEqual(stock.current_quantity, Decimal("30.000"))
        movement_manager.create.assert_called_once()
        create_values = movement_manager.create.call_args.kwargs
        self.assertEqual(create_values["movement_type"], "ORDER_OUT")
        self.assertEqual(create_values["reference_type"], "EXTERNAL_DEMAND")
        self.assertEqual(create_values["reference_id"], "demand-1")
        self.assertEqual(locked.status, ExternalCustomerDemandStatus.FULFILLED)
        self.assertIsNotNone(locked.fulfilled_at)
        sync.assert_called_once_with(producer=producer, product=product, acting_user=None)

    def test_mark_external_demand_fulfilled_fails_without_available_stock(self):
        producer, _, demand, locked = self._fulfillable_demand()
        stock = SimpleNamespace(
            current_quantity=Decimal("20.000"),
            reserved_quantity=Decimal("5.000"),
        )
        manager = MagicMock()
        manager.select_for_update.return_value.select_related.return_value.get.return_value = locked
        stock_manager = MagicMock()
        stock_manager.select_for_update.return_value.filter.return_value.first.return_value = stock
        movement_manager = MagicMock()
        movement_manager.filter.return_value.first.return_value = None
        fulfill = getattr(mark_external_customer_demand_fulfilled, "__wrapped__", mark_external_customer_demand_fulfilled)

        with (
            patch("apps.needs.services.ExternalCustomerDemand.objects", manager),
            patch("apps.needs.services.Stock.objects", stock_manager),
            patch("apps.needs.services.StockMovement.objects", movement_manager),
            patch("apps.needs.services.sync_external_customer_demand_state_for_product") as sync,
        ):
            with self.assertRaisesMessage(ValidationError, "Não existe stock atual suficiente"):
                fulfill(demand=demand, producer=producer, updated_by=None)

        self.assertEqual(locked.status, ExternalCustomerDemandStatus.OPEN)
        locked.save.assert_not_called()
        movement_manager.create.assert_not_called()
        sync.assert_not_called()

    def test_mark_external_demand_fulfilled_is_noop_when_already_fulfilled(self):
        producer, _, demand, locked = self._fulfillable_demand(status=ExternalCustomerDemandStatus.FULFILLED)
        manager = MagicMock()
        manager.select_for_update.return_value.select_related.return_value.get.return_value = locked
        movement_manager = MagicMock()
        fulfill = getattr(mark_external_customer_demand_fulfilled, "__wrapped__", mark_external_customer_demand_fulfilled)

        with (
            patch("apps.needs.services.ExternalCustomerDemand.objects", manager),
            patch("apps.needs.services.StockMovement.objects", movement_manager),
        ):
            result, changed = fulfill(demand=demand, producer=producer, updated_by=None)

        self.assertIs(result, locked)
        self.assertFalse(changed)
        movement_manager.filter.assert_not_called()
        movement_manager.create.assert_not_called()
        locked.save.assert_not_called()

    def test_mark_external_demand_fulfilled_does_not_duplicate_existing_external_movement(self):
        producer, product, demand, locked = self._fulfillable_demand()
        manager = MagicMock()
        manager.select_for_update.return_value.select_related.return_value.get.return_value = locked
        movement_manager = MagicMock()
        movement_manager.filter.return_value.first.return_value = SimpleNamespace(id="existing-movement")
        stock_manager = MagicMock()
        fulfill = getattr(mark_external_customer_demand_fulfilled, "__wrapped__", mark_external_customer_demand_fulfilled)

        with (
            patch("apps.needs.services.ExternalCustomerDemand.objects", manager),
            patch("apps.needs.services.Stock.objects", stock_manager),
            patch("apps.needs.services.StockMovement.objects", movement_manager),
            patch("apps.needs.services.log_audit_event"),
            patch("apps.needs.services.sync_external_customer_demand_state_for_product") as sync,
        ):
            result, changed = fulfill(demand=demand, producer=producer, updated_by=None)

        self.assertTrue(changed)
        self.assertIs(result, locked)
        stock_manager.select_for_update.assert_not_called()
        movement_manager.create.assert_not_called()
        self.assertEqual(locked.status, ExternalCustomerDemandStatus.FULFILLED)
        sync.assert_called_once_with(producer=producer, product=product, acting_user=None)

    def test_external_demands_context_separates_near_deadline_from_coverage_and_history(self):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1", name="Batata", unit="kg")
        active = SimpleNamespace(
            id="demand-active",
            product_id="product-1",
            product=product,
            requested_delivery_date=timezone.now().date() + timedelta(days=3),
            status=ExternalCustomerDemandStatus.OPEN,
        )
        fulfilled = SimpleNamespace(
            id="demand-fulfilled",
            product_id="product-1",
            product=product,
            requested_delivery_date=timezone.now().date() - timedelta(days=2),
            status=ExternalCustomerDemandStatus.FULFILLED,
        )
        plan = {
            "product": product,
            "rows": [
                {
                    "delivery_date": active.requested_delivery_date,
                    "deficit_until_date": Decimal("0.000"),
                    "remaining_capacity_until_date": Decimal("0.000"),
                }
            ],
        }

        with (
            patch("apps.needs.views.list_external_customer_demands", return_value=[active, fulfilled]),
            patch("apps.needs.views.get_need_candidate_products", return_value=[product]),
            patch("apps.needs.views.build_external_demand_plans", return_value=[plan]),
            patch("apps.needs.views.get_external_customer_demand_summary", return_value={}),
        ):
            context = build_external_demands_context(
                producer,
                create_form=MagicMock(),
            )

        self.assertEqual(context["active_demand_rows"], [active])
        self.assertEqual(context["past_demand_rows"], [fulfilled])
        self.assertEqual(active.urgency, "soon")
        self.assertEqual(active.coverage_key, "no_margin")
        self.assertEqual(active.stock_diff, Decimal("0.000"))

    def test_recalculation_withdraws_a_published_automatic_need_when_deficit_changes(self):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1", name="Batata")
        existing_need = SimpleNamespace(
            id="need-1",
            producer_id="producer-1",
            product_id="product-1",
            producer=producer,
            product=product,
            required_quantity=Decimal("25.000"),
            needed_by_date=timezone.now(),
            source_system=NeedSourceSystem.CUSTOMER_DEMAND,
            external_id="customer_demands:producer-1:product-1",
            notes="",
            status=NeedStatus.OPEN,
            is_marketplace_published=True,
            published_at=timezone.now(),
            updated_at=None,
            save=MagicMock(),
        )
        plan = {"max_deficit": Decimal("40.000"), "first_deficit_date": date(2026, 6, 30)}
        sync = getattr(sync_need_from_external_demands, "__wrapped__", sync_need_from_external_demands)

        with (
            patch("apps.needs.services.calculate_external_demand_plan", return_value=plan),
            patch("apps.needs.services._lock_need_for_customer_demand_sync", return_value=existing_need),
            patch("apps.needs.services.recalculate_need_status", return_value=(existing_need, {}, False)),
            patch("apps.needs.services._set_external_demands_generated_need"),
            patch("apps.needs.services.log_audit_event") as audit,
        ):
            need, _, changed = sync(producer=producer, product=product)

        self.assertIs(need, existing_need)
        self.assertTrue(changed)
        self.assertFalse(existing_need.is_marketplace_published)
        self.assertIn(
            "NEED_MARKETPLACE_UNPUBLISHED_AFTER_RECALCULATION",
            [call.kwargs["action"] for call in audit.call_args_list],
        )


class NeedResponsePublishViewTests(SimpleTestCase):
    def _request(self):
        request = RequestFactory().post(
            "/necessidades/responder/?from=need&need=need-1&product=product-1",
            data={
                "need_id": "need-1",
                "listing_source": LISTING_SOURCE_STOCK,
                "quantity": "5",
                "unit_price": "2.50",
                "delivery_mode": "PICKUP",
                "notes": "Posso entregar esta quantidade.",
            },
        )
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
        )
        request.session = {}
        return request

    def test_need_response_publish_creates_private_listing_and_redirects_to_need(self):
        producer = SimpleNamespace(id="seller-1")
        need = SimpleNamespace(
            id="need-1",
            product_id="product-1",
            producer_id="buyer-1",
            product=SimpleNamespace(id="product-1", name="Tomate", unit="kg"),
            producer=SimpleNamespace(id="buyer-1"),
        )
        need_model = MagicMock()
        need_model.objects.select_related.return_value.filter.return_value.first.return_value = need
        form = MagicMock()
        form.is_valid.return_value = True
        form.cleaned_data = {
            "listing_source": LISTING_SOURCE_STOCK,
            "forecast": None,
            "quantity": Decimal("5"),
            "unit_price": Decimal("2.50"),
            "delivery_mode": "PICKUP",
            "delivery_radius_km": None,
            "delivery_fee": None,
            "show_location_on_map": True,
            "notes": "Posso entregar esta quantidade.",
        }

        request = self._request()
        with (
            patch("apps.needs.views.Need", need_model),
            patch("apps.needs.views.NeedResponsePublishForm", return_value=form),
            patch("apps.needs.views.get_current_producer_for_user", return_value=producer),
            patch("apps.needs.views.calculate_need_coverage", return_value={"remaining_to_receive": Decimal("10")}),
            patch("apps.needs.views.get_active_need_response_for_responder", return_value=None),
            patch("apps.needs.views.get_need_response_summaries_for_responder", return_value={}),
            patch("apps.needs.views.create_listing", return_value=SimpleNamespace(id="listing-1")) as create_listing,
            patch("apps.needs.views.sync_alerts_after_need_change") as sync_alerts,
            patch("apps.needs.views.messages"),
        ):
            from apps.needs.views import need_response_publish_view

            response = need_response_publish_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/necessidades/?need=need-1")
        self.assertEqual(create_listing.call_args.kwargs["need"], need)
        self.assertIsNone(create_listing.call_args.kwargs["photo_path"])
        self.assertEqual(create_listing.call_args.kwargs["status"], ListingStatus.ACTIVE)
        self.assertEqual(
            sync_alerts.call_args_list,
            [
                ((producer, request.current_user), {}),
                ((need.producer, request.current_user), {}),
            ],
        )

    def test_need_response_publish_updates_existing_active_response_instead_of_creating_duplicate(self):
        producer = SimpleNamespace(id="seller-1")
        need = SimpleNamespace(
            id="need-1",
            product_id="product-1",
            producer_id="buyer-1",
            product=SimpleNamespace(id="product-1", name="Tomate", unit="kg"),
            producer=SimpleNamespace(id="buyer-1"),
        )
        listing_id = uuid4()
        existing_response = SimpleNamespace(id=listing_id, need=need)
        need_model = MagicMock()
        need_model.objects.select_related.return_value.filter.return_value.first.return_value = need
        form = MagicMock()
        form.is_valid.return_value = True
        form.cleaned_data = {
            "quantity": Decimal("7"),
            "unit_price": Decimal("2.70"),
            "delivery_mode": "PICKUP",
            "delivery_radius_km": None,
            "delivery_fee": None,
            "notes": "Proposta ajustada.",
        }

        with (
            patch("apps.needs.views.Need", need_model),
            patch("apps.needs.views.NeedResponsePublishForm", return_value=MagicMock()),
            patch("apps.needs.views.NeedResponseEditForm", return_value=form),
            patch("apps.needs.views.get_current_producer_for_user", return_value=producer),
            patch("apps.needs.views.calculate_need_coverage", return_value={"remaining_to_receive": Decimal("10")}),
            patch("apps.needs.views.get_active_need_response_for_responder", return_value=existing_response),
            patch("apps.needs.views.get_need_response_summaries_for_responder", return_value={}),
            patch("apps.needs.views.create_listing") as create_listing,
            patch("apps.needs.views.update_need_response", return_value=existing_response) as update_response,
            patch("apps.needs.views.sync_alerts_after_need_change"),
            patch("apps.needs.views.messages"),
        ):
            from apps.needs.views import need_response_publish_view

            response = need_response_publish_view(self._request())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/necessidades/respostas/{listing_id}/")
        create_listing.assert_not_called()
        update_response.assert_called_once()

    def test_needs_index_get_does_not_expire_listings(self):
        request = RequestFactory().get("/necessidades/")
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
        )
        producer = SimpleNamespace(id="producer-1")

        with (
            patch("apps.needs.views.get_current_producer_for_user", return_value=producer),
            patch("apps.needs.views.build_needs_index_context", return_value={"page_title": "Necessidades"}),
            patch("apps.needs.views.render", return_value=HttpResponse("ok")),
            patch("apps.marketplace.services.expire_due_active_listings") as expire,
        ):
            from apps.needs.views import needs_index_view

            response = needs_index_view(request)

        self.assertEqual(response.status_code, 200)
        expire.assert_not_called()

    def test_need_response_publish_blocks_new_proposal_when_latest_response_cannot_be_replaced(self):
        producer = SimpleNamespace(id="seller-1")
        listing_id = uuid4()
        need = SimpleNamespace(
            id="need-1",
            product_id="product-1",
            producer_id="buyer-1",
            product=SimpleNamespace(id="product-1", name="Tomate", unit="kg"),
            producer=SimpleNamespace(id="buyer-1"),
        )
        need_model = MagicMock()
        need_model.objects.select_related.return_value.filter.return_value.first.return_value = need
        latest_summary = SimpleNamespace(
            listing_id=listing_id,
            can_send_new_proposal=False,
        )

        with (
            patch("apps.needs.views.Need", need_model),
            patch("apps.needs.views.get_current_producer_for_user", return_value=producer),
            patch("apps.needs.views.calculate_need_coverage", return_value={"remaining_to_receive": Decimal("10")}),
            patch("apps.needs.views.get_active_need_response_for_responder", return_value=None),
            patch("apps.needs.views.get_need_response_summaries_for_responder", return_value={"need-1": latest_summary}),
            patch("apps.needs.views.create_listing") as create_listing,
            patch("apps.needs.views.messages"),
        ):
            from apps.needs.views import need_response_publish_view

            response = need_response_publish_view(self._request())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/necessidades/respostas/{listing_id}/")
        create_listing.assert_not_called()


class NeedEditTests(SimpleTestCase):
    def test_need_create_form_limits_notes_length(self):
        form = NeedCreateForm(
            data={
                "product_id": "product-1",
                "required_quantity": "10",
                "needed_by_date": "",
                "notes": "x" * 1201,
            },
            producer=None,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("notes", form.errors)

    def test_need_response_edit_form_limits_notes_length(self):
        listing = SimpleNamespace(
            quantity_total=Decimal("12.000"),
            unit_price=Decimal("2.40"),
            delivery_mode="PICKUP",
            delivery_radius_km=None,
            delivery_fee=None,
            notes="",
        )
        form = NeedResponseEditForm(
            data={
                "quantity": "12",
                "unit_price": "2.40",
                "delivery_mode": "PICKUP",
                "delivery_radius_km": "",
                "delivery_fee": "",
                "notes": "x" * 1201,
            },
            listing=listing,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("notes", form.errors)

    def test_search_query_is_normalized_to_safe_length(self):
        self.assertEqual(len(normalize_needs_search_query("  " + "x" * 200)), 120)

    def test_need_edit_form_blocks_quantity_below_planned_quantity(self):
        need = SimpleNamespace(
            status=NeedStatus.PARTIALLY_COVERED,
            required_quantity=Decimal("100"),
            needed_by_date=None,
            notes="",
            product=SimpleNamespace(unit="kg"),
        )
        coverage = {
            "planned_qty": Decimal("80.000"),
            "completed_qty": Decimal("20.000"),
        }

        with patch("apps.needs.forms.calculate_need_coverage", return_value=coverage):
            form = NeedEditForm(
                data={
                    "required_quantity": "70",
                    "needed_by_date": "",
                    "notes": "Atualização",
                },
                need=need,
            )

        self.assertFalse(form.is_valid())
        self.assertIn("required_quantity", form.errors)

    def test_need_response_edit_form_uses_listing_values(self):
        listing = SimpleNamespace(
            quantity_total=Decimal("12.000"),
            unit_price=Decimal("2.40"),
            delivery_mode="PICKUP",
            delivery_radius_km=None,
            delivery_fee=None,
            notes="Entrega amanhã.",
        )

        form = NeedResponseEditForm(listing=listing)

        self.assertEqual(form.initial["quantity"], Decimal("12.000"))
        self.assertEqual(form.initial["unit_price"], Decimal("2.40"))
        self.assertNotIn("listing_source", form.fields)

    def test_update_need_allows_increasing_covered_need_and_recalculates_status(self):
        producer = SimpleNamespace(id="producer-1")
        locked_need = SimpleNamespace(
            id="need-1",
            producer_id="producer-1",
            status=NeedStatus.COVERED,
            product=SimpleNamespace(unit="kg"),
            required_quantity=Decimal("100.000"),
            needed_by_date=None,
            notes=None,
            updated_at=None,
            save=MagicMock(),
        )
        coverage_before = {
            "planned_qty": Decimal("100.000"),
            "completed_qty": Decimal("100.000"),
        }
        coverage_after = {
            "planned_qty": Decimal("100.000"),
            "completed_qty": Decimal("100.000"),
            "required_quantity": Decimal("150.000"),
        }
        update = getattr(update_need, "__wrapped__", update_need)

        with (
            patch("apps.needs.services.Need") as need_model,
            patch("apps.needs.services.calculate_need_coverage", return_value=coverage_before),
            patch("apps.needs.services.recalculate_need_status", return_value=(locked_need, coverage_after, True)) as recalc,
        ):
            need_model.objects.select_for_update.return_value.select_related.return_value.get.return_value = locked_need
            updated_need, coverage, changed = update(
                need=locked_need,
                producer=producer,
                required_quantity=Decimal("150"),
                needed_by_date=None,
                notes="Preciso de reforço.",
            )

        self.assertIs(updated_need, locked_need)
        self.assertEqual(coverage, coverage_after)
        self.assertTrue(changed)
        self.assertEqual(locked_need.required_quantity, Decimal("150.000"))
        self.assertEqual(locked_need.notes, "Preciso de reforço.")
        locked_need.save.assert_called_once()
        recalc.assert_called_once_with(locked_need)

    def test_update_need_blocks_quantity_below_planned_quantity(self):
        producer = SimpleNamespace(id="producer-1")
        locked_need = SimpleNamespace(
            id="need-1",
            producer_id="producer-1",
            status=NeedStatus.PARTIALLY_COVERED,
            product=SimpleNamespace(unit="kg"),
            required_quantity=Decimal("100.000"),
            needed_by_date=None,
            notes=None,
            updated_at=None,
            save=MagicMock(),
        )
        coverage_before = {
            "planned_qty": Decimal("80.000"),
            "completed_qty": Decimal("20.000"),
        }
        update = getattr(update_need, "__wrapped__", update_need)

        with (
            patch("apps.needs.services.Need") as need_model,
            patch("apps.needs.services.calculate_need_coverage", return_value=coverage_before),
        ):
            need_model.objects.select_for_update.return_value.select_related.return_value.get.return_value = locked_need
            with self.assertRaisesMessage(Exception, "quantidade mínima permitida"):
                update(
                    need=locked_need,
                    producer=producer,
                    required_quantity=Decimal("70"),
                    needed_by_date=None,
                    notes=None,
                )

        locked_need.save.assert_not_called()

    def test_update_need_blocks_other_producer(self):
        producer = SimpleNamespace(id="producer-2")
        locked_need = SimpleNamespace(
            id="need-1",
            producer_id="producer-1",
            status=NeedStatus.OPEN,
            product=SimpleNamespace(unit="kg"),
            required_quantity=Decimal("100.000"),
            needed_by_date=None,
            notes=None,
            updated_at=None,
            save=MagicMock(),
        )
        update = getattr(update_need, "__wrapped__", update_need)

        with patch("apps.needs.services.Need") as need_model:
            need_model.objects.select_for_update.return_value.select_related.return_value.get.return_value = locked_need
            with self.assertRaisesMessage(Exception, "Não pode editar esta necessidade"):
                update(
                    need=locked_need,
                    producer=producer,
                    required_quantity=Decimal("120"),
                    needed_by_date=None,
                    notes=None,
                )

        locked_need.save.assert_not_called()

    def test_update_need_blocks_ignored_need(self):
        producer = SimpleNamespace(id="producer-1")
        locked_need = SimpleNamespace(
            id="need-1",
            producer_id="producer-1",
            status=NeedStatus.IGNORED,
            product=SimpleNamespace(unit="kg"),
            required_quantity=Decimal("100.000"),
            needed_by_date=None,
            notes=None,
            updated_at=None,
            save=MagicMock(),
        )
        update = getattr(update_need, "__wrapped__", update_need)

        with patch("apps.needs.services.Need") as need_model:
            need_model.objects.select_for_update.return_value.select_related.return_value.get.return_value = locked_need
            with self.assertRaisesMessage(Exception, "já não pode ser editada"):
                update(
                    need=locked_need,
                    producer=producer,
                    required_quantity=Decimal("120"),
                    needed_by_date=None,
                    notes=None,
                )

        locked_need.save.assert_not_called()


class NeedsServiceTests(SimpleTestCase):
    def test_calculate_need_coverage_counts_planned_and_completed_items(self):
        need = SimpleNamespace(id="need-1", required_quantity=Decimal("10"))
        items = [
            SimpleNamespace(
                item_status=OrderItemStatus.COMPLETED,
                quantity=Decimal("2"),
                order=SimpleNamespace(status=OrderStatus.COMPLETED),
            ),
            SimpleNamespace(
                item_status=OrderItemStatus.CONFIRMED,
                quantity=Decimal("3"),
                order=SimpleNamespace(status=OrderStatus.CONFIRMED),
            ),
            SimpleNamespace(
                item_status=OrderItemStatus.CANCELLED,
                quantity=Decimal("4"),
                order=SimpleNamespace(status=OrderStatus.CANCELLED),
            ),
        ]

        with patch(
            "apps.needs.services.OrderItem.objects.filter"
        ) as filter_items:
            filter_items.return_value.select_related.return_value = items
            coverage = calculate_need_coverage(need)

        self.assertEqual(coverage["required_quantity"], Decimal("10.000"))
        self.assertEqual(coverage["planned_qty"], Decimal("5.000"))
        self.assertEqual(coverage["completed_qty"], Decimal("2.000"))
        self.assertEqual(coverage["remaining_to_plan"], Decimal("5.000"))

    def test_create_need_blocks_existing_active_need_without_updating(self):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1")
        need = MagicMock(
            id="need-1",
            producer=producer,
            product=product,
            status=NeedStatus.OPEN,
        )
        need.updated_at = None
        active_qs = MagicMock()
        active_qs.order_by.return_value.first.return_value = need
        manager = MagicMock()
        manager.objects.select_for_update.return_value.filter.return_value = active_qs
        create = getattr(create_need, "__wrapped__", create_need)

        with patch("apps.needs.services.Need", manager):
            with self.assertRaises(DuplicateActiveNeedError) as caught:
                create(
                    producer=producer,
                    product=product,
                    required_quantity=Decimal("7"),
                    source_system=NeedSourceSystem.MANUAL,
                    notes="Observação",
                )

        self.assertIs(caught.exception.existing_need, need)
        manager.objects.create.assert_not_called()

    def test_create_need_allows_new_need_when_existing_need_is_covered(self):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1")
        need = MagicMock(
            id="need-new",
            product_id="product-1",
            producer=producer,
            product=product,
            required_quantity=Decimal("7.000"),
            needed_by_date=None,
            source_system=NeedSourceSystem.MANUAL,
            status=NeedStatus.OPEN,
            is_marketplace_published=True,
            published_at=timezone.now(),
        )
        active_qs = MagicMock()
        active_qs.order_by.return_value.first.return_value = None
        manager = MagicMock()
        manager.objects.select_for_update.return_value.filter.return_value = active_qs
        manager.objects.create.return_value = need
        create = getattr(create_need, "__wrapped__", create_need)

        with (
            patch("apps.needs.services.Need", manager),
            patch(
                "apps.needs.services.recalculate_need_status",
                return_value=(need, {"remaining_to_plan": Decimal("4.000")}, False),
            ),
            patch("apps.needs.services.log_audit_event"),
        ):
            result, _ = create(
                producer=producer,
                product=product,
                required_quantity=Decimal("7"),
                source_system=NeedSourceSystem.MANUAL,
                notes="Observação",
            )

        self.assertIs(result, need)
        manager.objects.create.assert_called_once()
        self.assertEqual(manager.objects.create.call_args.kwargs["required_quantity"], Decimal("7.000"))
        self.assertTrue(manager.objects.create.call_args.kwargs["is_marketplace_published"])
        self.assertIsNotNone(manager.objects.create.call_args.kwargs["published_at"])

    def test_ignore_need_marks_need_as_ignored(self):
        producer = SimpleNamespace(id="producer-1")
        need = MagicMock(producer_id="producer-1", status=NeedStatus.OPEN)
        ignore = getattr(ignore_need, "__wrapped__", ignore_need)

        changed = ignore(need=need, producer=producer)

        self.assertTrue(changed)
        self.assertEqual(need.status, NeedStatus.IGNORED)
        need.save.assert_called()

    def test_public_needs_hide_partially_covered_status(self):
        need = SimpleNamespace(
            id="need-1",
            status=NeedStatus.PARTIALLY_COVERED,
            producer=SimpleNamespace(display_name="Produtor A"),
            required_quantity=Decimal("10"),
            get_status_display=lambda: "Parcialmente Coberta",
        )
        qs = MagicMock()
        qs.exclude.return_value = qs
        qs.filter.return_value = qs
        qs.order_by.return_value = qs
        manager = MagicMock()
        manager.objects.select_related.return_value.filter.return_value = qs
        qs.__iter__.return_value = iter([need])

        with (
            patch("apps.needs.services.Need", manager),
            patch(
                "apps.needs.services.calculate_need_coverage",
                return_value={
                    "required_quantity": Decimal("10.000"),
                    "planned_qty": Decimal("4.000"),
                    "completed_qty": Decimal("0.000"),
                    "remaining_to_plan": Decimal("6.000"),
                    "remaining_to_receive": Decimal("10.000"),
                },
            ),
            patch("apps.needs.services.get_need_response_summaries_for_responder", return_value={}),
            patch("apps.needs.services.get_public_offered_quantities_by_need", return_value={"need-1": Decimal("5.000")}),
        ):
            rows = list_marketplace_public_needs(viewer_producer=SimpleNamespace(id="viewer-1"))

        self.assertEqual(rows[0]["public_status"], NeedStatus.OPEN)
        self.assertEqual(rows[0]["public_status_label"], "Aberta")
        self.assertEqual(rows[0]["public_quantity"], Decimal("6.000"))
        self.assertEqual(rows[0]["public_offered_quantity"], Decimal("5.000"))
        filter_kwargs = manager.objects.select_related.return_value.filter.call_args.kwargs
        self.assertTrue(filter_kwargs["is_marketplace_published"])

    def test_publish_and_withdraw_customer_need_preserves_publication_timestamp(self):
        producer = SimpleNamespace(id="producer-1")
        need = SimpleNamespace(
            id="need-1",
            producer_id="producer-1",
            product_id="product-1",
            producer=producer,
            product=SimpleNamespace(id="product-1", name="Batata"),
            required_quantity=Decimal("10.000"),
            needed_by_date=None,
            source_system=NeedSourceSystem.CUSTOMER_DEMAND,
            status=NeedStatus.OPEN,
            is_marketplace_published=False,
            published_at=None,
            updated_at=None,
            save=MagicMock(),
        )
        manager = MagicMock()
        manager.select_for_update.return_value.select_related.return_value.get.return_value = need
        publish = getattr(publish_need_to_marketplace, "__wrapped__", publish_need_to_marketplace)
        withdraw = getattr(withdraw_need_from_marketplace, "__wrapped__", withdraw_need_from_marketplace)

        with (
            patch("apps.needs.services.Need.objects", manager),
            patch(
                "apps.needs.services.calculate_need_coverage",
                return_value={"remaining_to_plan": Decimal("10.000")},
            ),
            patch(
                "apps.needs.services.calculate_external_demand_plan",
                return_value={"max_deficit": Decimal("10.000")},
            ),
            patch("apps.needs.services.log_audit_event") as audit,
        ):
            _, published = publish(need=need, producer=producer)
            publication_timestamp = need.published_at
            _, withdrawn = withdraw(need=need, producer=producer)

        self.assertTrue(published)
        self.assertTrue(withdrawn)
        self.assertFalse(need.is_marketplace_published)
        self.assertIs(need.published_at, publication_timestamp)
        self.assertEqual(
            [call.kwargs["action"] for call in audit.call_args_list],
            ["NEED_MARKETPLACE_PUBLISHED", "NEED_MARKETPLACE_WITHDRAWN"],
        )

    def test_public_offered_quantity_counts_only_other_relevant_offers(self):
        viewer = SimpleNamespace(id="viewer-1")
        pending_listings = FakeQuerySet([
            SimpleNamespace(
                need_id="need-1",
                producer_id="producer-b",
                status=ListingStatus.ACTIVE,
                need_response_status=NeedResponseStatus.PENDING,
                quantity_available=Decimal("50.000"),
                has_order_items=False,
            ),
            SimpleNamespace(
                need_id="need-1",
                producer_id="viewer-1",
                status=ListingStatus.ACTIVE,
                need_response_status=NeedResponseStatus.PENDING,
                quantity_available=Decimal("20.000"),
                has_order_items=False,
            ),
            SimpleNamespace(
                need_id="need-1",
                producer_id="producer-b",
                status=ListingStatus.CANCELLED,
                need_response_status=NeedResponseStatus.PENDING,
                quantity_available=Decimal("10.000"),
                has_order_items=False,
            ),
            SimpleNamespace(
                need_id="need-1",
                producer_id="producer-b",
                status=ListingStatus.ACTIVE,
                need_response_status=NeedResponseStatus.REJECTED,
                quantity_available=Decimal("10.000"),
                has_order_items=False,
            ),
            SimpleNamespace(
                need_id="need-1",
                producer_id="producer-b",
                status=ListingStatus.ACTIVE,
                need_response_status=NeedResponseStatus.PENDING,
                quantity_available=Decimal("10.000"),
                has_order_items=True,
            ),
            SimpleNamespace(
                need_id="need-1",
                producer_id="producer-b",
                status=ListingStatus.ACTIVE,
                need_response_status=NeedResponseStatus.PENDING,
                quantity_available=Decimal("70.000"),
                has_order_items=False,
                expires_at=timezone.now() - timedelta(hours=1),
            ),
        ])
        order_items = FakeQuerySet([
            SimpleNamespace(
                need_id="need-1",
                seller_producer_id="producer-b",
                item_status=OrderItemStatus.PENDING,
                order=SimpleNamespace(status=OrderStatus.PENDING),
                quantity=Decimal("30.000"),
            ),
            SimpleNamespace(
                need_id="need-1",
                seller_producer_id="viewer-1",
                item_status=OrderItemStatus.PENDING,
                order=SimpleNamespace(status=OrderStatus.PENDING),
                quantity=Decimal("5.000"),
            ),
            SimpleNamespace(
                need_id="need-1",
                seller_producer_id="producer-b",
                item_status=OrderItemStatus.CANCELLED,
                order=SimpleNamespace(status=OrderStatus.CANCELLED),
                quantity=Decimal("15.000"),
            ),
            SimpleNamespace(
                need_id="need-1",
                seller_producer_id="producer-b",
                item_status=OrderItemStatus.COMPLETED,
                order=SimpleNamespace(status=OrderStatus.COMPLETED),
                quantity=Decimal("20.000"),
            ),
            SimpleNamespace(
                need_id="need-1",
                seller_producer_id="producer-b",
                item_status=OrderItemStatus.PENDING,
                order=SimpleNamespace(status=OrderStatus.CANCELLED),
                quantity=Decimal("40.000"),
            ),
        ])

        with (
            patch(
                "apps.needs.services.MarketplaceListing.objects.filter",
                side_effect=lambda **kwargs: pending_listings.filter(**kwargs),
            ),
            patch(
                "apps.needs.services.OrderItem.objects.filter",
                side_effect=lambda **kwargs: order_items.filter(**kwargs),
            ),
        ):
            quantities = get_public_offered_quantities_by_need(
                need_ids=["need-1"],
                viewer_producer=viewer,
            )

        self.assertEqual(quantities["need-1"], Decimal("80.000"))

    def test_critical_stock_product_ids_use_available_quantity(self):
        producer = SimpleNamespace(id="producer-1")
        stocks = [
            SimpleNamespace(
                product_id="critical-product",
                product=SimpleNamespace(id="critical-product"),
                current_quantity=Decimal("9.000"),
                reserved_quantity=Decimal("2.000"),
                safety_stock=Decimal("8.000"),
            ),
            SimpleNamespace(
                product_id="normal-product",
                product=SimpleNamespace(id="normal-product"),
                current_quantity=Decimal("12.000"),
                reserved_quantity=Decimal("1.000"),
                safety_stock=Decimal("8.000"),
            ),
        ]
        qs = MagicMock()
        qs.filter.return_value = qs
        qs.select_related.return_value = qs
        qs.only.return_value = stocks

        def commitment_side_effect(producer, product, stock=None):
            return {
                "state_key": "critical" if stock.product_id == "critical-product" else "normal",
            }

        with (
            patch("apps.needs.services.Stock.objects.filter", return_value=qs),
            patch(
                "apps.inventory.services.calculate_inventory_commitment_state",
                side_effect=commitment_side_effect,
            ),
        ):
            product_ids = get_critical_stock_product_ids(
                producer,
                product_ids=["critical-product", "normal-product"],
            )

        self.assertEqual(product_ids, {"critical-product"})

    def test_need_responses_are_explicit_domain_objects(self):
        listing_id = uuid4()
        need_id = uuid4()
        listing = SimpleNamespace(
            id=listing_id,
            need_id=need_id,
            producer=SimpleNamespace(display_name="Produtor B"),
            product=SimpleNamespace(name="Tomate", unit="kg"),
            stock_id="stock-1",
            forecast_id=None,
            quantity_available=Decimal("5.000"),
            unit_price=Decimal("2.50"),
            status="ACTIVE",
            need_response_status=NeedResponseStatus.PENDING,
            notes="Entrega amanhã.",
            get_status_display=lambda: "Ativo",
        )

        with patch(
            "apps.needs.services._get_need_response_listings_for_owner",
            return_value=[listing],
        ), patch(
            "apps.needs.services._get_need_response_order_state_listing_ids",
            return_value=(set(), set(), set()),
        ), patch(
            "apps.needs.services.get_need_response_order_snapshot",
            return_value={},
        ):
            responses = list_need_responses_for_owner(
                owner_producer=SimpleNamespace(id="owner-1"),
                need_id=str(need_id),
            )

        response = responses[0]
        self.assertEqual(response.producer_label, "Produtor B")
        self.assertEqual(response.product_name, "Tomate")
        self.assertEqual(response.source_label, "Disponível agora")
        self.assertEqual(response.response_status_label, "Pendente")
        self.assertEqual(response.offered_quantity, Decimal("5.000"))
        self.assertEqual(response.available_quantity, Decimal("5.000"))
        self.assertEqual(response.ordered_quantity, Decimal("0.000"))
        self.assertEqual(response.cta_label, "Ver oferta e comprar")
        self.assertEqual(response.detail_url, f"/necessidades/respostas/{listing_id}/")
        self.assertEqual(response.reject_url, f"/necessidades/respostas/{listing_id}/rejeitar/")
        self.assertEqual(response.edit_url, f"/necessidades/respostas/{listing_id}/editar/")
        self.assertTrue(response.is_editable)

    def test_responder_summary_marks_active_response(self):
        listing_id = uuid4()
        need_id = uuid4()
        listing = SimpleNamespace(
            id=listing_id,
            need_id=need_id,
            producer=SimpleNamespace(display_name="Produtor B"),
            product=SimpleNamespace(name="Tomate", unit="kg"),
            stock_id="stock-1",
            forecast_id=None,
            quantity_available=Decimal("5.000"),
            unit_price=Decimal("2.50"),
            status=ListingStatus.ACTIVE,
            need_response_status=NeedResponseStatus.PENDING,
            notes="",
            get_status_display=lambda: "Ativo",
        )

        qs = MagicMock()
        qs.filter.return_value.order_by.return_value = [listing]

        with (
            patch("apps.needs.services._get_need_response_listing_queryset", return_value=qs),
            patch("apps.needs.services._get_need_response_order_state_listing_ids", return_value=(set(), set(), set())),
            patch("apps.needs.services.get_need_response_order_snapshot", return_value={}),
        ):
            summaries = get_need_response_summaries_for_responder(
                responder_producer=SimpleNamespace(id="seller-1"),
                need_ids=[need_id],
            )

        summary = summaries[str(need_id)]
        self.assertEqual(summary.status_label, "Pendente")
        self.assertTrue(summary.is_active)
        self.assertTrue(summary.can_edit)
        self.assertFalse(summary.can_send_new_proposal)
        self.assertEqual(summary.detail_url, f"/necessidades/respostas/{listing_id}/")
        self.assertEqual(summary.edit_url, f"/necessidades/respostas/{listing_id}/editar/")

    def test_responder_summary_allows_new_proposal_after_rejection(self):
        listing_id = uuid4()
        need_id = uuid4()
        listing = SimpleNamespace(
            id=listing_id,
            need_id=need_id,
            producer=SimpleNamespace(display_name="Produtor B"),
            product=SimpleNamespace(name="Tomate", unit="kg"),
            stock_id="stock-1",
            forecast_id=None,
            quantity_available=Decimal("5.000"),
            unit_price=Decimal("2.50"),
            status=ListingStatus.CANCELLED,
            need_response_status=NeedResponseStatus.REJECTED,
            notes="",
            get_status_display=lambda: "Desativado",
        )

        qs = MagicMock()
        qs.filter.return_value.order_by.return_value = [listing]

        with (
            patch("apps.needs.services._get_need_response_listing_queryset", return_value=qs),
            patch("apps.needs.services._get_need_response_order_state_listing_ids", return_value=(set(), set(), set())),
            patch("apps.needs.services.get_need_response_order_snapshot", return_value={}),
        ):
            summaries = get_need_response_summaries_for_responder(
                responder_producer=SimpleNamespace(id="seller-1"),
                need_ids=[need_id],
            )

        summary = summaries[str(need_id)]
        self.assertEqual(summary.status_label, "Rejeitada")
        self.assertFalse(summary.is_active)
        self.assertTrue(summary.can_send_new_proposal)

    def test_rejected_response_is_not_active_for_publish_warning(self):
        listing = SimpleNamespace(
            id=uuid4(),
            status=ListingStatus.CANCELLED,
            need_response_status=NeedResponseStatus.REJECTED,
            quantity_available=Decimal("5.000"),
            get_status_display=lambda: "Desativado",
        )
        qs = MagicMock()
        qs.filter.return_value.order_by.return_value = [listing]

        with (
            patch("apps.needs.services._get_need_response_listing_queryset", return_value=qs),
            patch("apps.needs.services._get_need_response_order_state_listing_ids", return_value=(set(), set(), set())),
        ):
            active = get_active_need_response_for_responder(
                responder_producer=SimpleNamespace(id="seller-1"),
                need=SimpleNamespace(id="need-1"),
            )

        self.assertIsNone(active)

    def test_need_response_cancelled_order_is_historical_state(self):
        listing_id = uuid4()
        need_id = uuid4()
        listing = SimpleNamespace(
            id=listing_id,
            need_id=need_id,
            producer=SimpleNamespace(display_name="Produtor B"),
            product=SimpleNamespace(name="Tomate", unit="kg"),
            stock_id="stock-1",
            forecast_id=None,
            quantity_available=Decimal("5.000"),
            unit_price=Decimal("2.50"),
            status=ListingStatus.ACTIVE,
            need_response_status=NeedResponseStatus.PENDING,
            notes="",
            get_status_display=lambda: "Ativo",
        )

        with patch(
            "apps.needs.services._get_need_response_listings_for_owner",
            return_value=[listing],
        ), patch(
            "apps.needs.services._get_need_response_order_state_listing_ids",
            return_value=(set(), {listing_id}, set()),
        ), patch(
            "apps.needs.services.get_need_response_order_snapshot",
            return_value={
                listing_id: {
                    "status": NeedResponseStatus.CANCELLED,
                    "ordered_quantity": Decimal("5.000"),
                    "item_statuses": [OrderItemStatus.CANCELLED],
                }
            },
        ):
            responses = list_need_responses_for_owner(
                owner_producer=SimpleNamespace(id="owner-1"),
                need_id=str(need_id),
            )

        response = responses[0]
        self.assertEqual(response.response_status, "CANCELLED")
        self.assertEqual(response.response_status_label, "Cancelada")
        self.assertEqual(response.offered_quantity, Decimal("5.000"))
        self.assertEqual(response.ordered_quantity, Decimal("5.000"))
        self.assertFalse(response.can_buy)
        self.assertFalse(response.can_reject)

    def test_need_response_completed_order_is_concluded_state(self):
        listing_id = uuid4()
        need_id = uuid4()
        listing = SimpleNamespace(
            id=listing_id,
            need_id=need_id,
            need=SimpleNamespace(producer=SimpleNamespace(display_name="Produtor A")),
            producer=SimpleNamespace(display_name="Produtor B"),
            product=SimpleNamespace(name="Tomate", unit="kg"),
            stock_id="stock-1",
            forecast_id=None,
            quantity_available=Decimal("5.000"),
            unit_price=Decimal("2.50"),
            status=ListingStatus.ACTIVE,
            need_response_status=NeedResponseStatus.PENDING,
            notes="",
            get_status_display=lambda: "Ativo",
        )

        with patch(
            "apps.needs.services._get_need_response_listings_for_owner",
            return_value=[listing],
        ), patch(
            "apps.needs.services._get_need_response_order_state_listing_ids",
            return_value=(set(), set(), {listing_id}),
        ), patch(
            "apps.needs.services.get_need_response_order_snapshot",
            return_value={
                listing_id: {
                    "status": NeedResponseStatus.COMPLETED,
                    "ordered_quantity": Decimal("5.000"),
                    "item_statuses": [OrderItemStatus.COMPLETED],
                }
            },
        ):
            responses = list_need_responses_for_owner(
                owner_producer=SimpleNamespace(id="owner-1"),
                need_id=str(need_id),
            )

        response = responses[0]
        self.assertEqual(response.response_status, "COMPLETED")
        self.assertEqual(response.response_status_label, "Concluída")
        self.assertEqual(response.offered_quantity, Decimal("5.000"))
        self.assertEqual(response.ordered_quantity, Decimal("5.000"))
        self.assertFalse(response.can_buy)
        self.assertFalse(response.can_reject)

    def test_expired_active_listing_is_read_as_expired_without_persisting(self):
        listing_id = uuid4()
        need_id = uuid4()
        listing = SimpleNamespace(
            id=listing_id,
            need_id=need_id,
            producer=SimpleNamespace(display_name="Produtor B"),
            product=SimpleNamespace(name="Tomate", unit="kg"),
            stock_id="stock-1",
            forecast_id=None,
            quantity_total=Decimal("5.000"),
            quantity_available=Decimal("5.000"),
            unit_price=Decimal("2.50"),
            status=ListingStatus.ACTIVE,
            need_response_status=NeedResponseStatus.PENDING,
            expires_at=timezone.now() - timedelta(hours=1),
            notes="",
            get_status_display=lambda: "Ativo",
        )

        with patch(
            "apps.needs.services._get_need_response_listings_for_owner",
            return_value=[listing],
        ), patch(
            "apps.needs.services._get_need_response_order_state_listing_ids",
            return_value=(set(), set(), set()),
        ), patch(
            "apps.needs.services.get_need_response_order_snapshot",
            return_value={},
        ):
            responses = list_need_responses_for_owner(
                owner_producer=SimpleNamespace(id="owner-1"),
                need_id=str(need_id),
            )

        response = responses[0]
        self.assertEqual(response.response_status, NeedResponseStatus.EXPIRED)
        self.assertFalse(response.can_buy)
        self.assertFalse(response.is_editable)

    def test_completed_response_summary_allows_new_proposal(self):
        listing_id = uuid4()
        need_id = uuid4()
        listing = SimpleNamespace(
            id=listing_id,
            need_id=need_id,
            producer=SimpleNamespace(display_name="Produtor B"),
            product=SimpleNamespace(name="Tomate", unit="kg"),
            stock_id="stock-1",
            forecast_id=None,
            quantity_available=Decimal("5.000"),
            unit_price=Decimal("2.50"),
            status=ListingStatus.ACTIVE,
            need_response_status=NeedResponseStatus.PENDING,
            notes="",
            get_status_display=lambda: "Ativo",
        )
        qs = MagicMock()
        qs.filter.return_value.order_by.return_value = [listing]

        with (
            patch("apps.needs.services._get_need_response_listing_queryset", return_value=qs),
            patch("apps.needs.services._get_need_response_order_state_listing_ids", return_value=(set(), set(), {listing_id})),
            patch(
                "apps.needs.services.get_need_response_order_snapshot",
                return_value={
                    listing_id: {
                        "status": NeedResponseStatus.COMPLETED,
                        "ordered_quantity": Decimal("5.000"),
                        "item_statuses": [OrderItemStatus.COMPLETED],
                    }
                },
            ),
        ):
            summaries = get_need_response_summaries_for_responder(
                responder_producer=SimpleNamespace(id="seller-1"),
                need_ids=[need_id],
            )

        summary = summaries[str(need_id)]
        self.assertEqual(summary.status, "COMPLETED")
        self.assertEqual(summary.status_label, "Concluída")
        self.assertFalse(summary.is_active)
        self.assertFalse(summary.can_edit)
        self.assertTrue(summary.can_send_new_proposal)
        self.assertEqual(summary.detail_url, f"/necessidades/respostas/{listing_id}/")

    def test_sent_need_responses_are_explicit_history_rows(self):
        listing_id = uuid4()
        need_id = uuid4()
        listing = SimpleNamespace(
            id=listing_id,
            need_id=need_id,
            need=SimpleNamespace(producer=SimpleNamespace(display_name="Produtor A")),
            producer=SimpleNamespace(display_name="Produtor B"),
            product=SimpleNamespace(name="Tomate", unit="kg"),
            stock_id="stock-1",
            forecast_id=None,
            quantity_available=Decimal("5.000"),
            unit_price=Decimal("2.50"),
            status=ListingStatus.ACTIVE,
            need_response_status=NeedResponseStatus.PENDING,
            notes="",
            get_status_display=lambda: "Ativo",
        )
        qs = MagicMock()
        qs.filter.return_value.exclude.return_value.order_by.return_value = [listing]

        with (
            patch("apps.needs.services._get_need_response_listing_queryset", return_value=qs),
            patch("apps.needs.services._get_need_response_order_state_listing_ids", return_value=(set(), set(), {listing_id})),
            patch(
                "apps.needs.services.get_need_response_order_snapshot",
                return_value={
                    listing_id: {
                        "status": NeedResponseStatus.COMPLETED,
                        "ordered_quantity": Decimal("5.000"),
                        "item_statuses": [OrderItemStatus.COMPLETED],
                    }
                },
            ),
        ):
            responses = list_need_responses_for_responder(
                responder_producer=SimpleNamespace(id="seller-1"),
            )

        response = responses[0]
        self.assertEqual(response.need_owner_label, "Produtor A")
        self.assertEqual(response.response_status, "COMPLETED")
        self.assertEqual(response.ordered_quantity, Decimal("5.000"))

    def test_reject_need_response_marks_response_and_closes_listing(self):
        owner = SimpleNamespace(id="owner-1")
        listing = MagicMock(
            id="listing-1",
            need=SimpleNamespace(producer_id="owner-1"),
            need_response_status=NeedResponseStatus.PENDING,
            status=ListingStatus.ACTIVE,
        )
        qs = MagicMock()
        qs.select_for_update.return_value.get.return_value = listing
        reject = getattr(reject_need_response, "__wrapped__", reject_need_response)

        with (
            patch("apps.needs.services._get_need_response_listing_for_update", return_value=listing),
            patch("apps.needs.services._get_accepted_need_response_listing_ids", return_value=set()),
        ):
            changed = reject(listing=listing, owner_producer=owner)

        self.assertTrue(changed)
        self.assertEqual(listing.need_response_status, NeedResponseStatus.REJECTED)
        self.assertEqual(listing.status, ListingStatus.CANCELLED)
        listing.save.assert_called_once()

    def test_reject_need_response_blocks_accepted_listing(self):
        owner = SimpleNamespace(id="owner-1")
        listing = MagicMock(
            id="listing-1",
            need=SimpleNamespace(producer_id="owner-1"),
            need_response_status=NeedResponseStatus.PENDING,
            status=ListingStatus.ACTIVE,
        )
        qs = MagicMock()
        qs.select_for_update.return_value.get.return_value = listing
        reject = getattr(reject_need_response, "__wrapped__", reject_need_response)

        with (
            patch("apps.needs.services._get_need_response_listing_for_update", return_value=listing),
            patch("apps.needs.services._get_accepted_need_response_listing_ids", return_value={"listing-1"}),
        ):
            with self.assertRaisesMessage(Exception, "Esta oferta já foi aceite"):
                reject(listing=listing, owner_producer=owner)

    def test_update_need_response_reuses_existing_listing(self):
        responder = SimpleNamespace(id="seller-1")
        listing = MagicMock(
            id="listing-1",
            producer_id="seller-1",
            need=SimpleNamespace(producer_id="buyer-1"),
            status=ListingStatus.ACTIVE,
            need_response_status=NeedResponseStatus.PENDING,
            quantity_available=Decimal("8.000"),
            photo_path=None,
        )
        update = getattr(update_need_response, "__wrapped__", update_need_response)

        with (
            patch("apps.needs.services._get_need_response_listing_for_update", return_value=listing),
            patch("apps.needs.services._get_need_response_order_state_listing_ids", return_value=(set(), set(), set())),
            patch("apps.marketplace.services.update_listing", return_value=listing) as update_listing,
        ):
            updated_listing = update(
                listing=listing,
                responder_producer=responder,
                quantity=Decimal("8"),
                unit_price=Decimal("2.80"),
                delivery_mode="PICKUP",
                notes="Condições ajustadas.",
            )

        self.assertIs(updated_listing, listing)
        update_listing.assert_called_once()
        self.assertEqual(update_listing.call_args.kwargs["listing"], listing)
        self.assertEqual(update_listing.call_args.kwargs["quantity_total"], Decimal("8"))

    def test_needs_context_does_not_auto_select_first_own_need(self):
        need = SimpleNamespace(
            id="need-1",
            product_id="product-1",
            product=SimpleNamespace(id="product-1", name="Tomate", unit="kg", category=None),
        )
        own_row = {
            "need": need,
            "status": NeedStatus.OPEN,
            "status_label": "Aberta",
            "required_quantity": Decimal("10.000"),
            "planned_qty": Decimal("0.000"),
            "completed_qty": Decimal("0.000"),
            "remaining_to_plan": Decimal("10.000"),
            "remaining_to_receive": Decimal("10.000"),
            "progress_percent": Decimal("0"),
        }
        active_response = SimpleNamespace(id="response-1", response_status="PENDING")
        past_response = SimpleNamespace(id="response-2", response_status="REJECTED")

        with (
            patch("apps.needs.views.list_marketplace_public_needs", return_value=[]),
            patch("apps.needs.views.list_marketplace_my_needs", return_value=[own_row]),
            patch("apps.needs.views.get_need_candidate_products", return_value=[need.product]),
            patch("apps.needs.views.get_critical_stock_product_ids", return_value={"product-1"}),
            patch("apps.needs.views.get_need_response_counts_for_owner", return_value={"need-1": 1}),
            patch("apps.needs.views.list_need_responses_for_owner", return_value=[active_response, past_response]) as responses,
            patch("apps.needs.views.list_need_responses_for_responder", return_value=[active_response, past_response]) as sent_responses,
            patch("apps.needs.views.list_external_customer_demands", return_value=[]),
            patch("apps.needs.views.ExternalCustomerDemand.objects.filter") as demand_filter,
            patch("apps.needs.views.Stock.objects.filter", return_value=[]),
        ):
            demand_filter.return_value.select_related.return_value.order_by.return_value.__getitem__ = lambda self, s: iter([])
            demand_filter.return_value.count.return_value = 0
            context = build_needs_index_context(
                SimpleNamespace(id="owner-1"),
                q="",
                category_id="",
            )

        self.assertEqual(context["selected_need_id"], "")
        self.assertIsNone(context["selected_need_row"])
        self.assertEqual(context["need_my_rows"][0]["response_count"], 1)
        self.assertEqual(context["need_response_rows"], [])
        self.assertEqual(context["active_need_response_rows"], [])
        self.assertEqual(context["past_need_response_rows"], [])
        self.assertEqual(context["all_past_need_response_rows"], [past_response])
        self.assertEqual(context["received_past_need_response_rows"], [past_response])
        self.assertEqual(context["sent_need_response_rows"], [active_response, past_response])
        self.assertEqual(context["sent_active_need_response_rows"], [active_response])
        self.assertEqual(context["sent_past_need_response_rows"], [past_response])
        self.assertTrue(context["need_products"][0].is_critical_stock)
        responses.assert_any_call(
            owner_producer=SimpleNamespace(id="owner-1"),
            q="",
            category_id="",
        )
        sent_responses.assert_called_once()


class ExternalDemandConflictTests(SimpleTestCase):
    def _commitment(self, max_deficit, temporal_sellable=Decimal("0.000"), first_deficit_date=None):
        return {
            "max_deficit": max_deficit,
            "temporal_sellable_quantity": temporal_sellable,
            "first_deficit_date": first_deficit_date,
            "has_external_demands": True,
        }

    @patch("apps.marketplace.models.MarketplaceListing")
    @patch("apps.inventory.services.calculate_inventory_commitment_state")
    def test_returns_none_when_no_deficit(self, commit_mock, listing_mock):
        commit_mock.return_value = self._commitment(
            max_deficit=Decimal("0.000"),
            temporal_sellable=Decimal("50.000"),
        )
        listing_mock.objects.filter.return_value.aggregate.return_value = {
            "total": Decimal("0.000"),
            "count": 0,
        }
        result = evaluate_external_demand_conflict_with_listings(
            producer=SimpleNamespace(id="p1"),
            product=SimpleNamespace(id="prod1", unit="kg"),
        )
        self.assertIsNone(result)

    @patch("apps.marketplace.models.MarketplaceListing")
    @patch("apps.inventory.services.calculate_inventory_commitment_state")
    def test_returns_conflict_when_listings_exceed_capacity(self, commit_mock, listing_mock):
        commit_mock.return_value = self._commitment(
            max_deficit=Decimal("30.000"),
            temporal_sellable=Decimal("-10.000"),
            first_deficit_date=date(2026, 7, 1),
        )
        listing_mock.objects.filter.return_value.aggregate.return_value = {
            "total": Decimal("50.000"),
            "count": 2,
        }
        result = evaluate_external_demand_conflict_with_listings(
            producer=SimpleNamespace(id="p1"),
            product=SimpleNamespace(id="prod1", unit="kg"),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["max_deficit"], Decimal("30.000"))
        self.assertEqual(result["published_quantity"], Decimal("50.000"))
        self.assertEqual(result["affected_listings_count"], 2)

    @patch("apps.marketplace.models.MarketplaceListing")
    @patch("apps.inventory.services.calculate_inventory_commitment_state")
    def test_returns_none_when_no_published_listings(self, commit_mock, listing_mock):
        commit_mock.return_value = self._commitment(
            max_deficit=Decimal("30.000"),
        )
        listing_mock.objects.filter.return_value.aggregate.return_value = {
            "total": Decimal("0.000"),
            "count": 0,
        }
        result = evaluate_external_demand_conflict_with_listings(
            producer=SimpleNamespace(id="p1"),
            product=SimpleNamespace(id="prod1", unit="kg"),
        )
        self.assertIsNone(result)
