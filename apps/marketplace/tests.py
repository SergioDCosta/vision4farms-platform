from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
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

        with (
            patch("apps.marketplace.views.get_current_producer_for_user", return_value=None),
            patch("apps.marketplace.views.expire_due_active_listings"),
        ):
            response = marketplace_index_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/necessidades/?q=tomate&show_need_form=1")


class MarketplacePublishNeedResponseTests(SimpleTestCase):
    def _build_request(self, *, include_photo=False):
        data = {
            "from": "need",
            "need_id": "need-1",
            "product": "product-1",
            "listing_source": "stock",
            "quantity": "5",
            "unit_price": "2.50",
            "delivery_mode": "PICKUP",
            "notes": "Posso entregar esta quantidade.",
        }
        if include_photo:
            data["photo"] = SimpleUploadedFile(
                "oferta.jpg",
                b"fake image content",
                content_type="image/jpeg",
            )
        request = RequestFactory().post(
            "/marketplace/publicar/?from=need&need=need-1&product=product-1",
            data=data,
        )
        request.current_user = SimpleNamespace(
            is_active=True,
            account_status=AccountStatus.ACTIVE,
            role=UserRole.CLIENTE,
        )
        request.session = {}
        return request

    def _build_form(self):
        product = SimpleNamespace(id="product-1")
        form = MagicMock()
        form.is_valid.return_value = True
        form.cleaned_data = {
            "product": product,
            "quantity": "5",
            "unit_price": "2.50",
            "delivery_mode": "PICKUP",
            "delivery_radius_km": None,
            "delivery_fee": None,
            "show_location_on_map": True,
            "notes": "Posso entregar esta quantidade.",
            "photo_crop": "",
            "status": None,
            "expires_at_final": None,
            "listing_source": "stock",
            "forecast": None,
        }
        form.__getitem__.side_effect = lambda field_name: SimpleNamespace(
            value=lambda: {
                "product": "product-1",
                "listing_source": "stock",
            }.get(field_name, "")
        )
        form.fields = {
            "product": SimpleNamespace(
                queryset=SimpleNamespace(
                    values_list=MagicMock(return_value=["product-1"])
                )
            ),
        }
        return form

    def _run_publish_need_response(self, *, include_photo=False):
        producer = SimpleNamespace(id="seller-1")
        need = SimpleNamespace(
            id="need-1",
            product_id="product-1",
            producer_id="buyer-1",
            product=SimpleNamespace(id="product-1", name="Tomate", unit="kg"),
            producer=SimpleNamespace(display_name="Coop Norte", company_name=""),
            required_quantity="10",
            needed_by_date=None,
            notes="Preciso para cabaz semanal.",
        )
        need_model = MagicMock()
        need_model.objects.select_related.return_value.filter.return_value.first.return_value = need
        publishable_products = MagicMock()
        publishable_products.values.return_value = []
        form = self._build_form()

        with (
            patch("apps.marketplace.views.Need", need_model),
            patch("apps.marketplace.views.MarketplacePublishForm", return_value=form) as form_class,
            patch("apps.marketplace.views.get_current_producer_for_user", return_value=producer),
            patch("apps.marketplace.views.expire_due_active_listings"),
            patch("apps.marketplace.views.get_publishable_products", return_value=publishable_products),
            patch("apps.marketplace.views.get_marketplace_eligible_forecasts", return_value=[]),
            patch("apps.marketplace.views.get_market_price_trends_for_product_sources", return_value={}),
            patch("apps.marketplace.views.get_publishable_products_summary", return_value=[]),
            patch("apps.marketplace.views._sync_alerts_after_marketplace_change"),
            patch("apps.marketplace.views._maybe_crop_uploaded_photo") as crop_photo,
            patch("apps.marketplace.views._save_listing_photo") as save_photo,
            patch(
                "apps.marketplace.views.create_listing",
                return_value=SimpleNamespace(id="listing-1"),
            ) as create_listing,
            patch("apps.marketplace.views.messages"),
        ):
            response = marketplace_publish_view(
                self._build_request(include_photo=include_photo)
            )

        return response, create_listing, crop_photo, save_photo, form_class

    def test_need_response_redirects_back_to_selected_need(self):
        response, create_listing, _, _, _ = self._run_publish_need_response()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/necessidades/?need=need-1")
        self.assertEqual(create_listing.call_args.kwargs["need"].id, "need-1")

    def test_need_response_ignores_uploaded_photo(self):
        response, create_listing, crop_photo, save_photo, form_class = self._run_publish_need_response(
            include_photo=True
        )

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(form_class.call_args.args[1])
        self.assertIsNone(create_listing.call_args.kwargs["photo_path"])
        crop_photo.assert_not_called()
        save_photo.assert_not_called()
