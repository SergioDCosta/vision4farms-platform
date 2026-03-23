import uuid
from django.db import models


class NotificationType(models.TextChoices):
    ALERT = "ALERT", "Alerta"
    MESSAGE = "MESSAGE", "Mensagem"
    ORDER_UPDATE = "ORDER_UPDATE", "Atualização de Encomenda"
    RECOMMENDATION = "RECOMMENDATION", "Recomendação"
    SYSTEM = "SYSTEM", "Sistema"
    ACCOUNT = "ACCOUNT", "Conta"


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    alert = models.ForeignKey(
        "alerts.Alert",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="notifications",
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="notifications",
    )
    message = models.ForeignKey(
        "messaging.Message",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="notifications",
    )
    recommendation = models.ForeignKey(
        "recommendations.Recommendation",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="notifications",
    )
    type = models.CharField(max_length=30, choices=NotificationType.choices)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, null=True)
    action_url = models.CharField(max_length=500, blank=True, null=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "notifications"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title