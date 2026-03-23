import uuid
from django.db import models


class RecommendationSourceType(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    ALERT = "ALERT", "Alerta"
    VISION4FARMS = "VISION4FARMS", "Vision4Farms"


class RecommendationStatus(models.TextChoices):
    GENERATED = "GENERATED", "Gerada"
    ACCEPTED = "ACCEPTED", "Aceite"
    ADJUSTED = "ADJUSTED", "Ajustada"
    IGNORED = "IGNORED", "Ignorada"
    EXPIRED = "EXPIRED", "Expirada"


class Recommendation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.CASCADE,
        related_name="recommendations",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="recommendations",
    )
    generated_from_alert = models.ForeignKey(
        "alerts.Alert",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="generated_recommendations",
    )
    requested_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    deadline_date = models.DateTimeField(blank=True, null=True)
    deficit_quantity = models.DecimalField(max_digits=14, decimal_places=3, blank=True, null=True)
    source_type = models.CharField(max_length=30, choices=RecommendationSourceType.choices, default=RecommendationSourceType.MANUAL)
    status = models.CharField(max_length=20, choices=RecommendationStatus.choices, default=RecommendationStatus.GENERATED)
    summary_text = models.TextField(blank=True, null=True)
    reason_summary = models.TextField(blank=True, null=True)
    estimated_total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    accepted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "recommendations"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Recomendação - {self.product} - {self.producer}"


class RecommendationItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recommendation = models.ForeignKey(
        "recommendations.Recommendation",
        on_delete=models.CASCADE,
        related_name="items",
    )
    listing = models.ForeignKey(
        "marketplace.MarketplaceListing",
        on_delete=models.RESTRICT,
        related_name="recommendation_items",
    )
    seller_producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.RESTRICT,
        related_name="recommended_sales",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.RESTRICT,
        related_name="recommendation_items",
    )
    suggested_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    position = models.IntegerField(default=1)
    is_selected = models.BooleanField(default=True)
    reasons = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "recommendation_items"
        ordering = ["position", "created_at"]

    def __str__(self):
        return f"{self.recommendation} - {self.product}"