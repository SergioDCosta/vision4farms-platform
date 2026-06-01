from dataclasses import dataclass
from decimal import Decimal

from apps.marketplace.models import MarketplaceListing


@dataclass(frozen=True)
class NeedResponse:
    listing: MarketplaceListing
    id: object
    need_id: object
    producer_label: str
    need_owner_label: str
    product_name: str
    product_unit: str
    offered_quantity: Decimal
    available_quantity: Decimal
    ordered_quantity: Decimal
    quantity_available: Decimal
    unit_price: Decimal
    source_key: str
    source_label: str
    status: str
    status_label: str
    response_status: str
    response_status_label: str
    response_badge_class: str
    response_message: str
    can_buy: bool
    can_reject: bool
    notes: str
    detail_url: str
    reject_url: str
    edit_url: str
    is_editable: bool
    cta_label: str = "Ver oferta e comprar"


@dataclass(frozen=True)
class NeedResponseSummary:
    listing_id: object
    status: str
    status_label: str
    badge_class: str
    message: str
    detail_url: str
    is_active: bool
    edit_url: str = ""
    can_edit: bool = False
    can_send_new_proposal: bool = False
