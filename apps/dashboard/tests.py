from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.cache import caches
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from apps.dashboard import views
from apps.dashboard.services import weather as weather_service


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "dashboard-tests-default",
    },
    "weather": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "dashboard-tests-weather",
    },
}


class _MockResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@override_settings(CACHES=TEST_CACHES)
class DashboardWeatherServiceTests(SimpleTestCase):
    def setUp(self):
        caches["weather"].clear()

    def _requests_get_side_effect(self, url, timeout=None, headers=None):
        today = timezone.localdate()

        if url == weather_service.IPMA_LOCATIONS_URL:
            return _MockResponse(
                {
                    "data": [
                        {"globalIdLocal": 1010500, "local": "Viseu"},
                        {"globalIdLocal": 1110600, "local": "Lisboa"},
                        {"globalIdLocal": 2310300, "local": "Funchal"},
                    ]
                }
            )

        if url == weather_service.IPMA_WEATHER_TYPES_URL:
            return _MockResponse(
                {"data": [{"idWeatherType": 3, "descWeatherTypePT": "Céu pouco nublado"}]}
            )

        if "forecast/meteorology/cities/daily/" in url:
            return _MockResponse(
                {
                    "data": [
                        {
                            "forecastDate": today.isoformat(),
                            "tMin": "8.5",
                            "tMax": "18.4",
                            "idWeatherType": 3,
                            "precipitaProb": "10",
                        },
                        {
                            "forecastDate": (today + timedelta(days=1)).isoformat(),
                            "tMin": "10.0",
                            "tMax": "20.0",
                            "idWeatherType": 3,
                            "precipitaProb": "15",
                        },
                        {
                            "forecastDate": (today + timedelta(days=2)).isoformat(),
                            "tMin": "11.0",
                            "tMax": "21.0",
                            "idWeatherType": 3,
                            "precipitaProb": "20",
                        },
                        {
                            "forecastDate": (today + timedelta(days=3)).isoformat(),
                            "tMin": "9.0",
                            "tMax": "19.0",
                            "idWeatherType": 3,
                            "precipitaProb": "30",
                        },
                        {
                            "forecastDate": (today + timedelta(days=4)).isoformat(),
                            "tMin": "12.0",
                            "tMax": "22.0",
                            "idWeatherType": 3,
                            "precipitaProb": "35",
                        },
                    ]
                }
            )

        raise AssertionError(f"URL inesperado no teste: {url}")

    @patch("apps.dashboard.services.weather.requests.get")
    def test_weather_uses_city_first(self, requests_get_mock):
        requests_get_mock.side_effect = self._requests_get_side_effect

        result = weather_service.get_dashboard_weather_snapshot(
            city="Viseu",
            district="Lisboa",
        )

        self.assertEqual(result["state"], "success")
        self.assertEqual(result["location_label"], "Viseu")
        self.assertEqual(result["temperature_min"], "8.5")
        self.assertEqual(result["temperature_max"], "18.4")
        self.assertEqual(len(result["daily_forecast"]), 5)
        self.assertEqual(result["temperature_trend"]["key"], "rising")
        self.assertEqual(result["temperature_badge"]["key"], "mild")

    @patch("apps.dashboard.services.weather.requests.get")
    def test_weather_falls_back_to_district(self, requests_get_mock):
        requests_get_mock.side_effect = self._requests_get_side_effect

        result = weather_service.get_dashboard_weather_snapshot(
            city="",
            district="Viseu",
        )

        self.assertEqual(result["state"], "success")
        self.assertEqual(result["location_label"], "Viseu")

    def test_weather_degrades_without_location(self):
        result = weather_service.get_dashboard_weather_snapshot(city="", district="")

        self.assertEqual(result["state"], "degraded")
        self.assertIn("Sem localização definida", result["message"])

    @patch("apps.dashboard.services.weather.requests.get")
    def test_weather_uses_cache_after_first_fetch(self, requests_get_mock):
        requests_get_mock.side_effect = self._requests_get_side_effect

        first = weather_service.get_dashboard_weather_snapshot(city="Viseu", district="")
        second = weather_service.get_dashboard_weather_snapshot(city="Viseu", district="")

        self.assertEqual(first["state"], "success")
        self.assertEqual(second["state"], "success")
        self.assertEqual(requests_get_mock.call_count, 3)


class DashboardWeatherCardViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("apps.dashboard.views.MarketplaceListing")
    @patch("apps.dashboard.views.Order")
    @patch("apps.dashboard.views.render")
    @patch("apps.dashboard.views.get_dashboard_weather_snapshot")
    @patch("apps.dashboard.views.ProducerProfile")
    def test_weather_card_view_renders_partial(
        self,
        producer_profile_mock,
        weather_mock,
        render_mock,
        order_model_mock,
        listing_model_mock,
    ):
        producer_profile_mock.objects.only.return_value.get.return_value = SimpleNamespace(
            city="Viseu",
            district="Viseu",
        )
        weather_mock.return_value = {
            "state": "success",
            "location_context": "Viseu",
            "location_label": "Viseu",
            "temperature_min": "8.5",
            "temperature_max": "18.4",
            "weather_type_label": "Céu pouco nublado",
            "forecast_date": timezone.localdate().isoformat(),
            "daily_forecast": [],
            "temperature_trend": {"key": "stable", "label": "Estável", "delta": 0.0},
            "temperature_badge": {"key": "mild", "label": "Ameno"},
        }
        render_mock.return_value = HttpResponse("ok")

        orders_qs = MagicMock()
        orders_qs.distinct.return_value = orders_qs
        orders_qs.count.return_value = 2
        delivery_qs = MagicMock()
        delivery_qs.exists.return_value = True
        orders_qs.filter.return_value = delivery_qs
        order_model_mock.objects.filter.return_value = orders_qs

        presales_qs = MagicMock()
        presales_qs.count.return_value = 1
        listing_model_mock.objects.filter.return_value = presales_qs

        request = self.factory.get("/painel/weather-card/", HTTP_HX_REQUEST="true")
        request.current_user = SimpleNamespace(id="user-1")

        response = views.dashboard_weather_card_view.__wrapped__(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "ok")

        render_args = render_mock.call_args[0]
        self.assertEqual(render_args[1], "dashboard/partials/weather_card.html")
