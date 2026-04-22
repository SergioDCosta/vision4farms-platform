from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("", include("apps.dashboard.urls")),
    path("", include("apps.alerts.urls")),
    path("", include("apps.inventory.urls")),
    path("", include("apps.recommendations.urls")),
    path("", include("apps.settings_app.urls")),
    path("", include("apps.support.urls")),
    path("", include("apps.marketplace.urls")),
    path("", include("apps.orders.urls")),
    path("", include("apps.messaging.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
