from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from apps.support import views


class SupportRedirectTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("apps.support.views.redirect")
    def test_redirect_to_support_uses_safe_local_next(self, redirect_mock):
        request = self.factory.post("/suporte/tickets/", {"next": "/definicoes/"})

        views._redirect_to_support(request)

        redirect_mock.assert_called_once_with("/definicoes/")

    @patch("apps.support.views.redirect")
    def test_redirect_to_support_rejects_external_next(self, redirect_mock):
        request = self.factory.post(
            "/suporte/tickets/",
            {"next": "https://evil.example/phish"},
        )
        request.current_user = SimpleNamespace(id="user-1")

        views._redirect_to_support(request)

        redirect_mock.assert_called_once_with("support:index")
