from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.accounts.models import UserRole
from apps.alerts.models import AlertStatus, AlertType
from apps.alerts.services import (
    get_alert_type_label,
    get_client_alerts_badge_state,
    list_alerts_for_producer,
    resolve_alert,
)


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


class ResolveAlertSemanticsTests(SimpleTestCase):
    databases = {"default"}

    @patch("apps.alerts.services._queue_alerts_badge_changed_for_user")
    @patch("apps.alerts.services.record_alert_event")
    @patch("apps.alerts.services.timezone")
    def test_managed_alert_keeps_cleared_at_null(self, timezone_mock, record_event_mock, queue_mock):
        now = timezone.now()
        timezone_mock.now.return_value = now
        alert = SimpleNamespace(
            status=AlertStatus.ACTIVE,
            type=AlertType.CRITICAL_STOCK,
            cleared_at=now,
            updated_at=None,
            save=MagicMock(),
        )
        user = SimpleNamespace(id="user-1")

        changed = resolve_alert(alert, user=user)

        self.assertTrue(changed)
        self.assertEqual(alert.status, AlertStatus.RESOLVED)
        self.assertIsNone(alert.cleared_at)
        self.assertEqual(alert.updated_at, now)
        alert.save.assert_called_once_with(update_fields=["status", "cleared_at", "updated_at"])
        record_event_mock.assert_called_once()
        queue_mock.assert_called_once_with(user_id="user-1")

    @patch("apps.alerts.services._queue_alerts_badge_changed_for_user")
    @patch("apps.alerts.services.record_alert_event")
    @patch("apps.alerts.services.timezone")
    def test_non_managed_alert_sets_cleared_at_now(self, timezone_mock, record_event_mock, queue_mock):
        now = timezone.now()
        timezone_mock.now.return_value = now
        alert = SimpleNamespace(
            status=AlertStatus.ACTIVE,
            type=AlertType.ORDER_CONFIRMED,
            cleared_at=None,
            updated_at=None,
            save=MagicMock(),
        )
        user = SimpleNamespace(id="user-2")

        changed = resolve_alert(alert, user=user)

        self.assertTrue(changed)
        self.assertEqual(alert.status, AlertStatus.RESOLVED)
        self.assertEqual(alert.cleared_at, now)
        self.assertEqual(alert.updated_at, now)
        alert.save.assert_called_once_with(update_fields=["status", "cleared_at", "updated_at"])
        record_event_mock.assert_called_once()
        queue_mock.assert_called_once_with(user_id="user-2")

    @patch("apps.alerts.services._queue_alerts_badge_changed_for_user")
    @patch("apps.alerts.services.record_alert_event")
    def test_already_resolved_returns_false_without_side_effects(self, record_event_mock, queue_mock):
        alert = SimpleNamespace(
            status=AlertStatus.RESOLVED,
            type=AlertType.CRITICAL_STOCK,
            cleared_at=None,
            updated_at=None,
            save=MagicMock(),
        )

        changed = resolve_alert(alert, user=SimpleNamespace(id="user-3"))

        self.assertFalse(changed)
        alert.save.assert_not_called()
        record_event_mock.assert_not_called()
        queue_mock.assert_not_called()


class AlertActionsFallbackTests(SimpleTestCase):
    @patch("apps.alerts.services.Alert")
    def test_order_alert_fallback_actions(self, alert_model_mock):
        alert = SimpleNamespace(
            type=AlertType.ORDER_CONFIRMED,
            severity="INFO",
            payload={"action_url": "/encomendas/1/", "order_id": "ord-1"},
            product=None,
        )
        alert_model_mock.objects.select_related.return_value.filter.return_value.order_by.return_value = [alert]

        alerts = list_alerts_for_producer(producer=SimpleNamespace(id="p1"), tab="active")

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alert.action_label, "Ir para encomenda")
        self.assertEqual(alert.secondary_action_url, "/mensagens/encomenda/ord-1/iniciar/")
        self.assertEqual(alert.secondary_action_label, "Ir para conversa")

    @patch("apps.alerts.services.Alert")
    def test_message_alert_fallback_primary_action_label(self, alert_model_mock):
        alert = SimpleNamespace(
            type=AlertType.MESSAGE_UNREAD,
            severity="INFO",
            payload={"action_url": "/mensagens/?tab=active&c=conv-1"},
            product=None,
        )
        alert_model_mock.objects.select_related.return_value.filter.return_value.order_by.return_value = [alert]

        alerts = list_alerts_for_producer(producer=SimpleNamespace(id="p1"), tab="active")

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alert.action_label, "Ir para conversa")
