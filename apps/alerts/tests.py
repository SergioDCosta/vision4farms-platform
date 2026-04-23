from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.accounts.models import UserRole
from apps.alerts.models import AlertType
from apps.alerts.services import get_alert_type_label, get_client_alerts_badge_state


class AlertLabelsTests(SimpleTestCase):
    def test_order_alert_types_have_human_labels(self):
        self.assertEqual(get_alert_type_label(AlertType.ORDER_PURCHASE_CREATED), "Nova compra recebida")
        self.assertEqual(get_alert_type_label(AlertType.ORDER_CONFIRMED), "Encomenda confirmada")
        self.assertEqual(get_alert_type_label(AlertType.ORDER_IN_PROGRESS), "Encomenda em preparação")
        self.assertEqual(get_alert_type_label(AlertType.ORDER_DELIVERING), "Encomenda em entrega")
        self.assertEqual(get_alert_type_label(AlertType.ORDER_CANCELLED), "Encomenda cancelada")
        self.assertEqual(get_alert_type_label(AlertType.ORDER_COMPLETED), "Receção confirmada")
        self.assertEqual(get_alert_type_label(AlertType.MESSAGE_UNREAD), "Nova mensagem")


class ClientAlertsBadgeStateTests(SimpleTestCase):
    @patch("apps.alerts.services.Alert")
    @patch("apps.alerts.services.ProducerProfile")
    def test_returns_red_when_has_unseen_active_alerts(self, producer_model_mock, alert_model_mock):
        now = timezone.now()
        request = SimpleNamespace(
            current_user=SimpleNamespace(id="user-1", role=UserRole.CLIENTE),
            session={},
        )

        producer = SimpleNamespace(id="producer-1")
        producer_qs = MagicMock()
        producer_qs.only.return_value.first.return_value = producer
        producer_model_mock.objects.filter.return_value = producer_qs

        alert_qs = MagicMock()
        alert_qs.aggregate.return_value = {
            "open_count": 4,
            "latest_active_created_at": now,
        }
        alert_model_mock.objects.filter.return_value = alert_qs

        state = get_client_alerts_badge_state(request)
        self.assertEqual(state, {"visible": True, "count": 4, "tone": "red"})

    @patch("apps.alerts.services.Alert")
    @patch("apps.alerts.services.ProducerProfile")
    def test_returns_orange_when_alerts_are_seen(self, producer_model_mock, alert_model_mock):
        now = timezone.now()
        request = SimpleNamespace(
            current_user=SimpleNamespace(id="user-2", role=UserRole.CLIENTE),
            session={"alerts_last_seen_at": now.isoformat()},
        )

        producer = SimpleNamespace(id="producer-2")
        producer_qs = MagicMock()
        producer_qs.only.return_value.first.return_value = producer
        producer_model_mock.objects.filter.return_value = producer_qs

        alert_qs = MagicMock()
        alert_qs.aggregate.return_value = {
            "open_count": 2,
            "latest_active_created_at": now,
        }
        alert_model_mock.objects.filter.return_value = alert_qs

        state = get_client_alerts_badge_state(request)
        self.assertEqual(state, {"visible": True, "count": 2, "tone": "orange"})

    def test_returns_hidden_for_non_client(self):
        request = SimpleNamespace(
            current_user=SimpleNamespace(id="admin-1", role=UserRole.ADMIN),
            session={},
        )

        state = get_client_alerts_badge_state(request)
        self.assertEqual(state, {"visible": False, "count": 0, "tone": "orange"})
