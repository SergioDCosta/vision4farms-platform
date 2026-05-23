from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.accounts.models import UserRole
from apps.alerts.models import AlertStatus, AlertType
from apps.alerts.services import (
    get_alert_type_label,
    get_client_alerts_badge_state,
    ignore_alert,
    ignore_all_active_alerts,
    list_alerts_for_producer,
    normalize_alert_type,
    resolve_alert,
    run_operational_alerts_job,
    _critical_stock_candidates,
    _quantity_label,
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


class AlertQuantityLabelsTests(SimpleTestCase):
    def test_quantity_labels_do_not_show_unneeded_decimal_places(self):
        self.assertEqual(_quantity_label(Decimal("200.000"), "kg"), "200 kg")


class AlertTemporalStockCandidatesTests(SimpleTestCase):
    @patch("apps.alerts.services.calculate_inventory_commitment_state")
    @patch("apps.alerts.services.Stock")
    def test_no_critical_alert_when_forecast_covers_external_demands(self, stock_model_mock, commitment_mock):
        product = SimpleNamespace(id="product-1", name="Batata", unit="kg")
        stock = SimpleNamespace(product=product, product_id="product-1")
        stock_model_mock.objects.select_related.return_value.filter.return_value.distinct.return_value = [stock]
        commitment_mock.return_value = {
            "max_deficit": Decimal("0.000"),
            "available_stock_now": Decimal("500.000"),
            "useful_forecast_total": Decimal("300.000"),
            "total_external_demand": Decimal("625.000"),
            "first_deficit_date": None,
        }

        rows = _critical_stock_candidates(SimpleNamespace(id="producer-1"))

        self.assertEqual(rows, [])

    @patch("apps.alerts.services.calculate_inventory_commitment_state")
    @patch("apps.alerts.services.Stock")
    def test_critical_alert_uses_temporal_deficit(self, stock_model_mock, commitment_mock):
        product = SimpleNamespace(id="product-1", name="Batata", unit="kg")
        stock = SimpleNamespace(product=product, product_id="product-1")
        stock_model_mock.objects.select_related.return_value.filter.return_value.distinct.return_value = [stock]
        commitment_mock.return_value = {
            "max_deficit": Decimal("125.000"),
            "available_stock_now": Decimal("500.000"),
            "useful_forecast_total": Decimal("0.000"),
            "total_external_demand": Decimal("625.000"),
            "first_deficit_date": timezone.localdate(),
        }

        rows = _critical_stock_candidates(SimpleNamespace(id="producer-1"))

        self.assertEqual(len(rows), 1)
        self.assertIn("Faltam 125 kg", rows[0]["description"])


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

    @patch("apps.alerts.services._queue_alerts_badge_changed_for_user")
    @patch("apps.alerts.services.record_alert_event")
    def test_ignored_alert_cannot_be_resolved_manually(self, record_event_mock, queue_mock):
        alert = SimpleNamespace(
            status=AlertStatus.IGNORED,
            type=AlertType.CRITICAL_STOCK,
            cleared_at=None,
            updated_at=None,
            save=MagicMock(),
        )

        changed = resolve_alert(alert, user=SimpleNamespace(id="user-4"))

        self.assertFalse(changed)
        alert.save.assert_not_called()
        record_event_mock.assert_not_called()
        queue_mock.assert_not_called()

    @patch("apps.alerts.services._queue_alerts_badge_changed_for_user")
    @patch("apps.alerts.services.record_alert_event")
    def test_cleared_alert_cannot_be_resolved_manually(self, record_event_mock, queue_mock):
        alert = SimpleNamespace(
            status=AlertStatus.CLEARED,
            type=AlertType.CRITICAL_STOCK,
            cleared_at=None,
            updated_at=None,
            save=MagicMock(),
        )

        changed = resolve_alert(alert, user=SimpleNamespace(id="user-6"))

        self.assertFalse(changed)
        alert.save.assert_not_called()
        record_event_mock.assert_not_called()
        queue_mock.assert_not_called()


class IgnoreAlertSemanticsTests(SimpleTestCase):
    databases = {"default"}

    @patch("apps.alerts.services._queue_alerts_badge_changed_for_user")
    @patch("apps.alerts.services.record_alert_event")
    def test_resolved_alert_cannot_be_ignored_manually(self, record_event_mock, queue_mock):
        alert = SimpleNamespace(
            status=AlertStatus.RESOLVED,
            type=AlertType.CRITICAL_STOCK,
            ignored_at=None,
            ignored_reason=None,
            cleared_at=None,
            updated_at=None,
            save=MagicMock(),
        )

        changed = ignore_alert(alert, user=SimpleNamespace(id="user-5"))

        self.assertFalse(changed)
        alert.save.assert_not_called()
        record_event_mock.assert_not_called()
        queue_mock.assert_not_called()

    @patch("apps.alerts.services._queue_alerts_badge_changed_for_user")
    @patch("apps.alerts.services.record_alert_event")
    def test_cleared_alert_cannot_be_ignored_manually(self, record_event_mock, queue_mock):
        alert = SimpleNamespace(
            status=AlertStatus.CLEARED,
            type=AlertType.CRITICAL_STOCK,
            ignored_at=None,
            ignored_reason=None,
            cleared_at=None,
            updated_at=None,
            save=MagicMock(),
        )

        changed = ignore_alert(alert, user=SimpleNamespace(id="user-7"))

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


class AlertFilterTests(SimpleTestCase):
    def test_normalize_alert_type_returns_empty_for_invalid_value(self):
        self.assertEqual(normalize_alert_type("NOT_A_TYPE"), "")
        self.assertEqual(normalize_alert_type(""), "")
        self.assertEqual(normalize_alert_type(AlertType.CRITICAL_STOCK), AlertType.CRITICAL_STOCK)

    @patch("apps.alerts.services.Alert")
    def test_list_alerts_for_producer_filters_by_valid_type(self, alert_model_mock):
        alert = SimpleNamespace(
            type=AlertType.CRITICAL_STOCK,
            severity="CRITICAL",
            payload={},
            product=None,
        )
        base_qs = MagicMock()
        filtered_qs = MagicMock()
        alert_model_mock.objects.select_related.return_value.filter.return_value = base_qs
        base_qs.filter.return_value = filtered_qs
        filtered_qs.order_by.return_value = [alert]

        alerts = list_alerts_for_producer(
            producer=SimpleNamespace(id="p1"),
            tab="active",
            alert_type=AlertType.CRITICAL_STOCK,
        )

        base_qs.filter.assert_called_once_with(type=AlertType.CRITICAL_STOCK)
        self.assertEqual(alerts, [alert])

    @patch("apps.alerts.services.Alert")
    def test_list_alerts_for_producer_ignores_invalid_type(self, alert_model_mock):
        alert = SimpleNamespace(
            type=AlertType.ORDER_CONFIRMED,
            severity="INFO",
            payload={},
            product=None,
        )
        base_qs = MagicMock()
        alert_model_mock.objects.select_related.return_value.filter.return_value = base_qs
        base_qs.order_by.return_value = [alert]

        alerts = list_alerts_for_producer(
            producer=SimpleNamespace(id="p1"),
            tab="active",
            alert_type="NOT_A_TYPE",
        )

        base_qs.filter.assert_not_called()
        self.assertEqual(alerts, [alert])


class IgnoreAllAlertFilterTests(SimpleTestCase):
    databases = {"default"}

    @patch("apps.alerts.services._queue_alerts_badge_changed_for_user")
    @patch("apps.alerts.services.ignore_alert")
    @patch("apps.alerts.services.Alert")
    def test_ignore_all_active_alerts_filters_by_type(self, alert_model_mock, ignore_alert_mock, queue_mock):
        user = SimpleNamespace(id="user-1")
        alert = SimpleNamespace(id="alert-1")
        base_qs = MagicMock()
        filtered_qs = MagicMock()
        alert_model_mock.objects.select_for_update.return_value.filter.return_value = base_qs
        base_qs.filter.return_value = filtered_qs
        filtered_qs.order_by.return_value = [alert]
        ignore_alert_mock.return_value = True

        count = ignore_all_active_alerts(
            producer=SimpleNamespace(id="p1"),
            user=user,
            alert_type=AlertType.ORDER_CONFIRMED,
        )

        base_qs.filter.assert_called_once_with(type=AlertType.ORDER_CONFIRMED)
        ignore_alert_mock.assert_called_once_with(
            alert,
            user=user,
            reason=None,
            snooze_key="1h",
            queue_badge_update=False,
        )
        queue_mock.assert_called_once_with(user_id="user-1")
        self.assertEqual(count, 1)


class OperationalAlertsJobTests(SimpleTestCase):
    @patch("apps.alerts.services.ProducerProfile")
    def test_job_dry_run_does_not_apply_changes(self, producer_model_mock):
        producer_qs = MagicMock()
        producer_qs.order_by.return_value = [SimpleNamespace(id="producer-1")]
        producer_model_mock.objects.select_related.return_value.filter.return_value = producer_qs

        summary = run_operational_alerts_job(apply=False)

        self.assertEqual(summary["mode"], "dry-run")
        self.assertEqual(summary["producers_seen"], 1)
        self.assertEqual(summary["producers_synced"], 0)

    @patch("apps.alerts.services.sync_alerts_for_producer")
    @patch("apps.alerts.services.expire_due_alerts", return_value=1)
    @patch("apps.alerts.services.expire_ignored_alerts_for_producer", return_value=2)
    @patch("apps.marketplace.services.expire_due_active_listings", return_value=3)
    @patch("apps.alerts.services.ProducerProfile")
    def test_job_apply_runs_periodic_tasks(
        self,
        producer_model_mock,
        expire_listings_mock,
        expire_ignored_mock,
        expire_alerts_mock,
        sync_mock,
    ):
        producer = SimpleNamespace(id="producer-1")
        producer_qs = MagicMock()
        producer_qs.order_by.return_value = [producer]
        producer_model_mock.objects.select_related.return_value.filter.return_value = producer_qs
        sync_mock.return_value = {"created": 4, "updated": 5, "resolved": 6, "cleared": 7}

        summary = run_operational_alerts_job(apply=True)

        self.assertEqual(summary["mode"], "apply")
        self.assertEqual(summary["listings_expired"], 3)
        self.assertEqual(summary["ignored_expired"], 2)
        self.assertEqual(summary["alerts_expired"], 1)
        self.assertEqual(summary["created"], 4)
        self.assertEqual(summary["updated"], 5)
        self.assertEqual(summary["resolved"], 6)
        self.assertEqual(summary["cleared"], 7)
        self.assertEqual(summary["producers_synced"], 1)
        expire_listings_mock.assert_called_once()
        expire_ignored_mock.assert_called_once_with(producer=producer, acting_user=None)
        expire_alerts_mock.assert_called_once_with(producer=producer, acting_user=None)
        sync_mock.assert_called_once_with(producer, acting_user=None)
