"""Compatibility façade for the order domain service modules."""

from apps.inventory.producers import get_current_producer_for_user
from apps.orders.constants import QTY_DECIMAL, MONEY_DECIMAL, RESERVED_ORDER_ITEM_STATUSES, PRESALE_TIMELINE_STEPS, ORDER_STATUS_LABELS, INCOMING_FORECAST_ORDER_STATUSES
from apps.orders.exceptions import (
    OrderServiceError,
)
from apps.orders.utils import (
    quantize_qty,
    quantize_money,
    _audit_qty,
    _order_audit_values,
    _quantity_label,
    _producer_display_name,
)
from apps.orders.notifications import (
    _sync_alerts_for_producers,
    _safe_emit_order_interaction_alert,
    _safe_create_order_update_notification,
    _order_detail_url_for_alert,
    _build_order_alert_summary,
    _notify_order_purchase_created,
    _notify_order_status_changed_to_buyer,
    _notify_order_completed_to_seller,
)
from apps.orders.reservations import (
    _log_listing_status_if_changed,
    _listing_source_kind,
    _map_delivery_method_from_listing,
    _validate_listing_source_xor,
    _is_persisted_model_instance,
    _lock_listing_for_order,
    _is_listing_expired,
    _validate_listing_can_be_ordered,
    _update_stock_reserved,
    _update_forecast_reserved,
    _consume_stock_reservation,
    _reconcile_listings_against_stock_capacity,
    _consume_forecast_reservation,
    _release_stock_reservation,
    _expected_reserved_quantity_for_listing,
    _reconcile_listing_reservation,
    _reserve_listing_quantity,
    _release_listing_reservation,
    _consume_listing_reservation,
    _ensure_buyer_product_link,
    _ensure_buyer_stock,
    _register_buyer_order_inbound,
    _release_forecast_reservation,
)
from apps.orders.statuses import (
    _create_status_history,
    _set_order_status,
    compute_order_status_from_db,
    reconcile_order_status,
    _recalculate_order_status,
    _log_order_status_change,
)
from apps.orders.projections import (
    _coerce_history_events,
    build_presale_timeline_context,
    get_buyer_incoming_forecast_projection,
)
from apps.orders.queries import (
    _collect_order_source_flags,
    is_order_from_need_response,
    is_order_forecast_only,
    get_order_source_label,
    compute_order_group_status,
    get_order_group_status_label,
    _sum_order_items_count,
    _sum_total_amount,
    _build_group_purchase_entry,
    _build_legacy_order_purchase_entry,
    _format_forecast_period_from_order,
    _build_presale_order_entry,
    get_presale_order_entries_for_producer,
    get_buyer_purchase_entries,
    get_orders_for_seller,
    get_order_group_detail_for_buyer,
    get_order_detail_for_buyer,
    get_order_detail_for_seller,
)
from apps.orders.lifecycle import (
    _next_order_number,
    _next_group_number,
    _create_order_group_with_retry,
    _create_order_with_retry,
    _sync_need_response_statuses_for_listing_ids,
    _sync_external_demands_for_product_change,
    create_order_from_listing,
    create_order_from_recommendation,
    confirm_order_receipt,
    buyer_cancel_order,
    seller_update_order_status,
)
