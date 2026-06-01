from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.common.audit import log_audit_event
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.needs.models import NeedResponseStatus
from apps.needs.types import NeedResponse, NeedResponseSummary
from apps.needs.utils import (
    is_listing_effectively_expired,
    normalize_needs_search_query,
    producer_marketplace_display_name as _producer_marketplace_display_name,
    quantize_need_quantity as _quantize_need_quantity,
)
from apps.orders.models import OrderItem, OrderItemStatus


def _get_need_response_listings_for_owner(*, owner_producer, q="", category_id="", need_id=""):
    qs = (
        MarketplaceListing.objects
        .select_related(
            "producer",
            "producer__user",
            "product",
            "stock",
            "forecast",
            "need",
            "need__producer",
            "need__producer__user",
        )
        .filter(
            need_id__isnull=False,
            need__producer=owner_producer,
        )
        .order_by("-published_at", "-created_at")
    )

    if need_id:
        qs = qs.filter(need_id=need_id)

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(producer__display_name__icontains=q)
            | Q(producer__company_name__icontains=q)
            | Q(producer__user__first_name__icontains=q)
            | Q(producer__user__last_name__icontains=q)
            | Q(notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    return qs


def _get_need_response_listing_queryset():
    return (
        MarketplaceListing.objects
        .select_related(
            "producer",
            "producer__user",
            "product",
            "stock",
            "forecast",
            "need",
            "need__producer",
            "need__producer__user",
            "need__product",
        )
        .filter(need_id__isnull=False)
    )


def get_need_response_listing_for_viewer(*, viewer_producer, listing_id):
    if not viewer_producer:
        return None

    return (
        _get_need_response_listing_queryset()
        .filter(id=listing_id)
        .filter(Q(need__producer=viewer_producer) | Q(producer=viewer_producer))
        .first()
    )


def get_active_need_response_for_responder(*, responder_producer, need):
    if not responder_producer or not need:
        return None

    listings = list(
        _get_need_response_listing_queryset()
        .filter(producer=responder_producer, need=need)
        .order_by("-published_at", "-created_at")
    )
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    for listing in listings:
        state = _derive_need_response_state(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
        )
        if state["is_active"]:
            return listing
    return None


def get_editable_need_response_for_responder(*, responder_producer, listing_id):
    if not responder_producer or not listing_id:
        return None

    listing = (
        _get_need_response_listing_queryset()
        .filter(id=listing_id, producer=responder_producer)
        .exclude(need__producer=responder_producer)
        .first()
    )
    if not listing:
        return None

    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids([listing.id])
    state = _derive_need_response_state(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
    )
    if (
        state["is_active"]
        and state["status"] == NeedResponseStatus.PENDING
        and listing.status == ListingStatus.ACTIVE
    ):
        return listing
    return None


def _get_need_response_listing_for_update(listing_id):
    return (
        MarketplaceListing.objects
        # Lock only marketplace_listings. stock, forecast and need are null=True
        # (LEFT JOINs); PostgreSQL rejects FOR UPDATE on the nullable side of an
        # outer join, so of=("self",) is required here.
        .select_for_update(of=("self",))
        .select_related(
            "producer",
            "product",
            "stock",
            "forecast",
            "need",
            "need__producer",
        )
        .filter(need_id__isnull=False)
        .get(id=listing_id)
    )


def _listing_source_label(listing):
    has_stock_source = bool(getattr(listing, "stock_id", None))
    has_forecast_source = bool(getattr(listing, "forecast_id", None))
    if has_forecast_source and not has_stock_source:
        return "forecast", "Pré-venda"
    return "stock", "Disponível agora"


def get_need_response_order_snapshot(listing_ids):
    if not listing_ids:
        return {}

    rows = (
        OrderItem.objects
        .filter(
            listing_id__in=listing_ids,
            need_id__isnull=False,
        )
        .values_list("listing_id", "item_status", "quantity")
    )
    snapshots = {}
    for listing_id, item_status, quantity in rows:
        snapshot = snapshots.setdefault(
            listing_id,
            {
                "item_statuses": [],
                "active_or_completed_quantity": Decimal("0.000"),
                "cancelled_quantity": Decimal("0.000"),
                "completed_quantity": Decimal("0.000"),
                "ordered_quantity": Decimal("0.000"),
                "status": None,
            },
        )
        snapshot["item_statuses"].append(item_status)
        quantity = _quantize_need_quantity(quantity)
        if item_status == OrderItemStatus.CANCELLED:
            snapshot["cancelled_quantity"] = _quantize_need_quantity(snapshot["cancelled_quantity"] + quantity)
        else:
            snapshot["active_or_completed_quantity"] = _quantize_need_quantity(
                snapshot["active_or_completed_quantity"] + quantity
            )
        if item_status == OrderItemStatus.COMPLETED:
            snapshot["completed_quantity"] = _quantize_need_quantity(snapshot["completed_quantity"] + quantity)

    for snapshot in snapshots.values():
        statuses = snapshot["item_statuses"]
        if any(status == OrderItemStatus.COMPLETED for status in statuses):
            snapshot["status"] = NeedResponseStatus.COMPLETED
            snapshot["ordered_quantity"] = snapshot["completed_quantity"]
        elif any(status != OrderItemStatus.CANCELLED for status in statuses):
            snapshot["status"] = NeedResponseStatus.ACCEPTED
            snapshot["ordered_quantity"] = snapshot["active_or_completed_quantity"]
        elif statuses:
            snapshot["status"] = NeedResponseStatus.CANCELLED
            snapshot["ordered_quantity"] = snapshot["cancelled_quantity"]

    return snapshots


def _get_need_response_order_state_listing_ids(listing_ids):
    snapshots = get_need_response_order_snapshot(listing_ids)
    accepted_listing_ids = set()
    cancelled_listing_ids = set()
    completed_listing_ids = set()
    for listing_id, snapshot in snapshots.items():
        if snapshot["status"] == NeedResponseStatus.CANCELLED:
            cancelled_listing_ids.add(listing_id)
        elif snapshot["status"] == NeedResponseStatus.COMPLETED:
            completed_listing_ids.add(listing_id)
        elif snapshot["status"] == NeedResponseStatus.ACCEPTED:
            accepted_listing_ids.add(listing_id)
    return accepted_listing_ids, cancelled_listing_ids, completed_listing_ids


def _get_accepted_need_response_listing_ids(listing_ids):
    accepted_listing_ids, _, completed_listing_ids = _get_need_response_order_state_listing_ids(listing_ids)
    return accepted_listing_ids | completed_listing_ids


def _derive_need_response_state(
    listing,
    *,
    accepted_listing_ids=None,
    cancelled_listing_ids=None,
    completed_listing_ids=None,
    order_snapshots=None,
):
    accepted_listing_ids = accepted_listing_ids or set()
    cancelled_listing_ids = cancelled_listing_ids or set()
    completed_listing_ids = completed_listing_ids or set()
    order_snapshots = order_snapshots or {}
    order_snapshot = order_snapshots.get(listing.id) or {}
    response_status = getattr(listing, "need_response_status", NeedResponseStatus.PENDING)

    if response_status != NeedResponseStatus.REJECTED and order_snapshot.get("status"):
        response_status = order_snapshot["status"]

    if response_status == NeedResponseStatus.COMPLETED or listing.id in completed_listing_ids:
        return {
            "status": NeedResponseStatus.COMPLETED,
            "label": NeedResponseStatus.COMPLETED.label,
            "badge_class": "ok",
            "message": "A encomenda criada a partir desta oferta foi concluída.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.REJECTED:
        return {
            "status": "REJECTED",
            "label": NeedResponseStatus.REJECTED.label,
            "badge_class": "danger",
            "message": "Esta oferta foi rejeitada pelo produtor da necessidade.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.ACCEPTED or listing.id in accepted_listing_ids:
        return {
            "status": NeedResponseStatus.ACCEPTED,
            "label": NeedResponseStatus.ACCEPTED.label,
            "badge_class": "ok",
            "message": "Esta oferta já foi aceite e originou uma encomenda.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.CANCELLED or listing.id in cancelled_listing_ids:
        return {
            "status": NeedResponseStatus.CANCELLED,
            "label": NeedResponseStatus.CANCELLED.label,
            "badge_class": "danger",
            "message": "A encomenda criada a partir desta oferta foi cancelada.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if (
        response_status == NeedResponseStatus.EXPIRED
        or listing.status == ListingStatus.EXPIRED
        or is_listing_effectively_expired(listing)
    ):
        return {
            "status": NeedResponseStatus.EXPIRED,
            "label": NeedResponseStatus.EXPIRED.label,
            "badge_class": "muted",
            "message": "Esta oferta expirou.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if response_status == NeedResponseStatus.WITHDRAWN or listing.status == ListingStatus.CANCELLED:
        return {
            "status": NeedResponseStatus.WITHDRAWN,
            "label": NeedResponseStatus.WITHDRAWN.label,
            "badge_class": "muted",
            "message": "Esta oferta foi retirada.",
            "is_active": False,
            "can_buy": False,
            "can_reject": False,
        }

    if listing.status == ListingStatus.ACTIVE:
        return {
            "status": NeedResponseStatus.PENDING,
            "label": NeedResponseStatus.PENDING.label,
            "badge_class": "warn",
            "message": "Esta oferta aguarda decisão do produtor da necessidade.",
            "is_active": True,
            "can_buy": Decimal(str(listing.quantity_available or 0)) > Decimal("0"),
            "can_reject": True,
        }

    return {
        "status": listing.status,
        "label": listing.get_status_display(),
        "badge_class": "info",
        "message": "Esta oferta já não está no estado inicial.",
        "is_active": False,
        "can_buy": False,
        "can_reject": False,
    }


def _build_need_response(
    listing,
    *,
    accepted_listing_ids=None,
    cancelled_listing_ids=None,
    completed_listing_ids=None,
    order_snapshots=None,
):
    order_snapshots = order_snapshots or {}
    order_snapshot = order_snapshots.get(listing.id) or {}
    source_key, source_label = _listing_source_label(listing)
    state = _derive_need_response_state(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
        order_snapshots=order_snapshots,
    )
    is_editable = bool(
        state["is_active"]
        and state["status"] == NeedResponseStatus.PENDING
        and listing.status == ListingStatus.ACTIVE
    )
    return NeedResponse(
        listing=listing,
        id=listing.id,
        need_id=listing.need_id,
        producer_label=_producer_marketplace_display_name(listing.producer),
        need_owner_label=_producer_marketplace_display_name(getattr(getattr(listing, "need", None), "producer", None)),
        product_name=listing.product.name,
        product_unit=listing.product.unit,
        offered_quantity=_quantize_need_quantity(
            getattr(listing, "quantity_total", getattr(listing, "quantity_available", Decimal("0.000")))
        ),
        available_quantity=_quantize_need_quantity(listing.quantity_available),
        ordered_quantity=_quantize_need_quantity(order_snapshot.get("ordered_quantity") or Decimal("0.000")),
        quantity_available=listing.quantity_available,
        unit_price=listing.unit_price,
        source_key=source_key,
        source_label=source_label,
        status=listing.status,
        status_label=listing.get_status_display(),
        response_status=state["status"],
        response_status_label=state["label"],
        response_badge_class=state["badge_class"],
        response_message=state["message"],
        can_buy=state["can_buy"],
        can_reject=state["can_reject"],
        notes=listing.notes or "",
        detail_url=reverse("marketplace:proposal_detail", args=[listing.id]),
        reject_url=reverse("marketplace:proposal_reject", args=[listing.id]),
        edit_url=reverse("marketplace:proposal_edit", args=[listing.id]) if is_editable else "",
        is_editable=is_editable,
    )


def list_need_responses_for_owner(*, owner_producer, q="", category_id="", need_id=""):
    listings = list(
        _get_need_response_listings_for_owner(
            owner_producer=owner_producer,
            q=q,
            category_id=category_id,
            need_id=need_id,
        )
    )
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    order_snapshots = get_need_response_order_snapshot([listing.id for listing in listings])
    return [
        _build_need_response(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
            order_snapshots=order_snapshots,
        )
        for listing in listings
    ]


def build_need_response_for_listing(listing):
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids([listing.id])
    order_snapshots = get_need_response_order_snapshot([listing.id])
    return _build_need_response(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
        order_snapshots=order_snapshots,
    )


def list_need_responses_for_responder(*, responder_producer, q="", category_id=""):
    if not responder_producer:
        return []

    qs = (
        _get_need_response_listing_queryset()
        .filter(producer=responder_producer)
        .exclude(need__producer=responder_producer)
        .order_by("-published_at", "-created_at")
    )

    if q:
        q = normalize_needs_search_query(q)
        qs = qs.filter(
            Q(product__name__icontains=q)
            | Q(need__producer__display_name__icontains=q)
            | Q(need__producer__company_name__icontains=q)
            | Q(need__producer__user__first_name__icontains=q)
            | Q(need__producer__user__last_name__icontains=q)
            | Q(notes__icontains=q)
            | Q(need__notes__icontains=q)
        )

    if category_id:
        qs = qs.filter(product__category_id=category_id)

    listings = list(qs)
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    order_snapshots = get_need_response_order_snapshot([listing.id for listing in listings])
    return [
        _build_need_response(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
            order_snapshots=order_snapshots,
        )
        for listing in listings
    ]


def get_need_response_summaries_for_responder(*, responder_producer, need_ids):
    if not responder_producer or not need_ids:
        return {}

    listings = list(
        _get_need_response_listing_queryset()
        .filter(producer=responder_producer, need_id__in=need_ids)
        .order_by("need_id", "-published_at", "-created_at")
    )
    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids(
        [listing.id for listing in listings]
    )
    order_snapshots = get_need_response_order_snapshot([listing.id for listing in listings])

    summaries = {}
    for listing in listings:
        need_key = str(listing.need_id)
        if need_key in summaries:
            continue
        state = _derive_need_response_state(
            listing,
            accepted_listing_ids=accepted_listing_ids,
            cancelled_listing_ids=cancelled_listing_ids,
            completed_listing_ids=completed_listing_ids,
            order_snapshots=order_snapshots,
        )
        can_edit = bool(
            state["is_active"]
            and state["status"] == NeedResponseStatus.PENDING
            and listing.status == ListingStatus.ACTIVE
        )
        summaries[need_key] = NeedResponseSummary(
            listing_id=listing.id,
            status=state["status"],
            status_label=state["label"],
            badge_class=state["badge_class"],
            message=state["message"],
            detail_url=reverse("marketplace:proposal_detail", args=[listing.id]),
            is_active=state["is_active"],
            edit_url=reverse("marketplace:proposal_edit", args=[listing.id]) if can_edit else "",
            can_edit=can_edit,
            can_send_new_proposal=state["status"] in {"REJECTED", "CANCELLED", "EXPIRED", "WITHDRAWN", "COMPLETED"},
        )

    return summaries


def _resolve_persisted_need_response_status(listing, order_snapshot):
    current_status = getattr(listing, "need_response_status", NeedResponseStatus.PENDING)
    if current_status == NeedResponseStatus.REJECTED:
        return NeedResponseStatus.REJECTED

    if order_snapshot and order_snapshot.get("status"):
        return order_snapshot["status"]

    if listing.status == ListingStatus.EXPIRED:
        return NeedResponseStatus.EXPIRED
    if listing.status == ListingStatus.CANCELLED:
        return NeedResponseStatus.WITHDRAWN
    return NeedResponseStatus.PENDING


def sync_need_response_status_for_listing(listing):
    if not listing or not getattr(listing, "need_id", None):
        return listing

    order_snapshot = get_need_response_order_snapshot([listing.id]).get(listing.id)
    next_status = _resolve_persisted_need_response_status(listing, order_snapshot)
    if listing.need_response_status != next_status:
        listing.need_response_status = next_status
        if hasattr(listing, "updated_at"):
            listing.updated_at = timezone.now()
            listing.save(update_fields=["need_response_status", "updated_at"])
        else:
            listing.save(update_fields=["need_response_status"])
    return listing


@transaction.atomic
def update_need_response(
    *,
    listing,
    responder_producer,
    quantity,
    unit_price,
    delivery_mode,
    delivery_radius_km=None,
    delivery_fee=None,
    notes=None,
    acting_user=None,
):
    from apps.marketplace.services import MarketplaceServiceError, update_listing

    listing = _get_need_response_listing_for_update(listing.id)
    if not responder_producer or listing.producer_id != responder_producer.id:
        raise ValidationError("Não pode editar esta proposta.")
    if not listing.need or listing.need.producer_id == responder_producer.id:
        raise ValidationError("Proposta inválida para edição.")

    accepted_listing_ids, cancelled_listing_ids, completed_listing_ids = _get_need_response_order_state_listing_ids([listing.id])
    state = _derive_need_response_state(
        listing,
        accepted_listing_ids=accepted_listing_ids,
        cancelled_listing_ids=cancelled_listing_ids,
        completed_listing_ids=completed_listing_ids,
    )
    if not (
        state["is_active"]
        and state["status"] == NeedResponseStatus.PENDING
        and listing.status == ListingStatus.ACTIVE
    ):
        raise ValidationError("Esta proposta já não pode ser editada.")

    try:
        updated_listing = update_listing(
            listing=listing,
            quantity_total=quantity,
            unit_price=unit_price,
            delivery_mode=delivery_mode,
            delivery_radius_km=delivery_radius_km,
            delivery_fee=delivery_fee,
            show_location_on_map=True,
            notes=notes,
            status=ListingStatus.ACTIVE,
            expires_at=None,
            photo_path=listing.photo_path,
            acting_user=acting_user,
        )
    except MarketplaceServiceError as exc:
        raise ValidationError(str(exc)) from exc

    if updated_listing.need_response_status != NeedResponseStatus.PENDING:
        updated_listing.need_response_status = NeedResponseStatus.PENDING
        updated_listing.save(update_fields=["need_response_status"])
    return updated_listing


@transaction.atomic
def reject_need_response(*, listing, owner_producer, acting_user=None):
    listing = _get_need_response_listing_for_update(listing.id)

    if not owner_producer or not listing.need or listing.need.producer_id != owner_producer.id:
        raise ValidationError("Não tem permissão para rejeitar esta oferta.")

    if _get_accepted_need_response_listing_ids([listing.id]):
        raise ValidationError("Esta oferta já foi aceite e não pode ser rejeitada.")

    if listing.need_response_status == NeedResponseStatus.REJECTED and listing.status == ListingStatus.CANCELLED:
        return False

    previous_values = {
        "status": listing.status,
        "need_response_status": listing.need_response_status,
    }
    listing.need_response_status = NeedResponseStatus.REJECTED
    listing.status = ListingStatus.CANCELLED
    listing.updated_at = timezone.now()
    listing.save(update_fields=["need_response_status", "status", "updated_at"])
    log_audit_event(
        actor=acting_user,
        action="NEED_RESPONSE_REJECTED",
        entity_type="marketplace_listings",
        entity_id=listing.id,
        notes="Proposta privada rejeitada pelo produtor que publicou a procura.",
        old_values=previous_values,
        new_values={
            "status": listing.status,
            "need_response_status": listing.need_response_status,
            "need_id": str(listing.need_id),
            "product_id": str(listing.product_id),
            "product_name": getattr(getattr(listing, "product", None), "name", None),
            "producer_id": str(listing.producer_id),
        },
    )
    try:
        from apps.alerts.models import AlertSeverity, AlertType
        from apps.alerts.services import create_need_response_event_alert

        create_need_response_event_alert(
            target_producer=listing.producer,
            listing=listing,
            alert_type=AlertType.OFFER_REJECTED,
            title=f"Oferta rejeitada: {listing.product.name}",
            description="O produtor da necessidade rejeitou a sua proposta. Pode enviar uma nova proposta se fizer sentido.",
            action_url=f"/marketplace/propostas/{listing.id}/",
            action_label="Ver proposta",
            acting_user=getattr(owner_producer, "user", None),
            severity=AlertSeverity.INFO,
            requires_action=False,
        )
    except Exception:
        pass
    return True
