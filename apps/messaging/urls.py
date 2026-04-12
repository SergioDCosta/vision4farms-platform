from django.urls import path

from apps.messaging import views

app_name = "messaging"

urlpatterns = [
    path("mensagens/", views.messages_index_view, name="index"),
    path("mensagens/upload/", views.upload_attachment_view, name="upload_attachment"),
    path(
        "mensagens/conversa/<uuid:conversation_id>/arquivar/",
        views.archive_conversation_view,
        name="archive_conversation",
    ),
    path(
        "mensagens/conversa/<uuid:conversation_id>/desarquivar/",
        views.unarchive_conversation_view,
        name="unarchive_conversation",
    ),
    path(
        "mensagens/listing/<uuid:listing_id>/iniciar/",
        views.start_listing_contact_view,
        name="start_listing_contact",
    ),
]
