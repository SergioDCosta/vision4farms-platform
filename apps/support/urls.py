from django.urls import path

from apps.support import views

app_name = "support"

urlpatterns = [
    path("suporte/tickets/", views.support_ticket_create_view, name="ticket_create"),
    path("gestor/suporte/", views.admin_support_tickets_view, name="admin_ticket_list"),
    path(
        "gestor/suporte/sidebar-state/",
        views.admin_support_sidebar_state_view,
        name="admin_ticket_sidebar_state",
    ),
    path(
        "gestor/suporte/<uuid:ticket_id>/",
        views.admin_support_ticket_detail_view,
        name="admin_ticket_detail",
    ),
    path(
        "gestor/suporte/<uuid:ticket_id>/claim/",
        views.admin_support_ticket_claim_view,
        name="admin_ticket_claim",
    ),
    path(
        "gestor/suporte/<uuid:ticket_id>/reply/",
        views.admin_support_ticket_reply_view,
        name="admin_ticket_reply",
    ),
]
