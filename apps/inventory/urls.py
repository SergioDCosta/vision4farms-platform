from django.urls import path
from apps.inventory import views

app_name = "inventory"

urlpatterns = [
    path("inventario/produtos/", views.meus_produtos, name="meus_produtos"),
    path("inventario/produtos/adicionar/", views.adicionar_produto, name="adicionar_produto"),
    path(
        "inventario/produtos/<uuid:producer_product_id>/remover/",
        views.remover_produto,
        name="remover_produto",
    ),
    path(
        "inventario/produtos/<uuid:producer_product_id>/reativar/",
        views.reativar_produto,
        name="reativar_produto",
    ),
    path(
        "inventario/stock/<uuid:product_id>/",
        views.stock_detalhe,
        name="stock_detalhe",
    ),
    path(
        "inventario/stock/<uuid:product_id>/atualizar/",
        views.atualizar_stock,
        name="atualizar_stock",
    ),
    path(
        "inventario/stock/<uuid:product_id>/previsoes/guardar/",
        views.guardar_previsao,
        name="guardar_previsao",
    ),
    path(
        "inventario/stock/<uuid:product_id>/previsoes/<uuid:forecast_id>/remover/",
        views.remover_previsao,
        name="remover_previsao",
    ),
    path(
        "inventario/stock/<uuid:product_id>/previsoes/<uuid:forecast_id>/assimilar/",
        views.assimilar_previsao,
        name="assimilar_previsao",
    ),
    path(
        "inventario/compras/exportar/",
        views.compras_export_pdf,
        name="compras_export_pdf",
    ),
]
