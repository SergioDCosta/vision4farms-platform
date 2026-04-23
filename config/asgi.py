import os
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.sessions import SessionMiddlewareStack
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_asgi_app = get_asgi_application()

import apps.messaging.routing
import apps.alerts.routing
import apps.support.routing

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": SessionMiddlewareStack(
        URLRouter(
            apps.messaging.routing.websocket_urlpatterns
            + apps.support.routing.websocket_urlpatterns
            + apps.alerts.routing.websocket_urlpatterns
        )
    ),
})
