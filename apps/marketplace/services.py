"""Compatibility façade for marketplace operations grouped by responsibility."""

from apps.inventory.producers import get_current_producer_for_user
from apps.marketplace.audit import _listing_audit_values, listing_audit_values
from apps.marketplace.availability import (
    _get_open_forecast_published_quantity,
    _get_open_stock_published_quantity,
    _get_pending_stock_need_response_quantity,
    _get_uncommitted_forecast_quantity,
    _valid_listing_source_filter,
    _validate_listing_source_xor,
    get_forecast_available_quantity,
    get_market_price_trends_for_product_sources,
    get_marketplace_eligible_forecasts,
    get_max_publishable_quantity,
    get_producer_products,
    get_publishable_products,
    get_publishable_products_summary,
    get_stock_available_quantity,
    get_stock_for_product,
    resolve_listing_source,
)
from apps.marketplace.commands import (
    create_listing,
    expire_due_active_listings,
    reactivate_listing,
    retire_listing,
    update_listing,
)
from apps.marketplace.constants import (
    LISTING_SOURCE_FORECAST,
    LISTING_SOURCE_STOCK,
    MARKETPLACE_EDITABLE_STATUSES,
    MARKETPLACE_FINAL_STATUSES,
    QTY_DECIMAL,
)
from apps.marketplace.exceptions import MarketplaceServiceError
from apps.marketplace.queries import (
    _apply_listing_filters,
    _apply_listing_sort,
    _retired_listing_filter,
    get_base_listing_queryset,
    get_listing_categories_for_queryset,
    get_listing_detail_queryset,
    get_my_listings,
    get_public_listings,
    is_listing_editable_in_marketplace,
    is_listing_retirable_in_marketplace,
    is_listing_retired_in_marketplace,
    is_listing_toggleable_in_marketplace,
)
from apps.marketplace.utils import (
    build_delivery_text,
    get_producer_display_name,
    get_producer_initials,
    get_producer_location,
    quantize_qty,
)
