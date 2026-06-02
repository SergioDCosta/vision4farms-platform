from decimal import Decimal

from apps.inventory.models import StockMovementType


ZERO = Decimal("0.00")
STOCK_WARNING_MARGIN_RATIO = Decimal("0.10")
COMMERCIAL_IN_PROGRESS_ORDER_STATUSES = ["PENDING", "CONFIRMED", "IN_PROGRESS", "DELIVERING"]
COMPLETED_ORDER_STATUS = "COMPLETED"
PRODUCTION_ENTRY_MOVEMENT_TYPES = [
    StockMovementType.IMPORT,
    StockMovementType.CORRECTION,
    StockMovementType.MANUAL_ADJUSTMENT,
]
MONTH_LABELS_PT = [
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
]
MONTH_SHORT_LABELS_PT = [
    "Jan",
    "Fev",
    "Mar",
    "Abr",
    "Mai",
    "Jun",
    "Jul",
    "Ago",
    "Set",
    "Out",
    "Nov",
    "Dez",
]
