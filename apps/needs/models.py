import uuid

from django.db import models


class NeedSourceSystem(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    VISION4FARMS = "VISION4FARMS", "Vision4Farms"
    ALERT = "ALERT", "Alerta"


class NeedStatus(models.TextChoices):
    OPEN = "OPEN", "Aberta"
    PARTIALLY_COVERED = "PARTIALLY_COVERED", "Parcialmente Coberta"
    COVERED = "COVERED", "Coberta"
    IGNORED = "IGNORED", "Ignorada"
    CANCELLED = "CANCELLED", "Cancelada"


class Need(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.CASCADE,
        related_name="needs",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="needs",
    )
    required_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    needed_by_date = models.DateTimeField(blank=True, null=True)
    source_system = models.CharField(
        max_length=30,
        choices=NeedSourceSystem.choices,
        default=NeedSourceSystem.MANUAL,
    )
    external_id = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(
        max_length=30,
        choices=NeedStatus.choices,
        default=NeedStatus.OPEN,
    )
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "needs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.producer} precisa de {self.product}"
