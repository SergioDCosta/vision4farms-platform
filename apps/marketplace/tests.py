from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountStatus, UserRole
from apps.marketplace.form_validation import (
    apply_delivery_validation,
    resolve_expiration,
)
from apps.marketplace.services import (
    MarketplaceServiceError,
    create_listing,
    get_max_publishable_quantity,
    get_my_listings,
    get_public_listings,
    is_listing_editable_in_marketplace,
    is_listing_retired_in_marketplace,
    reactivate_listing,
    retire_listing,
    update_listing,
)
from apps.marketplace.models import DeliveryMode, ListingStatus
from apps.marketplace.views import (
    marketplace_delete_view,
    marketplace_detail_view,
    marketplace_edit_view,
    marketplace_index_view,
    marketplace_publish_view,
    marketplace_toggle_status_view,
)


class MarketplaceNeedsRoutingTests(SimpleTestCase):
    def test_legacy_marketplace_needs_tab_redirects_to_needs_page(self):
        request = RequestFactory().get(
            "/marketplace/",
            {
                "tab": "necessidades",
                "q": "tomate",
                "show_need_form": "1",
            },
        )
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
        )
        request.session = {}
        producer = SimpleNamespace(id="seller-1")

        with (
            patch("apps.marketplace.views.get_current_producer_for_user", return_value=None),
            patch("apps.marketplace.views.expire_due_active_listings"),
        ):
            response = marketplace_index_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/necessidades/?q=tomate&show_need_form=1")


class MarketplaceSharedFormValidationTests(SimpleTestCase):
    def test_pickup_clears_delivery_only_values(self):
        form = MagicMock()
        cleaned_data = {
            "delivery_mode": DeliveryMode.PICKUP,
            "delivery_radius_km": Decimal("10"),
            "delivery_fee": Decimal("2.50"),
        }

        apply_delivery_validation(form, cleaned_data)

        self.assertIsNone(cleaned_data["delivery_radius_km"])
        self.assertIsNone(cleaned_data["delivery_fee"])
        form.add_error.assert_not_called()

    def test_delivery_requires_radius(self):
        form = MagicMock()
        cleaned_data = {
            "delivery_mode": DeliveryMode.DELIVERY,
            "delivery_radius_km": None,
        }

        apply_delivery_validation(form, cleaned_data)

        form.add_error.assert_called_once_with(
            "delivery_radius_km",
            "Indica o raio de entrega.",
        )

    @patch("apps.marketplace.form_validation.timezone.now")
    def test_edit_timer_builds_expiration_from_current_time(self, now_mock):
        now = timezone.make_aware(datetime(2026, 6, 2, 12, 0))
        now_mock.return_value = now
        form = MagicMock()

        expires_at = resolve_expiration(
            form,
            {
                "expiration_mode": "timer",
                "expires_in": 12,
                "status": ListingStatus.ACTIVE,
            },
            allow_timer=True,
        )

        self.assertEqual(expires_at, now + timedelta(hours=12))
        form.add_error.assert_not_called()

    @patch("apps.marketplace.form_validation.timezone.now")
    def test_expired_listing_without_deadline_uses_current_time(self, now_mock):
        now = timezone.make_aware(datetime(2026, 6, 2, 12, 0))
        now_mock.return_value = now

        expires_at = resolve_expiration(
            MagicMock(),
            {
                "expiration_mode": "none",
                "status": ListingStatus.EXPIRED,
            },
        )

        self.assertEqual(expires_at, now)


class MarketplacePublishNeedResponseTests(SimpleTestCase):
    def test_legacy_need_response_publish_redirects_to_marketplace_response_flow(self):
        need_id = "f470b620-6b04-4d24-af52-f2cb736cb4e6"
        request = RequestFactory().get(
            f"/marketplace/publicar/?from=need&need={need_id}&product=product-1",
        )
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
        )
        request.session = {}
        producer = SimpleNamespace(id="seller-1")

        with (
            patch("apps.marketplace.views.get_current_producer_for_user", return_value=producer),
            patch("apps.marketplace.views.messages"),
        ):
            response = marketplace_publish_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"/marketplace/procuras/{need_id}/responder/?product=product-1",
        )


class MarketplaceDetailRoutingTests(SimpleTestCase):
    def test_owner_and_public_detail_routes_are_distinct(self):
        self.assertEqual(
            reverse("marketplace:owner_detail", args=["f470b620-6b04-4d24-af52-f2cb736cb4e6"]),
            "/marketplace/meus/f470b620-6b04-4d24-af52-f2cb736cb4e6/",
        )
        self.assertEqual(
            reverse("marketplace:public_detail", args=["f470b620-6b04-4d24-af52-f2cb736cb4e6"]),
            "/marketplace/anuncios/f470b620-6b04-4d24-af52-f2cb736cb4e6/",
        )

    def test_legacy_detail_redirects_owner_to_owner_detail(self):
        listing_id = UUID("f470b620-6b04-4d24-af52-f2cb736cb4e6")
        request = RequestFactory().get(f"/marketplace/{listing_id}/")
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
        )
        request.session = {}
        producer = SimpleNamespace(id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        listing = SimpleNamespace(id=listing_id, producer_id=producer.id, need_id=None)

        with (
            patch("apps.marketplace.views.get_current_producer_for_user", return_value=producer),
            patch("apps.marketplace.views.expire_due_active_listings"),
            patch("apps.marketplace.views.get_listing_detail_queryset", return_value=MagicMock()),
            patch("apps.marketplace.views.get_object_or_404", return_value=listing),
        ):
            response = marketplace_detail_view(request, listing.id)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            "/marketplace/meus/f470b620-6b04-4d24-af52-f2cb736cb4e6/",
        )


class MarketplaceListingVisibilityTests(SimpleTestCase):
    @patch("apps.marketplace.queries.get_base_listing_queryset")
    def test_public_marketplace_includes_own_active_listings(self, base_queryset_mock):
        qs = MagicMock()
        qs.filter.return_value = qs
        qs.exclude.return_value = qs
        base_queryset_mock.return_value = qs
        producer = SimpleNamespace(id="producer-1")

        result = get_public_listings(producer=producer)

        self.assertIs(result, qs.order_by.return_value)
        qs.exclude.assert_not_called()

    @patch("apps.marketplace.queries.get_base_listing_queryset")
    def test_public_marketplace_only_fetches_active_listings_with_quantity(self, base_queryset_mock):
        qs = MagicMock()
        qs.filter.return_value = qs
        base_queryset_mock.return_value = qs

        get_public_listings()

        qs.filter.assert_any_call(
            status=ListingStatus.ACTIVE,
            quantity_available__gt=0,
            need_id__isnull=True,
            product__is_active=True,
        )

    @patch("apps.marketplace.queries.get_base_listing_queryset")
    def test_my_marketplace_listings_exclude_need_responses(self, base_queryset_mock):
        qs = MagicMock()
        qs.filter.return_value = qs
        qs.exclude.return_value = qs
        base_queryset_mock.return_value = qs
        producer = SimpleNamespace(id="producer-1")

        result = get_my_listings(producer=producer)

        self.assertIs(result, qs.order_by.return_value)
        qs.filter.assert_any_call(producer=producer, need_id__isnull=True)
        qs.exclude.assert_called_once()


class MarketplaceQuantityLimitTests(SimpleTestCase):
    @patch("apps.marketplace.availability._get_pending_stock_need_response_quantity", return_value=Decimal("30.000"))
    @patch("apps.marketplace.availability._get_open_stock_published_quantity", return_value=Decimal("20.000"))
    @patch(
        "apps.inventory.services.calculate_inventory_commitment_state",
        return_value={
            "temporal_sellable_quantity": Decimal("100.000"),
            "has_external_demands": False,
        },
    )
    def test_pending_private_proposal_reduces_stock_available_for_new_listing(
        self,
        commitment_mock,
        public_quantity_mock,
        private_quantity_mock,
    ):
        stock = SimpleNamespace(
            id="stock-1",
            producer=SimpleNamespace(id="producer-1"),
            product=SimpleNamespace(id="product-1"),
        )

        maximum = get_max_publishable_quantity(stock)

        self.assertEqual(maximum, Decimal("50.000"))
        public_quantity_mock.assert_called_once_with(stock, exclude_listing_id=None)
        private_quantity_mock.assert_called_once_with(stock, exclude_listing_id=None)

    @patch(
        "apps.marketplace.commands.get_max_publishable_quantity",
        side_effect=[Decimal("200.000"), Decimal("0.000")],
    )
    def test_second_listing_is_blocked_after_full_publishable_margin_is_used(
        self,
        max_publishable_mock,
    ):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1", name="Batata", unit="kg")
        stock = SimpleNamespace(id="stock-1")
        listing = SimpleNamespace(
            id="listing-1",
            producer_id=producer.id,
            product_id=product.id,
            product=product,
            stock_id=stock.id,
            forecast_id=None,
            need_id=None,
            quantity_total=Decimal("200.000"),
            quantity_available=Decimal("200.000"),
            quantity_reserved=Decimal("0.000"),
            unit_price=Decimal("1.00"),
            delivery_mode="PICKUP",
            status=ListingStatus.ACTIVE,
        )
        stock_manager = MagicMock()
        stock_manager.select_for_update.return_value.filter.return_value.first.return_value = stock
        listing_manager = MagicMock()
        listing_manager.create.return_value = listing
        create = getattr(create_listing, "__wrapped__", create_listing)

        with (
            patch("apps.marketplace.commands.Stock.objects", stock_manager),
            patch("apps.marketplace.commands.MarketplaceListing.objects", listing_manager),
            patch("apps.marketplace.commands.log_audit_event"),
        ):
            result = create(
                producer=producer,
                product=product,
                quantity=Decimal("200"),
                unit_price=Decimal("1"),
                delivery_mode="PICKUP",
            )
            with self.assertRaisesMessage(MarketplaceServiceError, "não tem excedente disponível"):
                create(
                    producer=producer,
                    product=product,
                    quantity=Decimal("1"),
                    unit_price=Decimal("1"),
                    delivery_mode="PICKUP",
                )

        self.assertIs(result, listing)
        listing_manager.create.assert_called_once()
        self.assertEqual(max_publishable_mock.call_count, 2)

    @patch("apps.marketplace.commands.listing_audit_values", return_value={})
    @patch("apps.marketplace.commands.log_audit_event")
    @patch(
        "apps.marketplace.commands.get_forecast_available_quantity",
        return_value=Decimal("25.000"),
    )
    def test_forecast_listing_creation_uses_forecast_as_exclusive_source(
        self,
        available_quantity_mock,
        audit_mock,
        audit_values_mock,
    ):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1", name="Batata", unit="kg")
        forecast = SimpleNamespace(
            id="forecast-1",
            producer=producer,
            product=product,
            is_marketplace_enabled=True,
        )
        listing = SimpleNamespace(id="listing-1")
        forecast_manager = MagicMock()
        forecast_manager.select_for_update.return_value.filter.return_value.first.return_value = forecast
        listing_manager = MagicMock()
        listing_manager.create.return_value = listing
        create = getattr(create_listing, "__wrapped__", create_listing)

        with (
            patch("apps.marketplace.commands.ProductionForecast.objects", forecast_manager),
            patch("apps.marketplace.commands.MarketplaceListing.objects", listing_manager),
        ):
            result = create(
                producer=producer,
                product=product,
                quantity=Decimal("10"),
                unit_price=Decimal("1"),
                delivery_mode="PICKUP",
                listing_source="forecast",
                forecast=forecast,
            )

        self.assertIs(result, listing)
        listing_manager.create.assert_called_once()
        create_kwargs = listing_manager.create.call_args.kwargs
        self.assertIsNone(create_kwargs["stock"])
        self.assertIs(create_kwargs["forecast"], forecast)
        available_quantity_mock.assert_called_once_with(forecast)

    @patch("apps.marketplace.commands.get_max_publishable_quantity", return_value=Decimal("200.000"))
    def test_edit_active_listing_above_source_maximum_is_blocked(self, max_publishable_mock):
        product = SimpleNamespace(name="Batata", unit="kg")
        listing = SimpleNamespace(
            id="listing-1",
            status=ListingStatus.ACTIVE,
            need_id=None,
            stock_id="stock-1",
            forecast_id=None,
            product=product,
            quantity_total=Decimal("200.000"),
            quantity_available=Decimal("200.000"),
            quantity_reserved=Decimal("0.000"),
            unit_price=Decimal("1.00"),
            delivery_mode="PICKUP",
            save=MagicMock(),
        )
        stock = SimpleNamespace(id="stock-1")
        stock_manager = MagicMock()
        stock_manager.select_for_update.return_value.filter.return_value.first.return_value = stock
        update = getattr(update_listing, "__wrapped__", update_listing)

        with (
            patch("apps.marketplace.commands.Stock.objects", stock_manager),
            self.assertRaisesMessage(MarketplaceServiceError, "máximo disponível"),
        ):
            update(
                listing=listing,
                quantity_total=Decimal("201"),
                unit_price=Decimal("1"),
                delivery_mode="PICKUP",
            )

        max_publishable_mock.assert_called_once_with(stock, exclude_listing_id=listing.id)
        listing.save.assert_not_called()


class MarketplaceEditSafetyTests(SimpleTestCase):
    def test_update_listing_rejects_closed_listing(self):
        listing = SimpleNamespace(
            status=ListingStatus.CLOSED,
            quantity_reserved=0,
        )
        update = getattr(update_listing, "__wrapped__", update_listing)

        with self.assertRaisesMessage(MarketplaceServiceError, "não pode ser editado"):
            update(
                listing=listing,
                quantity_total=10,
                unit_price=1,
                delivery_mode="PICKUP",
            )

    @patch("apps.marketplace.commands.get_max_publishable_quantity", return_value=Decimal("5.000"))
    def test_reactivate_listing_rejects_quantity_above_current_publishable_limit(self, max_quantity_mock):
        listing = SimpleNamespace(
            id="listing-1",
            need_id=None,
            status=ListingStatus.CANCELLED,
            quantity_available=Decimal("10.000"),
            quantity_reserved=Decimal("0.000"),
            stock_id="stock-1",
            forecast_id=None,
            expires_at=None,
            product=SimpleNamespace(unit="kg"),
        )
        stock = SimpleNamespace(id="stock-1")
        listing_manager = MagicMock()
        listing_manager.select_for_update.return_value.select_related.return_value.get.return_value = listing
        stock_manager = MagicMock()
        stock_manager.select_for_update.return_value.filter.return_value.first.return_value = stock
        reactivate = getattr(reactivate_listing, "__wrapped__", reactivate_listing)

        with (
            patch("apps.marketplace.commands.MarketplaceListing.objects", listing_manager),
            patch("apps.marketplace.commands.Stock.objects", stock_manager),
            self.assertRaisesMessage(MarketplaceServiceError, "máximo publicável atual"),
        ):
            reactivate(listing=listing)

        max_quantity_mock.assert_called_once_with(stock, exclude_listing_id=listing.id)

    @patch("apps.marketplace.views.expire_due_active_listings")
    @patch("apps.marketplace.views.get_object_or_404")
    @patch("apps.marketplace.views.get_current_producer_for_user")
    @patch("apps.marketplace.views.messages")
    def test_edit_view_blocks_closed_listing(
        self,
        messages_mock,
        get_producer_mock,
        get_object_mock,
        expire_mock,
    ):
        listing_id = UUID("f470b620-6b04-4d24-af52-f2cb736cb4e6")
        request = RequestFactory().get(f"/marketplace/{listing_id}/editar/")
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
        )
        request.session = {}
        producer = SimpleNamespace(id="producer-1")
        listing = SimpleNamespace(
            id=listing_id,
            producer=producer,
            producer_id="producer-1",
            need_id=None,
            status=ListingStatus.CLOSED,
        )
        get_producer_mock.return_value = producer
        get_object_mock.return_value = listing

        response = marketplace_edit_view.__wrapped__(request, listing_id)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            "/marketplace/meus/f470b620-6b04-4d24-af52-f2cb736cb4e6/",
        )
        messages_mock.warning.assert_called_once()

    @patch("apps.marketplace.views.log_audit_event")
    @patch("apps.marketplace.views.expire_due_active_listings")
    @patch("apps.marketplace.views._build_marketplace_index_context", return_value={})
    @patch("apps.marketplace.views._sync_alerts_after_marketplace_change")
    @patch("apps.marketplace.views.get_object_or_404")
    @patch("apps.marketplace.views.get_current_producer_for_user")
    @patch("apps.marketplace.views.messages")
    def test_toggle_status_rejects_external_next_url(
        self,
        messages_mock,
        get_producer_mock,
        get_object_mock,
        sync_mock,
        context_mock,
        expire_mock,
        audit_mock,
    ):
        listing_id = UUID("f470b620-6b04-4d24-af52-f2cb736cb4e6")
        request = RequestFactory().post(
            f"/marketplace/{listing_id}/estado/",
            {"next": "https://evil.example/phish"},
        )
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
            id="user-1",
        )
        request.session = {}
        producer = SimpleNamespace(id="producer-1")
        listing = SimpleNamespace(
            id=listing_id,
            producer=producer,
            producer_id="producer-1",
            need_id=None,
            status=ListingStatus.ACTIVE,
            quantity_available=10,
            quantity_reserved=0,
            expires_at=None,
            save=MagicMock(),
        )
        get_producer_mock.return_value = producer
        get_object_mock.return_value = listing

        response = marketplace_toggle_status_view.__wrapped__(request, listing_id)

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example", response["Location"])
        self.assertEqual(response["Location"], "/marketplace/?tab=todos&q=&category=")

    @patch("apps.marketplace.views.render", return_value=HttpResponse('<div id="mk-marketplace-workspace"></div>'))
    @patch("apps.marketplace.views.log_audit_event")
    @patch("apps.marketplace.views.expire_due_active_listings")
    @patch("apps.marketplace.views._build_marketplace_index_context", return_value={"active_tab": "meus"})
    @patch("apps.marketplace.views._sync_alerts_after_marketplace_change")
    @patch("apps.marketplace.views.get_object_or_404")
    @patch("apps.marketplace.views.get_current_producer_for_user")
    @patch("apps.marketplace.views.messages")
    def test_htmx_toggle_renders_fresh_index_context_for_counts_and_grid(
        self,
        messages_mock,
        get_producer_mock,
        get_object_mock,
        sync_mock,
        context_mock,
        expire_mock,
        audit_mock,
        render_mock,
    ):
        listing_id = UUID("f470b620-6b04-4d24-af52-f2cb736cb4e6")
        request = RequestFactory().post(
            f"/marketplace/{listing_id}/estado/",
            {"tab": "meus"},
            HTTP_HX_REQUEST="true",
        )
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
            id="user-1",
        )
        request.session = {}
        producer = SimpleNamespace(id="producer-1")
        listing = SimpleNamespace(
            id=listing_id,
            producer=producer,
            producer_id=producer.id,
            need_id=None,
            forecast_id=None,
            product=SimpleNamespace(name="Batata"),
            status=ListingStatus.ACTIVE,
            quantity_available=Decimal("10"),
            quantity_reserved=Decimal("0"),
            expires_at=None,
            save=MagicMock(),
        )
        get_producer_mock.return_value = producer
        get_object_mock.return_value = listing

        response = marketplace_toggle_status_view.__wrapped__(request, listing_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(listing.status, ListingStatus.CANCELLED)
        context_mock.assert_called_once()
        render_mock.assert_called_once_with(
            request,
            "marketplace/index.html",
            {"active_tab": "meus"},
        )

    def test_status_toggle_targets_workspace_that_contains_tabs_and_results(self):
        project_root = Path(__file__).resolve().parents[2]
        card_template = (
            project_root / "templates" / "marketplace" / "partials" / "listing_card.html"
        ).read_text(encoding="utf-8")
        index_template = (project_root / "templates" / "marketplace" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('hx-target="#mk-marketplace-workspace"', card_template)
        self.assertIn('hx-select="#mk-marketplace-workspace"', card_template)
        self.assertIn('id="mk-marketplace-workspace"', index_template)


class MarketplaceDeleteListingTests(SimpleTestCase):
    @patch("apps.marketplace.commands.log_audit_event")
    def test_retire_listing_cancels_without_physical_delete(self, audit_mock):
        listing = SimpleNamespace(
            status=ListingStatus.ACTIVE,
            quantity_available=10,
            photo_path="marketplace/listings/photo.jpg",
            expires_at=None,
            save=MagicMock(),
        )

        result = retire_listing(listing=listing)

        self.assertIs(result, listing)
        self.assertEqual(listing.status, ListingStatus.CANCELLED)
        self.assertEqual(str(listing.quantity_available), "0.000")
        self.assertIsNone(listing.photo_path)
        self.assertIsNotNone(listing.expires_at)
        listing.save.assert_called_once()

    def test_retired_listing_cannot_be_edited_again(self):
        listing = SimpleNamespace(
            status=ListingStatus.CANCELLED,
            quantity_available=0,
            photo_path=None,
            expires_at=None,
            need_id=None,
        )

        self.assertFalse(is_listing_editable_in_marketplace(listing))

    def test_disabled_listing_with_available_quantity_remains_manageable(self):
        listing = SimpleNamespace(
            status=ListingStatus.CANCELLED,
            quantity_available=Decimal("10.000"),
            photo_path="marketplace/listings/photo.jpg",
            expires_at=None,
            need_id=None,
        )

        self.assertFalse(is_listing_retired_in_marketplace(listing))
        self.assertTrue(is_listing_editable_in_marketplace(listing))

    @patch("apps.marketplace.views._sync_alerts_after_marketplace_change")
    @patch("apps.marketplace.views._delete_uploaded_file")
    @patch("apps.marketplace.views.retire_listing")
    @patch("apps.marketplace.views.get_object_or_404")
    @patch("apps.marketplace.views.get_current_producer_for_user")
    @patch("apps.marketplace.views.messages")
    def test_delete_view_uses_soft_delete_to_preserve_history(
        self,
        messages_mock,
        get_producer_mock,
        get_object_mock,
        retire_listing_mock,
        delete_file_mock,
        sync_alerts_mock,
    ):
        request = RequestFactory().post("/marketplace/listing-1/eliminar/")
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
            id="user-1",
        )
        request.session = {}
        producer = SimpleNamespace(id="producer-1")
        listing = SimpleNamespace(
            id="listing-1",
            quantity_reserved=0,
            need_id=None,
            photo_path="marketplace/listings/photo.jpg",
            delete=MagicMock(),
        )
        get_producer_mock.return_value = producer
        get_object_mock.return_value = listing

        response = marketplace_delete_view.__wrapped__(request, "listing-1")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/marketplace/?tab=meus")
        retire_listing_mock.assert_called_once_with(listing=listing)
        listing.delete.assert_not_called()
        delete_file_mock.assert_called_once_with("marketplace/listings/photo.jpg")
        sync_alerts_mock.assert_called_once()
