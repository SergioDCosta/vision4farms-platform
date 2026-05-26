from django.urls import path

from apps.needs import views


app_name = "needs"

urlpatterns = [
    path("necessidades/", views.needs_index_view, name="index"),
    path("necessidades/pedidos-clientes/", views.external_customer_demands_view, name="external_demands"),
    path("necessidades/pedidos-clientes/criar/", views.external_customer_demand_create_view, name="external_demand_create"),
    path(
        "necessidades/pedidos-clientes/<uuid:demand_id>/editar/",
        views.external_customer_demand_edit_view,
        name="external_demand_edit",
    ),
    path(
        "necessidades/pedidos-clientes/<uuid:demand_id>/cancelar/",
        views.external_customer_demand_cancel_view,
        name="external_demand_cancel",
    ),
    path(
        "necessidades/pedidos-clientes/<uuid:demand_id>/cumprir/",
        views.external_customer_demand_fulfill_view,
        name="external_demand_fulfill",
    ),
    path("necessidades/criar/", views.need_create_view, name="create"),
    path("necessidades/responder/", views.need_response_publish_view, name="respond"),
    path("necessidades/<uuid:need_id>/editar/", views.need_edit_view, name="edit"),
    path("necessidades/<uuid:need_id>/publicar/", views.need_publish_marketplace_view, name="publish_marketplace"),
    path(
        "necessidades/<uuid:need_id>/retirar-marketplace/",
        views.need_withdraw_marketplace_view,
        name="withdraw_marketplace",
    ),
    path("necessidades/<uuid:need_id>/ignorar/", views.need_ignore_view, name="ignore"),
    path("necessidades/respostas/<uuid:listing_id>/", views.need_response_detail_view, name="response_detail"),
    path("necessidades/respostas/<uuid:listing_id>/editar/", views.need_response_edit_view, name="response_edit"),
    path("necessidades/respostas/<uuid:listing_id>/rejeitar/", views.need_response_reject_view, name="response_reject"),
    path("marketplace/necessidades/criar/", views.need_create_view, name="legacy_create"),
    path("marketplace/necessidades/<uuid:need_id>/ignorar/", views.need_ignore_view, name="legacy_ignore"),
]
