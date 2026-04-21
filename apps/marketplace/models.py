import uuid
from django.db import models


class DeliveryMode(models.TextChoices):
    PICKUP = "PICKUP", "Levantamento"
    DELIVERY = "DELIVERY", "Entrega"
    BOTH = "BOTH", "Ambos"


class ListingStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Ativo"
    RESERVED = "RESERVED", "Reservado"
    CLOSED = "CLOSED", "Fechado"
    EXPIRED = "EXPIRED", "Expirado"
    CANCELLED = "CANCELLED", "Desativado"


class MarketplaceListing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.CASCADE,
        related_name="listings",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="listings",
    )
    stock = models.ForeignKey(
        "inventory.Stock",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="listings",
    )
    forecast = models.ForeignKey(
        "inventory.ProductionForecast",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="listings",
    )
    need = models.ForeignKey(
        "inventory.Need",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="response_listings",
    )
    quantity_total = models.DecimalField(max_digits=14, decimal_places=3)
    quantity_available = models.DecimalField(max_digits=14, decimal_places=3)
    quantity_reserved = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    delivery_mode = models.CharField(max_length=20, choices=DeliveryMode.choices)
    delivery_radius_km = models.DecimalField(max_digits=8, decimal_places=2, blank=True, null=True)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    photo_path = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=20, choices=ListingStatus.choices, default=ListingStatus.ACTIVE)
    published_at = models.DateTimeField()
    expires_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "marketplace_listings"
        ordering = ["-published_at"]

    def __str__(self):
        return f"{self.product} - {self.producer}"
