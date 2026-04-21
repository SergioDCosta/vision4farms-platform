from django.urls import path

from apps.recommendations import views

app_name = "recommendations"

urlpatterns = [
    path("recomendacoes/", views.recommendations_index_view, name="index"),
    path("recomendacoes/produto-metricas/", views.recommendations_product_metrics_view, name="product_metrics"),
    path("recomendacoes/gerar/", views.recommendations_generate_view, name="step_generate"),
    path("recomendacoes/<uuid:recommendation_id>/confirmar/", views.recommendations_prepare_confirm_view, name="step_prepare_confirm"),
    path("recomendacoes/<uuid:recommendation_id>/ajustar/", views.recommendations_back_to_need_view, name="step_back_to_need"),
    path("recomendacoes/<uuid:recommendation_id>/necessidade/", views.recommendations_create_need_view, name="step_create_need"),
    path("recomendacoes/<uuid:recommendation_id>/aceitar/", views.recommendations_accept_view, name="confirm_order"),
    path("recomendacoes/<uuid:recommendation_id>/mercado/", views.recommendations_market_options_view, name="step_market_options"),
    path("recomendacoes/<uuid:recommendation_id>/substituir/", views.recommendations_replace_item_view, name="step_replace_item"),
]
