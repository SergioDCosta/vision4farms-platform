from decimal import Decimal
from urllib.parse import urlencode

from django.utils import timezone

from apps.accounts.models import UserRole
from apps.marketplace.constants import LISTING_SOURCE_FORECAST, LISTING_SOURCE_STOCK
from apps.marketplace.media import _listing_photo_url, _producer_profile_photo_url
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.marketplace.queries import (
    get_listing_categories_for_queryset,
    get_my_listings,
    get_public_listings,
    is_listing_editable_in_marketplace,
    is_listing_retirable_in_marketplace,
    is_listing_toggleable_in_marketplace,
)
from apps.marketplace.utils import (
    build_delivery_text,
    get_producer_display_name,
    get_producer_initials,
    get_producer_location,
)
from apps.needs.models import NeedResponseStatus, NeedStatus
from apps.needs.services import (
    calculate_need_coverage,
    get_need_for_producer,
    list_marketplace_my_published_needs,
    list_marketplace_public_needs,
    list_need_responses_for_owner,
    list_need_responses_for_responder,
)
from apps.orders.models import Order, OrderItem, OrderStatus


def _attach_listing_photo_urls(listings):
    attached = []
    for listing in listings:
        listing.photo_url = _listing_photo_url(getattr(listing, "photo_path", None))
        listing.can_edit_listing = is_listing_editable_in_marketplace(listing)
        listing.can_toggle_listing = is_listing_toggleable_in_marketplace(listing)
        listing.can_retire_listing = is_listing_retirable_in_marketplace(listing)
        listing.producer_display_name = get_producer_display_name(getattr(listing, "producer", None))
        listing.producer_location = get_producer_location(getattr(listing, "producer", None))
        listing.delivery_text = build_delivery_text(listing)
        listing.has_delivery = getattr(listing, "delivery_mode", None) in {"DELIVERY", "BOTH"}
        listing.has_pickup = getattr(listing, "delivery_mode", None) in {"PICKUP", "BOTH"}
        listing.total_value = Decimal(str(getattr(listing, "quantity_available", 0) or 0)) * Decimal(str(getattr(listing, "unit_price", 0) or 0))
        has_stock_source = bool(getattr(listing, "stock_id", None))
        has_forecast_source = bool(getattr(listing, "forecast_id", None))
        if has_forecast_source and not has_stock_source:
            listing.source_key = LISTING_SOURCE_FORECAST
            listing.source_label = "Pré-venda"
            listing.source_badge_class = "mk-badge--forecast"
            if getattr(listing, "forecast", None):
                period_start = getattr(listing.forecast, "period_start", None)
                period_end = getattr(listing.forecast, "period_end", None)
                local_start = timezone.localtime(period_start) if period_start and timezone.is_aware(period_start) else period_start
                local_end = timezone.localtime(period_end) if period_end and timezone.is_aware(period_end) else period_end
                if period_start and period_end:
                    listing.source_period = (
                        f"{local_start.strftime('%d/%m/%Y')} - "
                        f"{local_end.strftime('%d/%m/%Y')}"
                    )
                elif period_start:
                    listing.source_period = f"A partir de {local_start.strftime('%d/%m/%Y')}"
                else:
                    listing.source_period = None
            else:
                listing.source_period = None
        else:
            listing.source_key = LISTING_SOURCE_STOCK
            listing.source_label = "Stock atual"
            listing.source_badge_class = "mk-badge--stock"
            listing.source_period = None
        attached.append(listing)
    return attached


def _attach_viewer_order_info(producer, listings):
    """Anota cada listing com o estado da order mais recente do utilizador atual."""
    listing_ids = [listing.id for listing in listings]
    order_items = (
        OrderItem.objects
        .filter(order__buyer_producer=producer, listing_id__in=listing_ids)
        .select_related("order")
        .order_by("-created_at")
    )
    order_map = {}
    for oi in order_items:
        key = str(oi.listing_id)
        if key not in order_map:
            order_map[key] = {
                "item_status": oi.item_status,
                "order_id": str(oi.order_id),
            }
    producer_id = getattr(producer, "id", None)
    for listing in listings:
        listing.viewer_order_info = order_map.get(str(listing.id))
        listing.is_owner = bool(producer_id and listing.producer_id == producer_id)


def _first_non_empty_text(*values):
    for value in values:
        text = (value or "").strip()
        if text:
            return text
    return None


def _get_index_filters(request):
    source = request.POST if request.method == "POST" else request.GET
    active_tab = (source.get("tab") or "todos").strip()
    if active_tab not in {"todos", "meus", "necessidades", "compras", "respostas"}:
        active_tab = "todos"
    q = (source.get("q") or "").strip()
    category_id = (source.get("category") or "").strip()
    origin = (source.get("origin") or "").strip()
    if origin not in {"", LISTING_SOURCE_STOCK, LISTING_SOURCE_FORECAST}:
        origin = ""
    sort = (source.get("sort") or "recent").strip()
    if sort not in {"recent", "price_asc", "price_desc", "quantity_desc"}:
        sort = "recent"
    only_available = (source.get("available") or "").strip().lower() in {"1", "true", "yes", "on"}
    kind = (source.get("kind") or "all").strip()
    if kind not in {"all", "offers", "needs"}:
        kind = "all"
    need_id = (source.get("need") or "").strip()
    requested_product_id = (source.get("product") or source.get("product_id") or "").strip()
    requested_quantity = (source.get("qty") or source.get("required_quantity") or "").strip()
    show_need_form = (source.get("show_need_form") or "").strip().lower() in {"1", "true", "yes", "on"}
    return active_tab, q, category_id, origin, sort, only_available, kind, need_id, requested_product_id, requested_quantity, show_need_form


def _build_marketplace_index_query(*, active_tab, q, category_id, origin="", sort="recent", only_available=False, kind="all"):
    params = {"tab": active_tab, "q": q, "category": category_id}
    if origin:
        params["origin"] = origin
    if sort and sort != "recent":
        params["sort"] = sort
    if only_available:
        params["available"] = "1"
    if kind and kind != "all":
        params["kind"] = kind
    return urlencode(params)


def _build_marketplace_index_context(
    producer,
    *,
    active_tab,
    q,
    category_id,
    origin="",
    sort="recent",
    only_available=False,
    kind="all",
    selected_need_id="",
    need_prefill_product_id="",
    need_prefill_quantity="",
    show_need_form=False,
):
    public_listings = get_public_listings(
        producer=producer,
        q=q,
        category_id=category_id,
        origin=origin,
        sort=sort,
        only_available=True,
    ) if kind in {"all", "offers"} else []
    marketplace_need_rows = (
        list_marketplace_public_needs(
            viewer_producer=producer,
            q=q,
            category_id=category_id,
        )
        if active_tab == "todos" and kind in {"all", "needs"}
        else []
    )
    for row in marketplace_need_rows:
        row["producer_location"] = get_producer_location(
            getattr(row.get("need"), "producer", None)
        )
    my_listings = get_my_listings(
        producer=producer,
        q=q,
        category_id=category_id,
        origin=origin,
        sort=sort,
        only_available=only_available,
    ) if producer else MarketplaceListing.objects.none()

    my_published_need_rows = (
        list_marketplace_my_published_needs(
            producer=producer,
            q=q,
            category_id=category_id,
        )
        if active_tab == "meus" and producer
        else []
    )
    for row in my_published_need_rows:
        row["producer_location"] = get_producer_location(
            getattr(row.get("need"), "producer", None)
        )

    categories_source = (
        get_my_listings(producer=producer, q=q, category_id="", origin=origin, sort=sort, only_available=only_available)
        if active_tab == "meus" and producer
        else get_public_listings(producer=producer, q=q, category_id="", origin=origin, sort=sort, only_available=True)
    )
    available_categories = list(get_listing_categories_for_queryset(categories_source))
    need_category_map = {
        str(row["need"].product.category.id): row["need"].product.category
        for row in marketplace_need_rows
        if getattr(getattr(row["need"], "product", None), "category", None)
    }
    existing_category_ids = {str(category.id) for category in available_categories}
    for category_id_key, category in need_category_map.items():
        if category_id_key not in existing_category_ids:
            available_categories.append(category)
            existing_category_ids.add(category_id_key)
    available_categories = sorted(
        available_categories,
        key=lambda category: (category.name or "").lower(),
    )

    if category_id and all(str(category.id) != category_id for category in available_categories):
        selected_public = (
            get_public_listings(producer=producer, q="", category_id=category_id, origin=origin, sort=sort)
            .exclude(product__category_id__isnull=True)
            .first()
        )
        selected_private = (
            get_my_listings(producer=producer, q="", category_id=category_id, origin=origin, sort=sort, only_available=only_available)
            .exclude(product__category_id__isnull=True)
            .first()
            if producer else None
        )
        selected_listing = selected_private or selected_public
        if selected_listing and selected_listing.product and selected_listing.product.category:
            available_categories.append(selected_listing.product.category)

    public_listings = _attach_listing_photo_urls(public_listings)
    my_listings = _attach_listing_photo_urls(my_listings)

    # Anotar listings públicos com info de order do utilizador atual (badges)
    if producer and public_listings:
        _attach_viewer_order_info(producer, public_listings)

    visible_public_marketplace_count = len(public_listings) + len(marketplace_need_rows)
    visible_my_listings_count = len(my_listings) + len(my_published_need_rows)

    # Tab badges describe the available sections, independently from the
    # active tab and its filters. Otherwise the public needs count disappears
    # as soon as the producer opens "Meus anúncios".
    public_tab_offers_count = get_public_listings(producer=producer).count()
    public_tab_needs_count = len(list_marketplace_public_needs(viewer_producer=producer))
    public_tab_count = public_tab_offers_count + public_tab_needs_count
    my_tab_listings_count = get_my_listings(producer=producer).count() if producer else 0
    my_tab_needs_count = len(list_marketplace_my_published_needs(producer=producer)) if producer else 0
    my_tab_count = my_tab_listings_count + my_tab_needs_count

    # Contagens para badges dos novos tabs (sempre calculadas se houver produtor)
    my_active_orders_count = 0
    my_pending_responses_count = 0
    if producer:
        my_active_orders_count = Order.objects.filter(
            buyer_producer=producer,
            status__in=[
                OrderStatus.PENDING,
                OrderStatus.CONFIRMED,
                OrderStatus.IN_PROGRESS,
                OrderStatus.DELIVERING,
            ],
        ).count()
        pending_sent_responses_count = MarketplaceListing.objects.filter(
            producer=producer,
            need_id__isnull=False,
            status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
            need_response_status=NeedResponseStatus.PENDING,
        ).count()
        pending_received_responses_count = MarketplaceListing.objects.filter(
            need__producer=producer,
            need_id__isnull=False,
            status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
            need_response_status=NeedResponseStatus.PENDING,
        ).count()
        my_pending_responses_count = pending_sent_responses_count + pending_received_responses_count

    # Dados completos para os novos tabs (só carregados quando necessário)
    my_orders = []
    my_active_orders = []
    my_past_orders = []
    purchase_summary = {
        "active_count": 0,
        "active_total": Decimal("0.00"),
        "completed_count": 0,
        "completed_total": Decimal("0.00"),
        "cancelled_count": 0,
    }
    sent_need_responses = []
    sent_active_need_responses = []
    sent_past_need_responses = []
    received_active_need_responses = []
    received_past_need_responses = []
    if active_tab == "compras" and producer:
        my_orders = list(
            Order.objects
            .filter(buyer_producer=producer)
            .prefetch_related("items", "items__product", "items__seller_producer")
            .order_by("-created_at")
        )
        in_progress_statuses = {
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
            OrderStatus.IN_PROGRESS,
            OrderStatus.DELIVERING,
        }
        for order in my_orders:
            amount = Decimal(str(order.total_amount or 0))
            if order.status in in_progress_statuses:
                my_active_orders.append(order)
                purchase_summary["active_count"] += 1
                purchase_summary["active_total"] += amount
            elif order.status == OrderStatus.COMPLETED:
                my_past_orders.append(order)
                purchase_summary["completed_count"] += 1
                purchase_summary["completed_total"] += amount
            elif order.status == OrderStatus.CANCELLED:
                my_past_orders.append(order)
                purchase_summary["cancelled_count"] += 1
    elif active_tab == "respostas" and producer:
        received_need_responses = list_need_responses_for_owner(owner_producer=producer)
        received_active_need_responses = [
            response for response in received_need_responses
            if response.response_status == NeedResponseStatus.PENDING
        ]
        received_past_need_responses = [
            response for response in received_need_responses
            if response.response_status != NeedResponseStatus.PENDING
        ]
        sent_need_responses = list_need_responses_for_responder(responder_producer=producer)
        sent_active_need_responses = [
            response for response in sent_need_responses
            if response.response_status == NeedResponseStatus.PENDING
        ]
        sent_past_need_responses = [
            response for response in sent_need_responses
            if response.response_status != NeedResponseStatus.PENDING
        ]

    return {
        "page_title": "Marketplace",
        "active_tab": active_tab,
        "q": q,
        "selected_category_id": category_id,
        "selected_origin": origin,
        "selected_sort": sort,
        "only_available": only_available,
        "selected_kind": kind,
        "listings": public_listings,
        "marketplace_need_rows": marketplace_need_rows,
        "my_listings": my_listings,
        "my_published_need_rows": my_published_need_rows,
        "my_needs_count": my_tab_needs_count,
        "public_listings_count": public_tab_count,
        "public_offers_count": len(public_listings),
        "public_needs_count": len(marketplace_need_rows),
        "my_listings_count": my_tab_count,
        "visible_public_listings_count": visible_public_marketplace_count,
        "visible_my_listings_count": visible_my_listings_count,
        "my_active_orders_count": my_active_orders_count,
        "my_pending_responses_count": my_pending_responses_count,
        "my_orders": my_orders,
        "my_active_orders": my_active_orders,
        "my_past_orders": my_past_orders,
        "purchase_summary": purchase_summary,
        "sent_need_responses": sent_need_responses,
        "sent_active_need_responses": sent_active_need_responses,
        "sent_past_need_responses": sent_past_need_responses,
        "received_active_need_responses": received_active_need_responses,
        "received_past_need_responses": received_past_need_responses,
        "selected_need_id": selected_need_id,
        "selected_need_row": None,
        "need_prefill_product_id": need_prefill_product_id,
        "need_prefill_quantity": need_prefill_quantity,
        "show_need_form": bool(show_need_form),
        "available_categories": available_categories,
        "can_publish": bool(producer),
    }


def _build_listing_purchase_quote(listing, raw_quantity=None):
    default_quantity = Decimal("100")
    minimum_quantity = Decimal("0.001")
    has_user_quantity_input = raw_quantity not in (None, "")
    parsed_quantity = None

    if has_user_quantity_input:
        try:
            parsed_quantity = Decimal(str(raw_quantity).strip())
        except Exception:
            parsed_quantity = None
    else:
        parsed_quantity = default_quantity

    invalid_quantity_input = parsed_quantity is None
    quantity = parsed_quantity if parsed_quantity is not None else minimum_quantity
    max_quantity = Decimal(str(listing.quantity_available or 0))
    is_quantity_clamped = False

    if max_quantity <= 0:
        if quantity != Decimal("0"):
            is_quantity_clamped = True
        quantity = Decimal("0")
    else:
        if quantity < minimum_quantity:
            quantity = minimum_quantity
            is_quantity_clamped = True
        if quantity > max_quantity:
            quantity = max_quantity
            is_quantity_clamped = True

    if invalid_quantity_input and has_user_quantity_input:
        is_quantity_clamped = True

    total = quantity * Decimal(str(listing.unit_price or 0))

    return {
        "quantity": quantity,
        "max_quantity": max_quantity,
        "total": total,
        "is_quantity_clamped": is_quantity_clamped,
        "has_user_quantity_input": has_user_quantity_input,
    }


def _build_marketplace_detail_context(request, listing, producer):
    quote = _build_listing_purchase_quote(
        listing,
        raw_quantity=request.GET.get("qty"),
    )

    producer_name = get_producer_display_name(listing.producer)
    producer_initials = get_producer_initials(listing.producer)
    producer_location = get_producer_location(listing.producer)
    delivery_text = build_delivery_text(listing)

    def _parse_valid_coordinates(profile):
        raw_latitude = getattr(profile, "latitude", None)
        raw_longitude = getattr(profile, "longitude", None)
        try:
            if raw_latitude is None or raw_longitude is None:
                return None, None
            candidate_latitude = float(raw_latitude)
            candidate_longitude = float(raw_longitude)
            if -90.0 <= candidate_latitude <= 90.0 and -180.0 <= candidate_longitude <= 180.0:
                return candidate_latitude, candidate_longitude
        except (TypeError, ValueError):
            return None, None
        return None, None

    map_latitude, map_longitude = _parse_valid_coordinates(listing.producer)
    map_show_enabled = bool(getattr(listing, "show_location_on_map", True))
    map_city = (getattr(listing.producer, "city", None) or "").strip()
    map_district = (getattr(listing.producer, "district", None) or "").strip()
    map_location_query = (
        ", ".join(part for part in [map_city, map_district, "Portugal"] if part)
        if (map_city or map_district)
        else ""
    )
    map_location_label = ", ".join(part for part in [map_city, map_district] if part)

    map_delivery_radius_km = None
    if listing.delivery_mode in {"DELIVERY", "BOTH"} and listing.delivery_radius_km is not None:
        try:
            radius_km = Decimal(str(listing.delivery_radius_km))
            if radius_km > 0:
                map_delivery_radius_km = float(radius_km)
        except (TypeError, ValueError):
            map_delivery_radius_km = None

    if not map_show_enabled:
        map_mode = "hidden"
    elif map_latitude is not None and map_longitude is not None:
        map_mode = "exact"
    elif map_city or map_district:
        map_mode = "approximate"
    else:
        map_mode = "unavailable"

    map_privacy_message = None
    if map_mode == "hidden":
        map_privacy_message = (
            "O produtor preferiu não divulgar a localização no mapa neste anúncio."
        )
    elif map_mode == "approximate":
        map_privacy_message = (
            "O produtor preferiu não divulgar a localização exata da exploração. "
            "Contacta-o diretamente para combinar detalhes."
        )

    can_show_delivery_map = map_mode in {"exact", "approximate"}
    buyer_map_latitude = None
    buyer_map_longitude = None
    buyer_map_name = None
    if producer and producer.id != listing.producer_id:
        buyer_map_latitude, buyer_map_longitude = _parse_valid_coordinates(producer)
        if buyer_map_latitude is not None and buyer_map_longitude is not None:
            buyer_map_name = get_producer_display_name(producer)

    detail_description = _first_non_empty_text(listing.notes) or "Não foram colocadas observações."

    producer_member_since = None
    producer_user = getattr(listing.producer, "user", None)
    producer_profile_photo_url = _producer_profile_photo_url(producer_user)
    producer_published_listings_count = (
        MarketplaceListing.objects
        .filter(producer_id=listing.producer_id)
        .count()
    )
    if producer_user and getattr(producer_user, "created_at", None):
        producer_member_since = producer_user.created_at.year

    is_owner_listing = bool(producer and listing.producer_id == producer.id)
    is_need_response_listing = bool(getattr(listing, "need_id", None))
    is_need_owner_listing = bool(
        producer
        and is_need_response_listing
        and getattr(listing, "need", None)
        and listing.need.producer_id == producer.id
    )
    is_admin_user = getattr(request.current_user, "role", None) == UserRole.ADMIN
    can_purchase_listing = (
        not is_admin_user
        and not is_owner_listing
        and (
            is_need_owner_listing
            if is_need_response_listing
            else True
        )
    )
    show_buybox = is_owner_listing or can_purchase_listing
    can_edit_listing = is_listing_editable_in_marketplace(listing)
    can_toggle_listing = is_listing_toggleable_in_marketplace(listing)
    can_retire_listing = is_listing_retirable_in_marketplace(listing)
    expires_at_local = None
    if listing.expires_at:
        expires_at_local = timezone.localtime(listing.expires_at)

    has_stock_source = bool(listing.stock_id)
    has_forecast_source = bool(listing.forecast_id)
    if has_forecast_source and not has_stock_source:
        listing_source_key = LISTING_SOURCE_FORECAST
        listing_source_label = "Pré-venda"
        listing_source_badge_class = "mkd-badge--forecast"
        forecast_period_text = None
        if listing.forecast:
            local_start = (
                timezone.localtime(listing.forecast.period_start)
                if listing.forecast.period_start and timezone.is_aware(listing.forecast.period_start)
                else listing.forecast.period_start
            )
            local_end = (
                timezone.localtime(listing.forecast.period_end)
                if listing.forecast.period_end and timezone.is_aware(listing.forecast.period_end)
                else listing.forecast.period_end
            )
            if listing.forecast.period_start and listing.forecast.period_end:
                forecast_period_text = (
                    f"{local_start.strftime('%d/%m/%Y')} - "
                    f"{local_end.strftime('%d/%m/%Y')}"
                )
            elif listing.forecast.period_start:
                forecast_period_text = (
                    f"A partir de {local_start.strftime('%d/%m/%Y')}"
                )
    else:
        listing_source_key = LISTING_SOURCE_STOCK
        listing_source_label = "Disponível agora"
        listing_source_badge_class = "mkd-badge--stock"
        forecast_period_text = None

    selected_need_id = (request.GET.get("need") or "").strip()
    linked_need = None
    linked_need_remaining = None
    if selected_need_id and producer:
        candidate_need = get_need_for_producer(producer=producer, need_id=selected_need_id)
        if (
            candidate_need
            and candidate_need.status != NeedStatus.IGNORED
            and candidate_need.product_id == listing.product_id
        ):
            linked_need = candidate_need
            selected_need_id = str(candidate_need.id)
            linked_need_remaining = calculate_need_coverage(candidate_need)["remaining_to_plan"]
        else:
            selected_need_id = ""

    return {
        "page_title": "Detalhe do Produto",
        "listing": listing,
        "listing_photo_url": _listing_photo_url(listing.photo_path),
        **quote,
        "producer_name": producer_name,
        "producer_initials": producer_initials,
        "producer_profile_photo_url": producer_profile_photo_url,
        "producer_published_listings_count": producer_published_listings_count,
        "producer_location": producer_location,
        "delivery_text": delivery_text,
        "map_latitude": map_latitude,
        "map_longitude": map_longitude,
        "map_show_enabled": map_show_enabled,
        "map_city": map_city,
        "map_district": map_district,
        "map_location_query": map_location_query if map_location_query else None,
        "map_location_label": map_location_label if map_location_label else None,
        "map_mode": map_mode,
        "map_privacy_message": map_privacy_message,
        "map_delivery_radius_km": map_delivery_radius_km,
        "can_show_delivery_map": can_show_delivery_map,
        "buyer_map_latitude": buyer_map_latitude,
        "buyer_map_longitude": buyer_map_longitude,
        "buyer_map_name": buyer_map_name,
        "detail_description": detail_description,
        "producer_member_since": producer_member_since,
        "is_owner_listing": is_owner_listing,
        "is_need_response_listing": is_need_response_listing,
        "is_need_owner_listing": is_need_owner_listing,
        "can_purchase_listing": can_purchase_listing,
        "can_edit_listing": can_edit_listing,
        "can_toggle_listing": can_toggle_listing,
        "can_retire_listing": can_retire_listing,
        "is_admin_user": is_admin_user,
        "show_buybox": show_buybox,
        "expires_at_local": expires_at_local,
        "listing_source_key": listing_source_key,
        "listing_source_label": listing_source_label,
        "listing_source_badge_class": listing_source_badge_class,
        "forecast_period_text": forecast_period_text,
        "selected_need_id": selected_need_id,
        "linked_need": linked_need,
        "linked_need_remaining": linked_need_remaining,
    }
