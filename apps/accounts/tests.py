from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.accounts import views


class RegisterEmailDeliveryTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _valid_register_request(self):
        request = self.factory.post(
            "/registo/",
            data={
                "first_name": "Sergio",
                "last_name": "Costa",
                "email": "sergio@example.com",
                "company": "Exploracao",
                "user_type": "AGRICULTOR",
                "password": "UmaSenhaForte123",
                "confirm_password": "UmaSenhaForte123",
            },
            REMOTE_ADDR="127.0.0.1",
        )
        request.session = {}
        return request

    @patch("apps.accounts.views.RegisterForm")
    @patch("apps.accounts.views.create_signup_verification_token")
    @patch("apps.accounts.views.create_user_and_profile")
    @patch("apps.accounts.views.send_signup_confirmation_email")
    @patch("apps.accounts.views.logger.exception")
    @patch("apps.accounts.views.transaction.atomic")
    def test_register_marks_confirmation_email_failure_without_rolling_back_account(
        self,
        atomic,
        logger_exception,
        send_email,
        create_user,
        create_token,
        form_class,
    ):
        atomic.return_value = nullcontext()
        form = Mock()
        form.is_valid.return_value = True
        form.cleaned_data = {"email": "sergio@example.com"}
        form_class.return_value = form
        user = SimpleNamespace(id="user-1", email="sergio@example.com")
        token = SimpleNamespace(token="token-1")
        create_user.return_value = user
        create_token.return_value = token
        send_email.side_effect = RuntimeError("email provider unavailable")

        request = self._valid_register_request()

        response = views.register_view.__wrapped__(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/registo/sucesso/")
        send_email.assert_called_once_with(request, user, token, async_send=False)
        logger_exception.assert_called_once()
        self.assertEqual(request.session["registration_email"], "sergio@example.com")
        self.assertTrue(request.session["registration_email_delivery_failed"])

    @override_settings(SUPPORT_CONTACT_EMAIL="suporte@example.com")
    @patch("apps.accounts.views.render")
    def test_register_success_context_includes_support_email_on_delivery_failure(
        self,
        render_mock,
    ):
        request = self.factory.get("/registo/sucesso/")
        request.session = {
            "registration_email": "sergio@example.com",
            "registration_email_delivery_failed": True,
        }
        render_mock.return_value = SimpleNamespace(status_code=200)

        views.register_success_view(request)

        context = render_mock.call_args.args[2]
        self.assertEqual(context["email"], "sergio@example.com")
        self.assertTrue(context["email_delivery_failed"])
        self.assertEqual(context["support_email"], "suporte@example.com")
