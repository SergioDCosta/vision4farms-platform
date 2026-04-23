from django.urls import path
from apps.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("painel/", views.dashboard_view, name="painel"),
    path("painel/weather-card/", views.dashboard_weather_card_view, name="painel_weather_card"),

    path("gestor/", views.admin_dashboard_view, name="gestor"),

    path("gestor/produtos/", views.admin_products_view, name="gestor_produtos"),
    path("gestor/produtos/novo/", views.admin_product_create_view, name="gestor_produto_novo"),
    path("gestor/produtos/<uuid:product_id>/", views.admin_product_detail_view, name="gestor_produto_detalhe"),
    path("gestor/produtos/<uuid:product_id>/editar/", views.admin_product_update_view, name="gestor_produto_editar"),
    path("gestor/produtos/<uuid:product_id>/remover/", views.admin_product_delete_view, name="gestor_produto_remover"),

    path("gestor/categorias/", views.admin_categories_view, name="gestor_categorias"),
    path("gestor/categorias/nova/", views.admin_category_create_view, name="gestor_categoria_nova"),
    path("gestor/categorias/<uuid:category_id>/editar/", views.admin_category_update_view, name="gestor_categoria_editar"),

    path("gestor/utilizadores/", views.admin_users_view, name="gestor_utilizadores"),
    path("gestor/utilizadores/novo/", views.admin_user_create_view, name="gestor_utilizador_novo"),
    path("gestor/utilizadores/<uuid:user_id>/", views.admin_user_detail_view, name="gestor_utilizador_detalhe"),
    path(
        "gestor/utilizadores/<uuid:user_id>/confirmar-email/",
        views.admin_user_confirm_email_view,
        name="gestor_utilizador_confirmar_email",
    ),
    path("gestor/utilizadores/<uuid:user_id>/estado/", views.admin_user_toggle_status_view, name="gestor_utilizador_estado"),

    path("gestor/auditoria/", views.admin_audit_view, name="gestor_auditoria"),
]
