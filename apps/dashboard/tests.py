from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import caches
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import AccountStatus, RegistrationSource, UserRole
from apps.dashboard import views
from apps.dashboard.services import admin_audit, admin_users
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

    @patch("apps.dashboard.views.render")
    @patch("apps.dashboard.views.build_weather_card_context")
    def test_weather_card_view_renders_partial(
        self,
        weather_context_mock,
        render_mock,
    ):
        weather_context_mock.return_value = {
            "weather": {
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
            },
            "weather_state": "success",
            "active_delivery_orders_count": 2,
            "presale_starting_soon_count": 1,
            "weather_operational_hints": [],
            "weather_actions": [],
        }
        render_mock.return_value = HttpResponse("ok")

        request = self.factory.get("/painel/weather-card/", HTTP_HX_REQUEST="true")
        request.current_user = SimpleNamespace(id="user-1")

        response = views.dashboard_weather_card_view.__wrapped__(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "ok")

        render_args = render_mock.call_args[0]
        self.assertEqual(render_args[1], "dashboard/partials/weather_card.html")
        weather_context_mock.assert_called_once_with(request.current_user)


class _DummyAtomic:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class AdminUserServiceTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("apps.dashboard.services.admin_users.send_admin_invite_email")
    @patch("apps.dashboard.services.admin_users.transaction.on_commit")
    @patch("apps.dashboard.services.admin_users.transaction.atomic", return_value=_DummyAtomic())
    @patch("apps.dashboard.services.admin_users.log_admin_action")
    @patch("apps.dashboard.services.admin_users.create_admin_invite_token")
    @patch("apps.dashboard.services.admin_users.User.objects.create")
    def test_create_invited_user_schedules_email_after_commit(
        self,
        create_user_mock,
        create_token_mock,
        log_mock,
        atomic_mock,
        on_commit_mock,
        send_email_mock,
    ):
        user = SimpleNamespace(
            id="user-1",
            email="novo@example.com",
            first_name="",
            last_name="",
            role=UserRole.CLIENTE,
            registration_source=RegistrationSource.ADMIN_CREATED,
            account_status=AccountStatus.PENDING_EMAIL_CONFIRMATION,
            email_verified_at=None,
            is_active=False,
            is_staff=False,
        )
        token = SimpleNamespace(id="token-1")
        create_user_mock.return_value = user
        create_token_mock.return_value = token
        callbacks = []
        on_commit_mock.side_effect = callbacks.append

        request = self.factory.post("/gestor/utilizadores/novo/")
        request.current_user = SimpleNamespace(id="admin-1")
        form = SimpleNamespace(
            cleaned_data={"email": "novo@example.com", "role": UserRole.CLIENTE}
        )

        result = admin_users.create_invited_user_from_admin_form(
            request=request,
            form=form,
        )

        self.assertIs(result, user)
        create_user_mock.assert_called_once()
        create_token_mock.assert_called_once_with(user)
        log_mock.assert_called_once()
        on_commit_mock.assert_called_once()
        send_email_mock.assert_not_called()

        callbacks[0]()
        send_email_mock.assert_called_once_with(request, user, token)


class AdminAuditPresenterTests(SimpleTestCase):
    def test_build_user_activity_rows_maps_labels_and_device(self):
        log = SimpleNamespace(
            action="USER_LOGIN",
            old_values={"remember_me": True},
            new_values={"remember_me": False},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/150.0",
            user=SimpleNamespace(first_name="Ana", last_name="Silva", email="ana@example.com"),
        )

        rows = admin_audit.build_user_activity_rows([log])

        self.assertEqual(rows[0]["action_label"], "Iniciou sessão")
        self.assertEqual(rows[0]["actor_label"], "Ana Silva")
        self.assertEqual(rows[0]["changes"][0]["label"], "Sessão persistente")
        self.assertIn("Firefox", rows[0]["device_label"])
