from unittest.mock import patch
import uuid

from django.test import SimpleTestCase
from django.utils import timezone

from apps.accounts.models import AccountStatus, User, UserRole
from apps.catalog.models import Product
from apps.inventory.models import ProducerProfile
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.messaging.services import (
    MAX_MESSAGE_CHARS,
    MessagingServiceError,
    get_client_messages_badge_state,
    validate_listing_contact_allowed,
    validate_text_message_content,
)


class ClientMessagesBadgeStateTests(SimpleTestCase):
    @patch("apps.messaging.unread.get_unread_totals_for_user")
    def test_returns_visible_orange_badge_with_count(self, totals_mock):
        totals_mock.return_value = {
            "active_unread_total": 5,
            "archived_unread_total": 2,
        }

        state = get_client_messages_badge_state(user=object())
        self.assertEqual(state, {"visible": True, "count": 5, "tone": "orange"})

    @patch("apps.messaging.unread.get_unread_totals_for_user")
    def test_hides_badge_when_zero(self, totals_mock):
        totals_mock.return_value = {
            "active_unread_total": 0,
            "archived_unread_total": 7,
        }

        state = get_client_messages_badge_state(user=object())
        self.assertEqual(state, {"visible": False, "count": 0, "tone": "orange"})


class MessagingValidationTests(SimpleTestCase):
    def _user(self, email, *, active=True, account_status=AccountStatus.ACTIVE):
        return User(
            id=uuid.uuid4(),
            email=email,
            first_name="Teste",
            last_name="Produtor",
            role=UserRole.CLIENTE,
            is_active=active,
            account_status=account_status,
        )

    def _listing(self, *, seller_user=None, status=ListingStatus.ACTIVE, need_id=None, quantity_available=10):
        seller_user = seller_user or self._user("seller@example.com")
        producer = ProducerProfile(
            id=uuid.uuid4(),
            user=seller_user,
            display_name="Produtor Teste",
            member_since=timezone.now(),
        )
        product = Product(
            id=uuid.uuid4(),
            name="Alface",
            slug="alface",
            unit="kg",
            is_active=True,
        )
        return MarketplaceListing(
            id=uuid.uuid4(),
            producer=producer,
            product=product,
            status=status,
            need_id=need_id,
            quantity_available=quantity_available,
            published_at=timezone.now(),
        )

    def test_text_message_content_has_server_side_limit(self):
        with self.assertRaisesMessage(MessagingServiceError, "mais de 2000 caracteres"):
            validate_text_message_content("x" * (MAX_MESSAGE_CHARS + 1))

    def test_private_need_listing_cannot_start_marketplace_conversation(self):
        current_user = self._user("buyer@example.com")
        listing = self._listing(need_id=uuid.uuid4())

        with self.assertRaisesMessage(MessagingServiceError, "página de necessidades"):
            validate_listing_contact_allowed(current_user=current_user, listing=listing)

    def test_closed_listing_cannot_start_marketplace_conversation(self):
        current_user = self._user("buyer@example.com")
        listing = self._listing(status=ListingStatus.CLOSED)

        with self.assertRaisesMessage(MessagingServiceError, "já não está disponível"):
            validate_listing_contact_allowed(current_user=current_user, listing=listing)

    def test_inactive_seller_cannot_receive_marketplace_conversation(self):
        current_user = self._user("buyer@example.com")
        seller_user = self._user("seller@example.com", active=False)
        listing = self._listing(seller_user=seller_user)

        with self.assertRaisesMessage(MessagingServiceError, "produtor já não está disponível"):
            validate_listing_contact_allowed(current_user=current_user, listing=listing)
