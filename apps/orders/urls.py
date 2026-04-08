from django.urls import path

from apps.orders import views
from apps.orders.models import OrderStatus

app_name = "orders"

urlpatterns = [
    path("encomendas/", views.orders_index_view, name="index"),
    path("encomendas/<uuid:order_id>/", views.order_detail_view, name="detail"),
    path("encomendas/criar/anuncio/<uuid:listing_id>/", views.create_order_from_listing_view, name="create_from_listing"),
    path("encomendas/<uuid:order_id>/confirmar-rececao/", views.confirm_order_receipt_view, name="confirm_receipt"),

    path("encomendas/<uuid:order_id>/estado/confirmar/", views.seller_update_order_status_view, {"status": OrderStatus.CONFIRMED}, name="seller_confirm"),
    path("encomendas/<uuid:order_id>/estado/preparar/", views.seller_update_order_status_view, {"status": OrderStatus.IN_PROGRESS}, name="seller_in_progress"),
    path("encomendas/<uuid:order_id>/estado/entrega/", views.seller_update_order_status_view, {"status": OrderStatus.DELIVERING}, name="seller_delivering"),
    path("encomendas/<uuid:order_id>/estado/cancelar/", views.seller_update_order_status_view, {"status": OrderStatus.CANCELLED}, name="seller_cancel"),
]