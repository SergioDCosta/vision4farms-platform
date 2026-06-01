from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.template.loader import get_template
from django.test import SimpleTestCase, override_settings

from apps.settings_app.forms import IdentityProfileForm, ProducerLocationForm, ProfilePhotoForm
from apps.settings_app.services import (
    avatar_initials,
    ensure_user_preference,
    profile_photo_url,
    update_identity_profile,
)


class SettingsTemplateTests(SimpleTestCase):
    def test_settings_templates_load(self):
        template_names = [
            "settings/settings_panel.html",
            "settings/partials/identity_profile.html",
            "settings/partials/photo.html",
            "settings/partials/notifications.html",
            "settings/partials/location.html",
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

    @patch("apps.settings_app.services.log_audit_event")
    def test_update_identity_profile_syncs_producer_display_name(self, audit_mock):
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
            company_name="Empresa Antiga",
            phone=None,
            nif=None,
            user_type=None,
            save=Mock(),
        )
        request = SimpleNamespace(session={})
        form = SimpleNamespace(
            cleaned_data={
                "first_name": "Joao",
                "last_name": "Silva",
                "company_name": "Empresa Nova",
                "phone": None,
                "nif": None,
                "user_type": None,
            }
        )

        changed = update_identity_profile(
            request=request,
            user=user,
            producer_profile=producer_profile,
            form=form,
        )

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
        form = ProducerLocationForm(
            data={
                "address_line": "",
                "postal_code": "",
                "city": "Viseu",
                "district": "Viseu",
                "latitude": Decimal("91"),
                "longitude": Decimal("181"),
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("latitude", form.errors)
        self.assertIn("longitude", form.errors)

    def test_location_form_does_not_expose_identity_or_marketplace_toggle(self):
        form = ProducerLocationForm()

        self.assertNotIn("display_name", form.fields)
        self.assertNotIn("is_active_marketplace", form.fields)

    def test_identity_form_hides_producer_fields_for_admin_user(self):
        form = IdentityProfileForm(
            user=SimpleNamespace(
                first_name="Admin",
                last_name="User",
                email="admin@example.com",
            ),
        )

        self.assertNotIn("company_name", form.fields)
        self.assertNotIn("phone", form.fields)
        self.assertNotIn("nif", form.fields)
        self.assertNotIn("user_type", form.fields)
