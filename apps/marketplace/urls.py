from django.urls import path

from apps.marketplace import views
from apps.needs import views as needs_views

app_name = "marketplace"

urlpatterns = [
    path("marketplace/", views.marketplace_index_view, name="index"),
    path("marketplace/publicar/", views.marketplace_publish_view, name="publish"),
    path(
        "marketplace/procuras/<uuid:need_id>/responder/",
        needs_views.need_response_publish_view,
        name="need_respond",
    ),
    path(
        "marketplace/propostas/<uuid:listing_id>/",
        needs_views.need_response_detail_view,
        name="proposal_detail",
    ),
    path(
        "marketplace/propostas/<uuid:listing_id>/editar/",
        needs_views.need_response_edit_view,
        name="proposal_edit",
    ),
    path(
        "marketplace/propostas/<uuid:listing_id>/rejeitar/",
        needs_views.need_response_reject_view,
        name="proposal_reject",
    ),
    path("marketplace/meus/<uuid:listing_id>/", views.marketplace_owner_detail_view, name="owner_detail"),
    path("marketplace/anuncios/<uuid:listing_id>/", views.marketplace_public_detail_view, name="public_detail"),
    path("marketplace/<uuid:listing_id>/editar/", views.marketplace_edit_view, name="edit"),
    path("marketplace/<uuid:listing_id>/eliminar/", views.marketplace_delete_view, name="delete"),
    path("marketplace/<uuid:listing_id>/estado/", views.marketplace_toggle_status_view, name="toggle_status"),
    path("marketplace/<uuid:listing_id>/total/", views.marketplace_detail_total_view, name="detail_total"),
    path("marketplace/<uuid:listing_id>/", views.marketplace_detail_view, name="detail"),
]
