import uuid
from django.db import models


class ConversationType(models.TextChoices):
    DIRECT = "DIRECT", "Direta"
    LISTING_CONTACT = "LISTING_CONTACT", "Contacto de Anúncio"
    ORDER_CONTACT = "ORDER_CONTACT", "Contacto de Encomenda"


class MessageType(models.TextChoices):
    TEXT = "TEXT", "Texto"
    SYSTEM_EVENT = "SYSTEM_EVENT", "Evento do Sistema"


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation_type = models.CharField(max_length=20, choices=ConversationType.choices)
    title = models.CharField(max_length=255, blank=True, null=True)
    listing = models.ForeignKey(
        "marketplace.MarketplaceListing",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="conversations",
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="conversations",
    )
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="created_conversations",
    )
    is_active = models.BooleanField(default=True)
    last_message_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "conversations"
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title or f"Conversa {self.id}"


class ConversationParticipant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        "messaging.Conversation",
        on_delete=models.CASCADE,
        related_name="participants",
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="conversation_participations",
    )
    last_read_at = models.DateTimeField(blank=True, null=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    is_archived = models.BooleanField(default=False)

    class Meta:
        managed = False
        db_table = "conversation_participants"
        unique_together = (("conversation", "user"),)

    def __str__(self):
        return f"{self.user} em {self.conversation}"


class Message(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        "messaging.Conversation",
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender_user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="sent_messages",
    )
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.TEXT)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "messages"
        ordering = ["created_at"]

    def __str__(self):
        return f"Mensagem {self.id}"