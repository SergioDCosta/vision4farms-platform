import uuid
from django.db import models


class SupportTicketStatus(models.TextChoices):
    OPEN = "OPEN", "Aberto"
    CLAIMED = "CLAIMED", "Em tratamento"
    CLOSED = "CLOSED", "Fechado"


class SupportMessageRole(models.TextChoices):
    REQUESTER = "REQUESTER", "Utilizador"
    ADMIN = "ADMIN", "Admin"
    SYSTEM = "SYSTEM", "Sistema"


class SupportTicket(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket_number = models.BigIntegerField(unique=True)
    requester_user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="support_tickets_requested",
    )
    assigned_admin = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="support_tickets_assigned",
    )
    status = models.CharField(max_length=20, choices=SupportTicketStatus.choices)
    subject = models.CharField(max_length=255)
    message = models.TextField()

    requester_name_snapshot = models.CharField(max_length=255)
    requester_email_snapshot = models.CharField(max_length=255)
    requester_role_snapshot = models.CharField(max_length=50, blank=True, null=True)
    requester_company_snapshot = models.CharField(max_length=255, blank=True, null=True)
    requester_phone_snapshot = models.CharField(max_length=50, blank=True, null=True)

    admin_reply_message = models.TextField(blank=True, null=True)
    last_message_at = models.DateTimeField(blank=True, null=True)
    last_message_by_role = models.CharField(
        max_length=20,
        choices=SupportMessageRole.choices,
        blank=True,
        null=True,
    )
    claimed_at = models.DateTimeField(blank=True, null=True)
    admin_replied_at = models.DateTimeField(blank=True, null=True)
    closed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "support_tickets"
        ordering = ["-created_at"]

    def __str__(self):
        return f"#{self.ticket_number} - {self.subject}"


class SupportTicketMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(
        SupportTicket,
        on_delete=models.CASCADE,
        related_name="thread_messages",
    )
    sender_user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="support_messages",
    )
    sender_role = models.CharField(max_length=20, choices=SupportMessageRole.choices)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "support_ticket_messages"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.ticket_id} - {self.sender_role}"


class SupportTicketAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(
        SupportTicketMessage,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    storage_path = models.TextField()
    file_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size_bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "support_ticket_attachments"
        ordering = ["created_at"]

    def __str__(self):
        return self.file_name
