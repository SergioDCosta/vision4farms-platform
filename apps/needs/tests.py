from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase
from django.urls import reverse

from apps.marketplace.models import ListingStatus
from apps.needs.models import NeedResponseStatus, NeedSourceSystem, NeedStatus
from apps.needs.services import (
    calculate_need_coverage,
    create_or_update_need,
    get_critical_stock_product_ids,
    get_need_response_summaries_for_responder,
    get_active_need_response_for_responder,
    get_public_offered_quantities_by_need,
    ignore_need,
    list_need_responses_for_owner,
    list_marketplace_public_needs,
    reject_need_response,
)
from apps.needs.views import build_needs_index_context
from apps.orders.models import OrderItemStatus, OrderStatus


class FakeQuerySet(list):
    def filter(self, **kwargs):
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
        return FakeQuerySet(items)

    def only(self, *args):
        return self


class NeedsRoutingTests(SimpleTestCase):
    def test_needs_index_url_is_public_needs_path(self):
        self.assertEqual(reverse("needs:index"), "/necessidades/")

    def test_need_response_urls_are_public_needs_paths(self):
        listing_id = uuid4()

        self.assertEqual(
            reverse("needs:response_detail", args=[listing_id]),
            f"/necessidades/respostas/{listing_id}/",
        )
        self.assertEqual(
            reverse("needs:response_reject", args=[listing_id]),
            f"/necessidades/respostas/{listing_id}/rejeitar/",
        )


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

    def test_create_or_update_need_updates_existing_active_need(self):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1")
        need = MagicMock(
            producer=producer,
            product=product,
            status=NeedStatus.OPEN,
        )
        need.updated_at = None
        active_qs = MagicMock()
        active_qs.order_by.return_value = [need]
        manager = MagicMock()
        manager.objects.select_for_update.return_value.filter.return_value = active_qs
        create_or_update = getattr(create_or_update_need, "__wrapped__", create_or_update_need)

        with (
            patch("apps.needs.services.Need", manager),
            patch(
                "apps.needs.services.recalculate_need_status",
                return_value=(need, {"remaining_to_plan": Decimal("4.000")}, False),
            ),
        ):
            result, _, created = create_or_update(
                producer=producer,
                product=product,
                required_quantity=Decimal("7"),
                source_system=NeedSourceSystem.MANUAL,
                notes="Observação",
            )

        self.assertIs(result, need)
        self.assertFalse(created)
        self.assertEqual(need.required_quantity, Decimal("7.000"))
        self.assertEqual(need.notes, "Observação")

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
        self.assertEqual(rows[0]["public_quantity"], Decimal("10.000"))
        self.assertEqual(rows[0]["public_offered_quantity"], Decimal("5.000"))

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
        ])
        order_items = FakeQuerySet([
            SimpleNamespace(
                need_id="need-1",
                seller_producer_id="producer-b",
                item_status=OrderItemStatus.PENDING,
                quantity=Decimal("30.000"),
            ),
            SimpleNamespace(
                need_id="need-1",
                seller_producer_id="viewer-1",
                item_status=OrderItemStatus.PENDING,
                quantity=Decimal("5.000"),
            ),
            SimpleNamespace(
                need_id="need-1",
                seller_producer_id="producer-b",
                item_status=OrderItemStatus.CANCELLED,
                quantity=Decimal("15.000"),
            ),
            SimpleNamespace(
                need_id="need-1",
                seller_producer_id="producer-b",
                item_status=OrderItemStatus.COMPLETED,
                quantity=Decimal("20.000"),
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
                current_quantity=Decimal("10.000"),
                reserved_quantity=Decimal("2.000"),
                safety_stock=Decimal("8.000"),
            ),
            SimpleNamespace(
                product_id="normal-product",
                current_quantity=Decimal("12.000"),
                reserved_quantity=Decimal("1.000"),
                safety_stock=Decimal("8.000"),
            ),
        ]
        qs = MagicMock()
        qs.filter.return_value = qs
        qs.only.return_value = stocks

        with patch("apps.needs.services.Stock.objects.filter", return_value=qs):
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
            return_value=(set(), set()),
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
        self.assertEqual(response.cta_label, "Ver oferta e comprar")
        self.assertEqual(response.detail_url, f"/necessidades/respostas/{listing_id}/")
        self.assertEqual(response.reject_url, f"/necessidades/respostas/{listing_id}/rejeitar/")

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
            patch("apps.needs.services._get_need_response_order_state_listing_ids", return_value=(set(), set())),
        ):
            summaries = get_need_response_summaries_for_responder(
                responder_producer=SimpleNamespace(id="seller-1"),
                need_ids=[need_id],
            )

        summary = summaries[str(need_id)]
        self.assertEqual(summary.status_label, "Pendente")
        self.assertTrue(summary.is_active)
        self.assertFalse(summary.can_send_new_proposal)
        self.assertEqual(summary.detail_url, f"/necessidades/respostas/{listing_id}/")

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
            patch("apps.needs.services._get_need_response_order_state_listing_ids", return_value=(set(), set())),
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
            patch("apps.needs.services._get_need_response_order_state_listing_ids", return_value=(set(), set())),
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
            return_value=(set(), {listing_id}),
        ):
            responses = list_need_responses_for_owner(
                owner_producer=SimpleNamespace(id="owner-1"),
                need_id=str(need_id),
            )

        response = responses[0]
        self.assertEqual(response.response_status, "CANCELLED")
        self.assertEqual(response.response_status_label, "Cancelada")
        self.assertFalse(response.can_buy)
        self.assertFalse(response.can_reject)

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

    def test_needs_context_selects_first_own_need_when_none_selected(self):
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
        ):
            context = build_needs_index_context(
                SimpleNamespace(id="owner-1"),
                q="",
                category_id="",
            )

        self.assertEqual(context["selected_need_id"], "need-1")
        self.assertIs(context["selected_need_row"], own_row)
        self.assertEqual(context["need_my_rows"][0]["response_count"], 1)
        self.assertEqual(context["need_response_rows"], [active_response, past_response])
        self.assertEqual(context["active_need_response_rows"], [active_response])
        self.assertEqual(context["past_need_response_rows"], [past_response])
        self.assertEqual(context["all_past_need_response_rows"], [past_response])
        self.assertTrue(context["need_products"][0].is_critical_stock)
        responses.assert_any_call(
            owner_producer=SimpleNamespace(id="owner-1"),
            q="",
            category_id="",
            need_id="need-1",
        )
