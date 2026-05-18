from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.template.loader import get_template
from django.test import SimpleTestCase, override_settings

from apps.settings_app.forms import ProducerProfileSettingsForm, ProfilePhotoForm
from apps.settings_app.services import (
    avatar_initials,
    ensure_user_preference,
    get_support_tickets_context,
    profile_photo_url,
    update_account_profile,
)


class SettingsTemplateTests(SimpleTestCase):
    def test_settings_templates_load(self):
        template_names = [
            "settings/settings_panel.html",
            "settings/partials/account.html",
            "settings/partials/photo.html",
            "settings/partials/notifications.html",
            "settings/partials/producer_profile.html",
            "settings/partials/location.html",
            "settings/partials/support.html",
            "settings/partials/security.html",
            "settings/partials/location_script.html",
        ]

        for template_name in template_names:
            with self.subTest(template_name=template_name):
                self.assertIsNotNone(get_template(template_name))


class SettingsServiceTests(SimpleTestCase):
    @patch("apps.settings_app.services.UserPreference.objects")
    def test_ensure_user_preference_reuses_existing_preference(self, objects_mock):
        preference = SimpleNamespace(id="pref-1")
        user = SimpleNamespace(id="user-1")
        objects_mock.filter.return_value.first.return_value = preference

        self.assertIs(ensure_user_preference(user), preference)
        objects_mock.create.assert_not_called()

    @patch("apps.settings_app.services.UserPreference.objects")
    def test_ensure_user_preference_creates_default_preference(self, objects_mock):
        user = SimpleNamespace(id="user-1")
        created = SimpleNamespace(id="pref-1")
        objects_mock.filter.return_value.first.return_value = None
        objects_mock.create.return_value = created

        self.assertIs(ensure_user_preference(user), created)
        self.assertTrue(objects_mock.create.call_args.kwargs["alerts_in_app"])
        self.assertFalse(objects_mock.create.call_args.kwargs["alerts_email"])
        self.assertFalse(objects_mock.create.call_args.kwargs["alerts_sms"])

    def test_avatar_initials_use_first_and_last_name(self):
        user = SimpleNamespace(first_name="Sergio", last_name="Costa")

        self.assertEqual(avatar_initials(user), "SC")

    @override_settings(MEDIA_URL="/media/")
    @patch("apps.common.media.default_storage.url", return_value="/media/profile.jpg")
    def test_profile_photo_url_adds_cache_buster(self, storage_url_mock):
        preference = SimpleNamespace(
            profile_photo="profile.jpg",
            updated_at=SimpleNamespace(timestamp=Mock(return_value=123)),
        )

        self.assertEqual(profile_photo_url(preference), "/media/profile.jpg?v=123")

    @patch("apps.settings_app.services.SupportTicket")
    def test_support_ticket_context_limits_to_three_by_default(self, ticket_model_mock):
        tickets = [SimpleNamespace(id=index) for index in range(4)]
        qs = ticket_model_mock.objects.filter.return_value.select_related.return_value.order_by.return_value
        qs.count.return_value = 4
        qs.__getitem__.return_value = tickets[:3]

        context = get_support_tickets_context(SimpleNamespace(id="user-1"))

        self.assertEqual(context["support_tickets"], tickets[:3])
        self.assertTrue(context["support_tickets_has_more"])
        self.assertFalse(context["support_tickets_show_all"])

    @patch("apps.settings_app.services.log_audit_event")
    @patch("apps.settings_app.services.ProducerProfile")
    def test_update_account_profile_syncs_producer_display_name(self, producer_model_mock, audit_mock):
        class DummyUser:
            id = "user-1"
            email = "sergio@example.com"
            first_name = "Sergio"
            last_name = "Costa"

            def __init__(self):
                self.save = Mock()

            @property
            def full_name(self):
                return f"{self.first_name} {self.last_name}".strip()

        user = DummyUser()
        producer_profile = SimpleNamespace(
            id="producer-1",
            display_name="Nome Antigo",
            save=Mock(),
        )
        producer_model_mock.objects.filter.return_value.first.return_value = producer_profile
        request = SimpleNamespace(session={})
        form = SimpleNamespace(
            cleaned_data={
                "first_name": "Joao",
                "last_name": "Silva",
            }
        )

        changed = update_account_profile(request=request, user=user, form=form)

        self.assertTrue(changed)
        self.assertEqual(user.full_name, "Joao Silva")
        self.assertEqual(request.session["user_name"], "Joao Silva")
        self.assertEqual(producer_profile.display_name, "Joao Silva")
        producer_profile.save.assert_called_once()
        self.assertEqual(audit_mock.call_count, 2)


class SettingsFormTests(SimpleTestCase):
    def test_profile_photo_form_requires_photo(self):
        form = ProfilePhotoForm(data={})

        self.assertFalse(form.is_valid())
        self.assertIn("profile_photo", form.errors)

    def test_producer_profile_rejects_invalid_coordinates(self):
        form = ProducerProfileSettingsForm(
            data={
                "company_name": "",
                "phone": "",
                "nif": "",
                "address_line": "",
                "postal_code": "",
                "city": "Viseu",
                "district": "Viseu",
                "latitude": Decimal("91"),
                "longitude": Decimal("181"),
                "user_type": "",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("latitude", form.errors)
        self.assertIn("longitude", form.errors)

    def test_producer_profile_form_does_not_expose_identity_or_marketplace_toggle(self):
        form = ProducerProfileSettingsForm()

        self.assertNotIn("display_name", form.fields)
        self.assertNotIn("is_active_marketplace", form.fields)
