import uuid
from django.db import models


class OrderSourceType(models.TextChoices):
    MARKETPLACE = "MARKETPLACE", "Marketplace"
    RECOMMENDATION = "RECOMMENDATION", "Recomendação"


class OrderStatus(models.TextChoices):
    PENDING = "PENDING", "Pendente"
    CONFIRMED = "CONFIRMED", "Confirmada"
    IN_PROGRESS = "IN_PROGRESS", "Em Progresso"
    DELIVERING = "DELIVERING", "Em Entrega"
    COMPLETED = "COMPLETED", "Concluída"
    CANCELLED = "CANCELLED", "Cancelada"


class DeliveryMethod(models.TextChoices):
    PICKUP = "PICKUP", "Levantamento"
    DELIVERY = "DELIVERY", "Entrega"
    MIXED = "MIXED", "Misto"


class PaymentStatus(models.TextChoices):
    PENDING = "PENDING", "Pendente"
    PAID = "PAID", "Pago"
    FAILED = "FAILED", "Falhado"


class OrderItemStatus(models.TextChoices):
    PENDING = "PENDING", "Pendente"
    CONFIRMED = "CONFIRMED", "Confirmado"
    IN_DELIVERY = "IN_DELIVERY", "Em Entrega"
    COMPLETED = "COMPLETED", "Concluído"
    CANCELLED = "CANCELLED", "Cancelado"


class Order(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_number = models.BigIntegerField(unique=True)
    buyer_producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.CASCADE,
        related_name="orders_as_buyer",
    )
    source_type = models.CharField(max_length=20, choices=OrderSourceType.choices, default=OrderSourceType.MARKETPLACE)
    recommendation = models.ForeignKey(
        "recommendations.Recommendation",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="generated_orders",
    )
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.PENDING)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    delivery_method = models.CharField(max_length=20, choices=DeliveryMethod.choices, blank=True, null=True)
    delivery_address = models.TextField(blank=True, null=True)
    delivery_city = models.CharField(max_length=255, blank=True, null=True)
    delivery_notes = models.TextField(blank=True, null=True)
    payment_method = models.CharField(max_length=50, blank=True, null=True)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    buyer_notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "orders"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Encomenda #{self.order_number}"


class OrderItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="items",
    )
    listing = models.ForeignKey(
        "marketplace.MarketplaceListing",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_items",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.RESTRICT,
        related_name="order_items",
    )
    seller_producer = models.ForeignKey(
        "inventory.ProducerProfile",
        on_delete=models.RESTRICT,
        related_name="sales_order_items",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    item_status = models.CharField(max_length=20, choices=OrderItemStatus.choices, default=OrderItemStatus.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "order_items"

    def __str__(self):
        return f"{self.order} - {self.product}"


class OrderStatusHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="status_history",
    )
    status = models.CharField(max_length=20, choices=OrderStatus.choices)
    changed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_status_changes",
    )
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "order_status_history"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.order} - {self.status}"