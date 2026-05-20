from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from apps.accounts.models import AccountStatus, UserRole
from apps.marketplace.services import (
    MarketplaceServiceError,
    get_my_listings,
    get_public_listings,
    retire_listing,
    update_listing,
)
from apps.marketplace.models import ListingStatus
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


class MarketplacePublishNeedResponseTests(SimpleTestCase):
    def test_legacy_need_response_publish_redirects_to_needs_flow(self):
        request = RequestFactory().get(
            "/marketplace/publicar/?from=need&need=need-1&product=product-1",
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
            "/necessidades/responder/?from=need&need=need-1&product=product-1",
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
    @patch("apps.marketplace.services.get_base_listing_queryset")
    def test_public_marketplace_keeps_own_active_listings(self, base_queryset_mock):
        qs = MagicMock()
        qs.filter.return_value = qs
        base_queryset_mock.return_value = qs

        result = get_public_listings(producer=SimpleNamespace(id="producer-1"))

        self.assertIs(result, qs)
        qs.exclude.assert_not_called()

    @patch("apps.marketplace.services.get_base_listing_queryset")
    def test_my_marketplace_listings_exclude_need_responses(self, base_queryset_mock):
        qs = MagicMock()
        qs.filter.return_value = qs
        base_queryset_mock.return_value = qs
        producer = SimpleNamespace(id="producer-1")

        result = get_my_listings(producer=producer)

        self.assertIs(result, qs)
        qs.filter.assert_any_call(producer=producer, need_id__isnull=True)


class MarketplaceEditSafetyTests(SimpleTestCase):
    def test_update_listing_rejects_closed_listing(self):
        listing = SimpleNamespace(
            status=ListingStatus.CLOSED,
            quantity_reserved=0,
        )

        with self.assertRaisesMessage(MarketplaceServiceError, "não pode ser editado"):
            update_listing(
                listing=listing,
                quantity_total=10,
                unit_price=1,
                delivery_mode="PICKUP",
            )

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


class MarketplaceDeleteListingTests(SimpleTestCase):
    def test_retire_listing_cancels_without_physical_delete(self):
        listing = SimpleNamespace(
            status=ListingStatus.ACTIVE,
            quantity_available=10,
            photo_path="marketplace/listings/photo.jpg",
            save=MagicMock(),
        )

        result = retire_listing(listing=listing)

        self.assertIs(result, listing)
        self.assertEqual(listing.status, ListingStatus.CANCELLED)
        self.assertEqual(str(listing.quantity_available), "0.000")
        self.assertIsNone(listing.photo_path)
        listing.save.assert_called_once()

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
