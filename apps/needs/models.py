import uuid

from django.db import models


class NeedSourceSystem(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    VISION4FARMS = "VISION4FARMS", "Vision4Farms"
    ALERT = "ALERT", "Alerta"
    CUSTOMER_DEMAND = "CUSTOMER_DEMAND", "Pedidos de clientes"


class NeedStatus(models.TextChoices):
    OPEN = "OPEN", "Aberta"
    PARTIALLY_COVERED = "PARTIALLY_COVERED", "Parcialmente Coberta"
    COVERED = "COVERED", "Coberta"
    IGNORED = "IGNORED", "Ignorada"
    CANCELLED = "CANCELLED", "Cancelada"


class NeedResponseStatus(models.TextChoices):
    PENDING = "PENDING", "Pendente"
    ACCEPTED = "ACCEPTED", "Aceite"
    REJECTED = "REJECTED", "Rejeitada"
    CANCELLED = "CANCELLED", "Cancelada"
    COMPLETED = "COMPLETED", "Concluída"
    WITHDRAWN = "WITHDRAWN", "Retirada"
    EXPIRED = "EXPIRED", "Expirada"


class ExternalCustomerDemandStatus(models.TextChoices):
    OPEN = "OPEN", "Aberto"
    PARTIALLY_COVERED = "PARTIALLY_COVERED", "Parcialmente coberto"
    COVERED = "COVERED", "Coberto"
    FULFILLED = "FULFILLED", "Cumprido"
    CANCELLED = "CANCELLED", "Cancelado"


class ExternalCustomerDemandSourceSystem(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    VISION4FARMS = "VISION4FARMS", "Vision4Farms"
    IMPORT = "IMPORT", "Importação"
    API = "API", "API"


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
    is_marketplace_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "needs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.producer} precisa de {self.product}"


class ExternalCustomerDemand(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.CASCADE,
        related_name="external_customer_demands",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.RESTRICT,
        related_name="external_customer_demands",
    )
    client_name = models.CharField(max_length=255)
    client_contact = models.CharField(max_length=255, blank=True, null=True)
    client_reference = models.CharField(max_length=120, blank=True, null=True)
    requested_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    requested_delivery_date = models.DateField()
    status = models.CharField(
        max_length=30,
        choices=ExternalCustomerDemandStatus.choices,
        default=ExternalCustomerDemandStatus.OPEN,
    )
    notes = models.TextField(blank=True, null=True)
    generated_need = models.ForeignKey(
        "needs.Need",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="external_customer_demands",
    )
    source_system = models.CharField(
        max_length=30,
        choices=ExternalCustomerDemandSourceSystem.choices,
        default=ExternalCustomerDemandSourceSystem.MANUAL,
    )
    external_id = models.CharField(max_length=120, blank=True, null=True)
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_external_customer_demands",
    )
    updated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="updated_external_customer_demands",
    )
    cancelled_at = models.DateTimeField(blank=True, null=True)
    fulfilled_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "external_customer_demands"
        ordering = ["requested_delivery_date", "-created_at"]

    def __str__(self):
        return f"{self.client_name} - {self.product} ({self.requested_quantity})"
