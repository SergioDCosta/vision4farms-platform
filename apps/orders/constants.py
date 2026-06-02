"""Constants shared by order domain services."""



from apps.orders.models import OrderItemStatus, OrderStatus

from decimal import Decimal





QTY_DECIMAL = Decimal("0.001")

MONEY_DECIMAL = Decimal("0.01")

RESERVED_ORDER_ITEM_STATUSES = (
    OrderItemStatus.PENDING,
    OrderItemStatus.CONFIRMED,
    OrderItemStatus.IN_DELIVERY,
)

PRESALE_TIMELINE_STEPS = (
    ("created", "Criada", {OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.IN_PROGRESS, OrderStatus.DELIVERING, OrderStatus.COMPLETED}),
    ("confirmed", "Confirmada", {OrderStatus.CONFIRMED, OrderStatus.IN_PROGRESS, OrderStatus.DELIVERING, OrderStatus.COMPLETED}),
    ("in_progress", "Em preparação", {OrderStatus.IN_PROGRESS, OrderStatus.DELIVERING, OrderStatus.COMPLETED}),
    ("delivered", "Entregue", {OrderStatus.DELIVERING, OrderStatus.COMPLETED}),
)

ORDER_STATUS_LABELS = dict(OrderStatus.choices)

INCOMING_FORECAST_ORDER_STATUSES = (
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.DELIVERING,
)
