from apps.needs.models import ExternalCustomerDemandStatus, NeedStatus
from apps.orders.models import OrderStatus


ACTIVE_NEED_STATUSES = [NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED]
EDITABLE_NEED_STATUSES = [NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED, NeedStatus.COVERED]
PLANNED_NEED_ORDER_STATUSES = [
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.DELIVERING,
]
PUBLIC_OFFERED_ORDER_STATUSES = [
    OrderStatus.PENDING,
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.DELIVERING,
]
NEEDS_SEARCH_QUERY_MAX_LENGTH = 120
NEED_NOTES_MAX_LENGTH = 1200
NEED_RESPONSE_NOTES_MAX_LENGTH = 1200
EXTERNAL_DEMAND_SEARCH_QUERY_MAX_LENGTH = 120
EXTERNAL_DEMAND_NOTES_MAX_LENGTH = 1200
EXTERNAL_DEMAND_ACTIVE_STATUSES = [
    ExternalCustomerDemandStatus.OPEN,
    ExternalCustomerDemandStatus.PARTIALLY_COVERED,
    ExternalCustomerDemandStatus.COVERED,
]
EXTERNAL_DEMAND_EDITABLE_STATUSES = [
    ExternalCustomerDemandStatus.OPEN,
    ExternalCustomerDemandStatus.PARTIALLY_COVERED,
    ExternalCustomerDemandStatus.COVERED,
]
CUSTOMER_DEMAND_NEED_NOTES = (
    "Necessidade gerada automaticamente a partir de pedidos externos de clientes. "
    "A quantidade reflete o maior défice temporal entre pedidos acumulados e "
    "stock/previsão disponível até às datas de entrega."
)
