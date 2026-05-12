from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.accounts.models import AccountStatus
from apps.common.session import resolve_active_session_user
from apps.notifications_app.services import _normalize_quantity_text


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
