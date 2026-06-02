"""Compatibility façade for inventory operations grouped by responsibility."""

from apps.inventory.audit import (
    _audit_qty,
    _forecast_audit_values,
    _log_stock_movement,
    _stock_audit_values,
)
from apps.inventory.commitments import (
    calculate_inventory_commitment_state,
    stock_state as _stock_state,
)
from apps.inventory.constants import (
    COMMERCIAL_IN_PROGRESS_ORDER_STATUSES,
    COMPLETED_ORDER_STATUS,
    MONTH_LABELS_PT,
    MONTH_SHORT_LABELS_PT,
    PRODUCTION_ENTRY_MOVEMENT_TYPES,
    STOCK_WARNING_MARGIN_RATIO,
    ZERO,
)
from apps.inventory.forecasts import (
    _forecast_periods_overlap,
    _forecast_saleable_quantity,
    assimilate_product_forecast_to_stock,
    delete_product_forecast,
    get_product_forecasts,
    save_product_forecast,
)
from apps.inventory.products import (
    _build_category_groups,
    _ensure_stock_for_product,
    add_product_to_producer,
    build_incoming_forecast_purchase_context,
    create_custom_product_for_producer,
    get_available_products_to_add,
    get_deactivated_products_dashboard,
    get_producer_profile,
    get_stock_dashboard,
    get_stock_for_product,
    get_stock_state,
    producer_has_active_inventory_products,
    reactivate_product_from_producer,
    remove_product_from_producer,
)
from apps.inventory.reporting import (
    _period_bounds,
    _period_chart_segments,
    get_purchase_dashboard,
    get_recent_orders_for_export,
)
from apps.inventory.stock_adjustments import (
    ListingsBlockStockReductionError,
    get_listings_blocking_stock_decrease,
    get_stock_activity_feed,
    get_stock_movements,
    reduce_listings_to_fit_stock,
    update_stock,
)
from apps.inventory.utils import (
    aware_datetime as _aware_datetime,
    format_qty as _format_qty,
    month_floor as _month_floor,
    progress_percent as _progress_percent,
    quantize_stock_quantity as _quantize_stock_quantity,
    safe_int as _safe_int,
    shift_month as _shift_month,
    to_decimal as _to_decimal,
)
