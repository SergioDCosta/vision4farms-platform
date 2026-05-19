from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from apps.accounts.models import AccountStatus, UserRole
from apps.marketplace.models import ListingStatus
from apps.marketplace.services import LISTING_SOURCE_STOCK
from apps.needs.forms import NeedEditForm, NeedResponseEditForm
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
    list_need_responses_for_responder,
    list_marketplace_public_needs,
    reject_need_response,
    update_need,
    update_need_response,
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

        with (
            patch("apps.needs.views.Need", need_model),
            patch("apps.needs.views.NeedResponsePublishForm", return_value=form),
            patch("apps.needs.views.get_current_producer_for_user", return_value=producer),
            patch("apps.needs.views.expire_due_active_listings"),
            patch("apps.needs.views.calculate_need_coverage", return_value={"remaining_to_receive": Decimal("10")}),
            patch("apps.needs.views.get_active_need_response_for_responder", return_value=None),
            patch("apps.needs.views.get_need_response_summaries_for_responder", return_value={}),
            patch("apps.needs.views.create_listing", return_value=SimpleNamespace(id="listing-1")) as create_listing,
            patch("apps.needs.views.sync_alerts_after_need_change"),
            patch("apps.needs.views.messages"),
        ):
            from apps.needs.views import need_response_publish_view

            response = need_response_publish_view(self._request())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/necessidades/?need=need-1")
        self.assertEqual(create_listing.call_args.kwargs["need"], need)
        self.assertIsNone(create_listing.call_args.kwargs["photo_path"])
        self.assertEqual(create_listing.call_args.kwargs["status"], ListingStatus.ACTIVE)

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
            patch("apps.needs.views.expire_due_active_listings"),
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
            patch("apps.needs.views.expire_due_active_listings"),
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
            patch("apps.needs.views.list_need_responses_for_responder", return_value=[active_response, past_response]) as sent_responses,
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
        self.assertEqual(context["received_past_need_response_rows"], [past_response])
        self.assertEqual(context["sent_need_response_rows"], [active_response, past_response])
        self.assertEqual(context["sent_active_need_response_rows"], [active_response])
        self.assertEqual(context["sent_past_need_response_rows"], [past_response])
        self.assertTrue(context["need_products"][0].is_critical_stock)
        responses.assert_any_call(
            owner_producer=SimpleNamespace(id="owner-1"),
            q="",
            category_id="",
            need_id="need-1",
        )
        sent_responses.assert_called_once()
