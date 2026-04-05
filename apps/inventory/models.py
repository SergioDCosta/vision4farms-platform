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


class ForecastSourceSystem(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    VISION4FARMS = "VISION4FARMS", "Vision4Farms"
    MODEL = "MODEL", "Modelo"


class StockMovementType(models.TextChoices):
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT", "Ajuste Manual"
    ORDER_IN = "ORDER_IN", "Entrada por Encomenda"
    ORDER_OUT = "ORDER_OUT", "Saída por Encomenda"
    IMPORT = "IMPORT", "Importação"
    CORRECTION = "CORRECTION", "Correção"
    LISTING_PUBLISH = "LISTING_PUBLISH", "Publicação de Anúncio"
    LISTING_CANCEL = "LISTING_CANCEL", "Cancelamento de Anúncio"


class ProducerUserType(models.TextChoices):
    AGRICULTOR = "AGRICULTOR", "Agricultor / Produtor"
    DISTRIBUIDOR = "DISTRIBUIDOR", "Distribuidor"
    VENDEDOR = "VENDEDOR", "Vendedor / Retalhista"


class ProducerProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="producer_profile",
    )
    display_name = models.CharField(max_length=255)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    user_type = models.CharField(
        max_length=30,
        choices=ProducerUserType.choices,
        blank=True,
        null=True,
    )
    phone = models.CharField(max_length=20, blank=True, null=True)
    nif = models.CharField(max_length=20, blank=True, null=True)
    address_line = models.CharField(max_length=255, blank=True, null=True)
    postal_code = models.CharField(max_length=20, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    district = models.CharField(max_length=100, blank=True, null=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    member_since = models.DateTimeField()
    rating_avg = models.DecimalField(max_digits=3, decimal_places=2, blank=True, null=True)
    completed_transactions_count = models.IntegerField(default=0)
    is_active_marketplace = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "producer_profiles"
        ordering = ["display_name"]

    def __str__(self):
        return self.display_name

class ProducerProduct(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.CASCADE,
        related_name="produced_products",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="producer_links",
    )
    producer_description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "producer_products"
        unique_together = (("producer", "product"),)

    def __str__(self):
        return f"{self.producer} - {self.product}"


class Stock(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.CASCADE,
        related_name="stocks",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="stocks",
    )
    current_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    reserved_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    minimum_threshold = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    updated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="updated_stocks",
    )
    last_updated_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "stocks"
        unique_together = (("producer", "product"),)

    @property
    def available_quantity(self):
        return self.current_quantity - self.reserved_quantity

    def __str__(self):
        return f"{self.producer} - {self.product}"


class StockMovement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stock = models.ForeignKey(
        "inventory.Stock",
        on_delete=models.CASCADE,
        related_name="movements",
    )
    movement_type = models.CharField(max_length=50, choices=StockMovementType.choices)
    quantity_delta = models.DecimalField(max_digits=14, decimal_places=3)
    reference_type = models.CharField(max_length=50, blank=True, null=True)
    reference_id = models.UUIDField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    performed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="stock_movements",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "stock_movements"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.stock} - {self.movement_type}"


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
    source_system = models.CharField(max_length=30, choices=NeedSourceSystem.choices, default=NeedSourceSystem.MANUAL)
    external_id = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=30, choices=NeedStatus.choices, default=NeedStatus.OPEN)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "needs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.producer} precisa de {self.product}"


class ProductionForecast(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.CASCADE,
        related_name="forecasts",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="forecasts",
    )
    forecast_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    period_start = models.DateTimeField(blank=True, null=True)
    period_end = models.DateTimeField(blank=True, null=True)
    confidence_score = models.DecimalField(max_digits=4, decimal_places=3, blank=True, null=True)
    source_system = models.CharField(max_length=30, choices=ForecastSourceSystem.choices, default=ForecastSourceSystem.MANUAL)
    external_id = models.CharField(max_length=100, blank=True, null=True)
    source_payload = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "production_forecasts"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Previsão {self.product} - {self.producer}"
