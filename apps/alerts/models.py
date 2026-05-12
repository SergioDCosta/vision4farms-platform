import uuid
from django.db import models


class AlertType(models.TextChoices):
    SHORTAGE = "SHORTAGE", "Falta"
    CRITICAL_STOCK = "CRITICAL_STOCK", "Stock Crítico"
    SURPLUS_AVAILABLE = "SURPLUS_AVAILABLE", "Excedente Disponível"
    BUY_OPPORTUNITY = "BUY_OPPORTUNITY", "Oportunidade de Compra"
    SELL_SUGGESTION = "SELL_SUGGESTION", "Sugestão de Venda"
    EXTERNAL_DEFICIT = "EXTERNAL_DEFICIT", "Défice Externo"
    NEED_UNDERCOVERED = "NEED_UNDERCOVERED", "Necessidade por cobrir"
    NEED_RESPONSE_RECEIVED = "NEED_RESPONSE_RECEIVED", "Oferta recebida"
    NEED_DEADLINE_APPROACHING = "NEED_DEADLINE_APPROACHING", "Prazo de necessidade próximo"
    OFFER_REJECTED = "OFFER_REJECTED", "Oferta rejeitada"
    ORDER_REQUIRES_CONFIRMATION = "ORDER_REQUIRES_CONFIRMATION", "Encomenda por confirmar"
    ORDER_DELIVERY_OVERDUE = "ORDER_DELIVERY_OVERDUE", "Entrega atrasada"
    ORDER_PURCHASE_CREATED = "ORDER_PURCHASE_CREATED", "Compra criada"
    ORDER_CONFIRMED = "ORDER_CONFIRMED", "Encomenda confirmada"
    ORDER_IN_PROGRESS = "ORDER_IN_PROGRESS", "Encomenda em preparação"
    ORDER_DELIVERING = "ORDER_DELIVERING", "Encomenda em entrega"
    ORDER_CANCELLED = "ORDER_CANCELLED", "Encomenda cancelada"
    ORDER_COMPLETED = "ORDER_COMPLETED", "Encomenda concluída"
    LISTING_EXPIRING_SOON = "LISTING_EXPIRING_SOON", "Anúncio a expirar"
    MESSAGE_UNREAD = "MESSAGE_UNREAD", "Nova mensagem"


class AlertCategory(models.TextChoices):
    STOCK = "STOCK", "Stock"
    NEEDS = "NEEDS", "Necessidades"
    ORDERS = "ORDERS", "Encomendas"
    MARKETPLACE = "MARKETPLACE", "Marketplace"
    MESSAGES = "MESSAGES", "Mensagens"
    SYSTEM = "SYSTEM", "Sistema"


class AlertSeverity(models.TextChoices):
    INFO = "INFO", "Informação"
    WARNING = "WARNING", "Atenção"
    CRITICAL = "CRITICAL", "Crítico"


class AlertSourceSystem(models.TextChoices):
    INTERNAL = "INTERNAL", "Interno"
    VISION4FARMS = "VISION4FARMS", "Vision4Farms"
    MANUAL = "MANUAL", "Manual"


class AlertStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Ativo"
    READ = "READ", "Lido"
    RESOLVED = "RESOLVED", "Resolvido"
    IGNORED = "IGNORED", "Ignorado"
    CLEARED = "CLEARED", "Limpo"


class AlertEventType(models.TextChoices):
    CREATED = "CREATED", "Criado"
    READ = "READ", "Lido"
    RESOLVED = "RESOLVED", "Resolvido"
    IGNORED = "IGNORED", "Ignorado"
    CLEARED = "CLEARED", "Limpo"


class AlertDeliveryChannel(models.TextChoices):
    IN_APP = "IN_APP", "Na app"
    EMAIL = "EMAIL", "Email"
    SMS = "SMS", "SMS"


class AlertDeliveryStatus(models.TextChoices):
    PENDING = "PENDING", "Pendente"
    SENT = "SENT", "Enviado"
    SKIPPED = "SKIPPED", "Ignorado"
    FAILED = "FAILED", "Falhado"


class Alert(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.CASCADE,
        related_name="alerts",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="alerts",
    )
    need = models.ForeignKey(
        "needs.Need",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="alerts",
    )
    forecast = models.ForeignKey(
        "inventory.ProductionForecast",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="alerts",
    )
    listing = models.ForeignKey(
        "marketplace.MarketplaceListing",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="alerts",
    )
    type = models.CharField(max_length=30, choices=AlertType.choices)
    severity = models.CharField(max_length=20, choices=AlertSeverity.choices)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    source_system = models.CharField(max_length=30, choices=AlertSourceSystem.choices)
    status = models.CharField(max_length=20, choices=AlertStatus.choices, default=AlertStatus.ACTIVE)
    assumed_loss = models.BooleanField(default=False)
    ignored_reason = models.TextField(blank=True, null=True)
    ignored_at = models.DateTimeField(blank=True, null=True)
    cleared_at = models.DateTimeField(blank=True, null=True)
    category = models.CharField(max_length=30, choices=AlertCategory.choices, default=AlertCategory.SYSTEM)
    context_key = models.CharField(max_length=180, blank=True, null=True)
    requires_action = models.BooleanField(default=False)
    due_at = models.DateTimeField(blank=True, null=True)
    read_at = models.DateTimeField(blank=True, null=True)
    snoozed_until = models.DateTimeField(blank=True, null=True)
    expires_at = models.DateTimeField(blank=True, null=True)
    priority = models.SmallIntegerField(default=50)
    payload = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "alerts"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class AlertDelivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alert = models.ForeignKey(
        "alerts.Alert",
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="alert_deliveries",
    )
    channel = models.CharField(max_length=20, choices=AlertDeliveryChannel.choices)
    status = models.CharField(
        max_length=20,
        choices=AlertDeliveryStatus.choices,
        default=AlertDeliveryStatus.PENDING,
    )
    error = models.TextField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "alert_deliveries"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.alert_id} - {self.channel} - {self.status}"


class AlertEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alert = models.ForeignKey(
        "alerts.Alert",
        on_delete=models.CASCADE,
        related_name="events",
    )
    event_type = models.CharField(max_length=20, choices=AlertEventType.choices)
    performed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="alert_events",
    )
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "alert_events"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.alert} - {self.event_type}"
