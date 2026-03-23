from django.urls import path
from apps.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("painel/", views.dashboard_view, name="painel"),

    path("gestor/", views.admin_dashboard_view, name="gestor"),
    path("gestor/produtos/", views.admin_products_view, name="gestor_produtos"),
    path("gestor/categorias/", views.admin_categories_view, name="gestor_categorias"),

    path("gestor/utilizadores/", views.admin_users_view, name="gestor_utilizadores"),
    path("gestor/utilizadores/novo/", views.admin_user_create_view, name="gestor_utilizador_novo"),
    path("gestor/utilizadores/<uuid:user_id>/", views.admin_user_detail_view, name="gestor_utilizador_detalhe"),
    path("gestor/utilizadores/<uuid:user_id>/editar/", views.admin_user_update_view, name="gestor_utilizador_editar"),
    path("gestor/utilizadores/<uuid:user_id>/estado/", views.admin_user_toggle_status_view, name="gestor_utilizador_estado"),

    path("gestor/auditoria/", views.admin_audit_view, name="gestor_auditoria"),
]