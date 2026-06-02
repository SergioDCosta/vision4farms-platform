from decimal import Decimal

from apps.marketplace.models import ListingStatus


QTY_DECIMAL = Decimal("0.001")
LISTING_SOURCE_STOCK = "stock"
LISTING_SOURCE_FORECAST = "forecast"
MARKETPLACE_EDITABLE_STATUSES = {
    ListingStatus.ACTIVE,
    ListingStatus.CANCELLED,
    ListingStatus.EXPIRED,
}
MARKETPLACE_FINAL_STATUSES = {
    ListingStatus.RESERVED,
    ListingStatus.CLOSED,
}
