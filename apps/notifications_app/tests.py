from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.notifications_app.models import NotificationType
from apps.notifications_app.services import (
    clear_recent_notifications_for_user,
    create_alert_notification,
    list_recent_notifications_for_user,
)


class AlertNotificationServiceTests(SimpleTestCase):
    @patch("apps.notifications_app.services.timezone")
    @patch("apps.notifications_app.services.transaction.atomic", return_value=nullcontext())
    @patch("apps.notifications_app.services.Notification")
    def test_create_alert_notification_updates_existing_notification(self, notification_model, atomic_mock, timezone_mock):
        now = timezone.now()
        timezone_mock.now.return_value = now
        existing = MagicMock(id="keep")
        duplicate = MagicMock(id="duplicate")
        lock_qs = MagicMock()
        lock_qs.filter.return_value.order_by.return_value = [existing, duplicate]
        delete_qs = MagicMock()
        notification_model.objects.select_for_update.return_value = lock_qs
        notification_model.objects.filter.return_value = delete_qs
        alert = SimpleNamespace(
            title="Oportunidade para cobrir Alface",
            description="Existem 200.000 kg disponíveis.",
            payload={"action_url": "/recomendacoes/?product=1"},
        )
        user = SimpleNamespace(id="user-1")

        notification = create_alert_notification(user=user, alert=alert)

        self.assertEqual(notification, existing)
        delete_qs.delete.assert_called_once()
        self.assertEqual(existing.title, "Oportunidade para cobrir Alface")
        self.assertEqual(existing.body, "Existem 200 kg disponíveis.")
        self.assertEqual(existing.action_url, "/recomendacoes/?product=1")
        self.assertFalse(existing.is_read)
        self.assertIsNone(existing.read_at)
        self.assertEqual(existing.created_at, now)
        existing.save.assert_called_once_with(
            update_fields=["title", "body", "action_url", "is_read", "read_at", "created_at"]
        )

    @patch("apps.notifications_app.services.transaction.atomic", return_value=nullcontext())
    @patch("apps.notifications_app.services.create_notification")
    @patch("apps.notifications_app.services.Notification")
    def test_create_alert_notification_creates_when_missing(self, notification_model, create_notification_mock, atomic_mock):
        lock_qs = MagicMock()
        lock_qs.filter.return_value.order_by.return_value = []
        notification_model.objects.select_for_update.return_value = lock_qs
        create_notification_mock.return_value = "created"
        alert = SimpleNamespace(
            title="Alerta",
            description="Descrição",
            payload={"action_url": "/alertas/"},
        )
        user = SimpleNamespace(id="user-1")

        notification = create_alert_notification(user=user, alert=alert)

        self.assertEqual(notification, "created")
        create_notification_mock.assert_called_once_with(
            user=user,
            notification_type=NotificationType.ALERT,
            title="Alerta",
            body="Descrição",
            action_url="/alertas/",
            alert=alert,
        )

    @patch("apps.notifications_app.services.transaction.atomic", return_value=nullcontext())
    @patch("apps.notifications_app.services.Notification")
    def test_clear_recent_notifications_deletes_only_user_notifications(self, notification_model, atomic_mock):
        qs = MagicMock()
        qs.delete.return_value = (4, {"notifications_app.Notification": 4})
        notification_model.objects.filter.return_value = qs
        user = SimpleNamespace(id="user-1")

        deleted_count = clear_recent_notifications_for_user(user=user)

        self.assertEqual(deleted_count, 4)
        notification_model.objects.filter.assert_called_once_with(user=user)

    @patch("apps.notifications_app.services.Notification")
    def test_recent_alert_notifications_only_include_active_or_read_alerts(self, notification_model):
        user = SimpleNamespace(id="user-1")
        user_qs = MagicMock()
        visible_qs = MagicMock()
        ordered_qs = MagicMock()
        notification_model.objects.select_related.return_value.filter.return_value = user_qs
        user_qs.filter.return_value = visible_qs
        visible_qs.order_by.return_value = ordered_qs
        ordered_qs.__getitem__.return_value = []

        notifications = list_recent_notifications_for_user(user=user)

        self.assertEqual(notifications, [])
        notification_model.objects.select_related.return_value.filter.assert_called_once_with(user=user)
        visibility_condition = str(user_qs.filter.call_args.args[0])
        self.assertIn("alert__status__in", visibility_condition)
        self.assertIn("ACTIVE", visibility_condition)
        self.assertIn("READ", visibility_condition)
