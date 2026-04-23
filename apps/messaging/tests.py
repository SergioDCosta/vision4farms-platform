from unittest.mock import patch

from django.test import SimpleTestCase

from apps.messaging.services import get_client_messages_badge_state


class ClientMessagesBadgeStateTests(SimpleTestCase):
    @patch("apps.messaging.services.get_unread_totals_for_user")
    def test_returns_visible_orange_badge_with_count(self, totals_mock):
        totals_mock.return_value = {
            "active_unread_total": 5,
            "archived_unread_total": 2,
        }

        state = get_client_messages_badge_state(user=object())
        self.assertEqual(state, {"visible": True, "count": 5, "tone": "orange"})

    @patch("apps.messaging.services.get_unread_totals_for_user")
    def test_hides_badge_when_zero(self, totals_mock):
        totals_mock.return_value = {
            "active_unread_total": 0,
            "archived_unread_total": 7,
        }

        state = get_client_messages_badge_state(user=object())
        self.assertEqual(state, {"visible": False, "count": 0, "tone": "orange"})
