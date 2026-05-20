from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase

from apps.accounts.models import AccountStatus, UserRole
from apps.marketplace.services import get_public_listings, retire_listing
from apps.marketplace.models import ListingStatus
from apps.marketplace.views import (
    marketplace_delete_view,
    marketplace_index_view,
    marketplace_publish_view,
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


class MarketplaceListingVisibilityTests(SimpleTestCase):
    @patch("apps.marketplace.services.get_base_listing_queryset")
    def test_public_marketplace_keeps_own_active_listings(self, base_queryset_mock):
        qs = MagicMock()
        qs.filter.return_value = qs
        base_queryset_mock.return_value = qs

        result = get_public_listings(producer=SimpleNamespace(id="producer-1"))

        self.assertIs(result, qs)
        qs.exclude.assert_not_called()


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
