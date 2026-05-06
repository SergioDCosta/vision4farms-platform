from django.urls import path

from apps.needs import views


app_name = "needs"

urlpatterns = [
    path("necessidades/", views.needs_index_view, name="index"),
    path("necessidades/criar/", views.need_create_view, name="create"),
    path("necessidades/<uuid:need_id>/ignorar/", views.need_ignore_view, name="ignore"),
    path("necessidades/respostas/<uuid:listing_id>/", views.need_response_detail_view, name="response_detail"),
    path("necessidades/respostas/<uuid:listing_id>/rejeitar/", views.need_response_reject_view, name="response_reject"),
    path("marketplace/necessidades/criar/", views.need_create_view, name="legacy_create"),
    path("marketplace/necessidades/<uuid:need_id>/ignorar/", views.need_ignore_view, name="legacy_ignore"),
]
