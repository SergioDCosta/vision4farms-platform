from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from apps.accounts.models import AccountStatus, UserRole
from apps.marketplace.views import marketplace_index_view, marketplace_publish_view


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
