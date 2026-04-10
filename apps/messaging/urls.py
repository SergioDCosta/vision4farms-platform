from django.urls import path

from apps.messaging import views

app_name = "messaging"

urlpatterns = [
    path("mensagens/", views.messages_index_view, name="index"),
    path(
        "mensagens/listing/<uuid:listing_id>/iniciar/",
        views.start_listing_contact_view,
        name="start_listing_contact",
    ),
]
