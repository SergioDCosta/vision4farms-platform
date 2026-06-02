"""Compatibility façade for the alert domain service modules."""

from apps.alerts.constants import ACTIVE_LIKE_ALERT_STATUSES, UI_TAB_STATUS_MAP, MANAGED_ALERT_TYPES, ORDER_ALERT_TYPES, AUTO_RESOLVED_NOTE, ALERTS_LAST_SEEN_SESSION_KEY, ALERTS_BADGE_GROUP_PREFIX, IGNORED_ALERT_TTL, INTERACTIVE_ALERT_STATUSES, SNOOZE_OPTIONS, DEFAULT_SNOOZE_KEY, NEED_DEADLINE_WINDOW, LISTING_EXPIRING_WINDOW, ORDER_CONFIRMATION_GRACE, ORDER_DELIVERY_GRACE, EMAIL_ALERT_TYPES
from apps.alerts.badges import (
    get_alerts_badge_group_name,
    broadcast_alerts_badge_changed_for_user,
    _queue_alerts_badge_changed_for_user,
    get_client_alerts_badge_state,
    mark_client_alerts_seen,
)
from apps.alerts.utils import (
    _as_decimal,
    _format_alert_quantity,
    _quantity_label,
    _money_label,
    get_alert_type_label,
    get_alert_category_label,
    normalize_alert_type,
    normalize_alert_category,
    _build_context_key,
    _alert_context_key,
    _candidate,
    _get_snooze_option,
)
from apps.alerts.delivery import (
    _get_user_alert_preferences,
    _user_wants_in_app_alerts,
    _user_wants_email_alerts,
    _user_wants_sms_alerts,
    _should_email_alert,
    _record_alert_delivery,
    _send_alert_email_now,
    _send_alert_email_safely,
    _queue_alert_email_delivery,
    deliver_alert_to_user,
    _queue_alert_delivery_for_producer,
)
from apps.alerts.events import (
    record_alert_event,
    create_order_interaction_alert,
    create_need_response_event_alert,
    upsert_message_unread_alert,
    resolve_message_unread_alert,
)
from apps.alerts.candidates import (
    _stock_commitment_rows,
    _critical_stock_candidates,
    _surplus_candidates,
    _need_candidates,
    _sell_suggestion_candidates,
    _need_response_candidates,
    _need_deadline_candidates,
    _buy_opportunity_candidates,
    _order_confirmation_candidates,
    _order_delivery_overdue_candidates,
    _listing_expiring_candidates,
    _candidate_rows,
)
from apps.alerts.sync import (
    _apply_candidate_to_alert,
    sync_alerts_for_producer,
    expire_due_alerts,
    run_operational_alerts_job,
)
from apps.alerts.actions import (
    ignore_alert,
    ignore_all_active_alerts,
    reactivate_ignored_alert,
    expire_ignored_alerts_for_producer,
    resolve_alert,
)
from apps.alerts.queries import (
    get_alert_for_producer,
    get_alert_tab_counts,
    get_alert_type_filter_options,
    get_alert_category_filter_options,
    _alert_section_key,
    build_alert_sections,
    list_alerts_for_producer,
)
