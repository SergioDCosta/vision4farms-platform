import json
from types import SimpleNamespace
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from apps.accounts.models import AccountStatus, UserRole
from apps.common.audit import log_audit_event
from apps.common.context_processors import (
    admin_support_sidebar_badge,
    client_alerts_sidebar_badge,
)
from apps.common.decorators import admin_required, client_only_required, login_required
from apps.common.formatting import format_quantity
from apps.common.htmx import with_htmx_toast, with_htmx_trigger
from apps.common.session import resolve_active_session_user
from apps.common.templatetags.quantity import quantity
from apps.notifications_app.services import _normalize_quantity_text


class QuantityFormattingTests(SimpleTestCase):
    def test_format_quantity_removes_unnecessary_decimal_places(self):
        self.assertEqual(format_quantity("20.000"), "20")
        self.assertEqual(format_quantity("20.500"), "20,5")
        self.assertEqual(format_quantity("20.550"), "20,55")
        self.assertEqual(format_quantity("20.345"), "20,345")

    def test_quantity_template_filter_uses_common_formatter(self):
        self.assertEqual(quantity("20.500"), "20,5")


class NotificationQuantityTextTests(SimpleTestCase):
    def test_normalizes_quantity_values_before_kg(self):
        text = "Existem 500.000 kg disponíveis e 20.500 kg por receber."

        normalized = _normalize_quantity_text(text)

        self.assertEqual(normalized, "Existem 500 kg disponíveis e 20,5 kg por receber.")


class ResolveActiveSessionUserTests(SimpleTestCase):
    @patch("apps.common.session.is_valid_session_auth_fingerprint", return_value=True)
    @patch("apps.common.session.User")
    def test_valid_session_returns_active_user(self, user_model_mock, fingerprint_mock):
        user = SimpleNamespace(is_active=True, account_status=AccountStatus.ACTIVE)
        user_model_mock.objects.filter.return_value.first.return_value = user

        resolved_user = resolve_active_session_user(
            {"user_id": "user-1", "session_auth_fingerprint": "fingerprint"}
        )

        self.assertIs(resolved_user, user)
        user_model_mock.objects.filter.assert_called_once_with(id="user-1")
        fingerprint_mock.assert_called_once_with(user, "fingerprint")

    @patch("apps.common.session.is_valid_session_auth_fingerprint", return_value=False)
    @patch("apps.common.session.User")
    def test_invalid_fingerprint_rejects_session(self, user_model_mock, fingerprint_mock):
        user = SimpleNamespace(is_active=True, account_status=AccountStatus.ACTIVE)
        user_model_mock.objects.filter.return_value.first.return_value = user

        resolved_user = resolve_active_session_user(
            {"user_id": "user-1", "session_auth_fingerprint": "old-fingerprint"}
        )

        self.assertIsNone(resolved_user)
        fingerprint_mock.assert_called_once_with(user, "old-fingerprint")

    @patch("apps.common.session.is_valid_session_auth_fingerprint")
    @patch("apps.common.session.User")
    def test_inactive_or_suspended_user_rejects_session(self, user_model_mock, fingerprint_mock):
        inactive_user = SimpleNamespace(is_active=False, account_status=AccountStatus.ACTIVE)
        suspended_user = SimpleNamespace(is_active=True, account_status=AccountStatus.SUSPENDED)

        for user in [inactive_user, suspended_user]:
            with self.subTest(user=user):
                user_model_mock.objects.filter.return_value.first.return_value = user

                resolved_user = resolve_active_session_user(
                    {"user_id": "user-1", "session_auth_fingerprint": "fingerprint"}
                )

                self.assertIsNone(resolved_user)

        fingerprint_mock.assert_not_called()


class CommonDecoratorTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, user=None):
        request = self.factory.get("/private/")
        request.session = {}
        request.current_user = user
        return request

    def _active_user(self, role):
        return SimpleNamespace(is_active=True, account_status=AccountStatus.ACTIVE, role=role)

    def test_login_required_redirects_anonymous_user(self):
        @login_required
        def view(request):
            return HttpResponse("ok")

        response = view(self._request())

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)

    def test_admin_required_redirects_client_user(self):
        @admin_required
        def view(request):
            return HttpResponse("ok")

        response = view(self._request(self._active_user(UserRole.CLIENTE)))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/painel", response.url)

    def test_client_only_required_redirects_admin_user(self):
        @client_only_required
        def view(request):
            return HttpResponse("ok")

        response = view(self._request(self._active_user(UserRole.ADMIN)))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/gestor", response.url)


class HtmxTests(SimpleTestCase):
    def test_with_htmx_trigger_preserves_existing_trigger_payload(self):
        response = HttpResponse("ok")

        with_htmx_trigger(response, "app:first", {"value": 1})
        with_htmx_toast(response, "success", "Guardado.")

        payload = json.loads(response["HX-Trigger"])
        self.assertEqual(payload["app:first"], {"value": 1})
        self.assertEqual(payload["app:toast"], {"level": "success", "message": "Guardado."})

    def test_with_htmx_trigger_preserves_plain_existing_trigger(self):
        response = HttpResponse("ok")
        response["HX-Trigger"] = "app:refresh"

        with_htmx_toast(response, "info", "Atualizado.")

        payload = json.loads(response["HX-Trigger"])
        self.assertEqual(payload["app:refresh"], {})
        self.assertEqual(payload["app:toast"], {"level": "info", "message": "Atualizado."})


class CommonContextProcessorTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("apps.common.context_processors.get_client_alerts_badge_state")
    def test_client_alerts_badge_does_not_call_service_for_admin(self, badge_mock):
        request = self.factory.get("/")
        request.current_user = SimpleNamespace(role=UserRole.ADMIN)

        result = client_alerts_sidebar_badge(request)

        self.assertEqual(result["client_alerts_badge"], {"visible": False, "count": 0, "tone": "orange"})
        badge_mock.assert_not_called()

    @patch("apps.common.context_processors.get_admin_support_badge_state")
    def test_admin_support_badge_is_cached_per_request(self, badge_mock):
        request = self.factory.get("/")
        request.current_user = SimpleNamespace(role=UserRole.ADMIN)
        badge_mock.return_value = {"visible": True, "count": 2, "tone": "orange"}

        first = admin_support_sidebar_badge(request)
        second = admin_support_sidebar_badge(request)

        self.assertEqual(first, second)
        badge_mock.assert_called_once_with(request)


class AuditTests(SimpleTestCase):
    @patch("apps.dashboard.models.AuditLog.objects.create")
    def test_log_audit_event_uses_request_metadata(self, create_mock):
        request = RequestFactory().get(
            "/",
            HTTP_X_FORWARDED_FOR="203.0.113.1, 10.0.0.1",
            HTTP_USER_AGENT="Mozilla/5.0",
        )
        request.current_user = SimpleNamespace(id="user-1")

        log_audit_event(request=request, action="TEST_ACTION", entity_type="tests")

        create_mock.assert_called_once()
        kwargs = create_mock.call_args.kwargs
        self.assertEqual(kwargs["ip_address"], "203.0.113.1")
        self.assertEqual(kwargs["user_agent"], "Mozilla/5.0")

    @patch("apps.common.audit.logger.exception")
    @patch("apps.dashboard.models.AuditLog.objects.create", side_effect=RuntimeError("db down"))
    def test_log_audit_event_does_not_break_request_when_create_fails(self, create_mock, logger_mock):
        result = log_audit_event(action="TEST_ACTION")

        self.assertIsNone(result)
        create_mock.assert_called_once()
        logger_mock.assert_called_once()
