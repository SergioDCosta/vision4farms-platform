from apps.marketplace.constants import (
    LISTING_SOURCE_FORECAST,
    LISTING_SOURCE_STOCK,
)
from apps.marketplace.utils import quantize_qty


def listing_audit_values(listing):
    if getattr(listing, "need_id", None):
        origin = "need_response"
    elif getattr(listing, "forecast_id", None):
        origin = LISTING_SOURCE_FORECAST
    else:
        origin = LISTING_SOURCE_STOCK
    return {
        "listing_id": str(getattr(listing, "id", "")) or None,
        "producer_id": str(getattr(listing, "producer_id", "")) or None,
        "product_id": str(getattr(listing, "product_id", "")) or None,
        "product_name": getattr(getattr(listing, "product", None), "name", None),
        "origin": origin,
        "stock_id": str(getattr(listing, "stock_id", "")) or None,
        "forecast_id": str(getattr(listing, "forecast_id", "")) or None,
        "need_id": str(getattr(listing, "need_id", "")) or None,
        "quantity_total": str(quantize_qty(getattr(listing, "quantity_total", 0) or 0)),
        "quantity_available": str(quantize_qty(getattr(listing, "quantity_available", 0) or 0)),
        "quantity_reserved": str(quantize_qty(getattr(listing, "quantity_reserved", 0) or 0)),
        "unit_price": str(getattr(listing, "unit_price", "")) or None,
        "delivery_mode": getattr(listing, "delivery_mode", None),
        "status": getattr(listing, "status", None),
    }


_listing_audit_values = listing_audit_values
