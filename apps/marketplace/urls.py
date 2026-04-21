from django.urls import path

from apps.marketplace import views

app_name = "marketplace"

urlpatterns = [
    path("marketplace/", views.marketplace_index_view, name="index"),
    path("marketplace/necessidades/criar/", views.marketplace_need_create_view, name="need_create"),
    path("marketplace/necessidades/<uuid:need_id>/ignorar/", views.marketplace_need_ignore_view, name="need_ignore"),
    path("marketplace/publicar/", views.marketplace_publish_view, name="publish"),
    path("marketplace/<uuid:listing_id>/editar/", views.marketplace_edit_view, name="edit"),
    path("marketplace/<uuid:listing_id>/eliminar/", views.marketplace_delete_view, name="delete"),
    path("marketplace/<uuid:listing_id>/estado/", views.marketplace_toggle_status_view, name="toggle_status"),
    path("marketplace/<uuid:listing_id>/total/", views.marketplace_detail_total_view, name="detail_total"),
    path("marketplace/<uuid:listing_id>/", views.marketplace_detail_view, name="detail"),
]
