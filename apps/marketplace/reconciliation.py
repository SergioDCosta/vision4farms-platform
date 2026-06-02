from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.common.audit import log_audit_event
from apps.marketplace.audit import listing_audit_values
from apps.marketplace.commands import retire_listing
from apps.marketplace.models import ListingStatus, MarketplaceListing


ZERO = Decimal("0.00")


def _quantize_quantity(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))


def _inspect_stock_reduction_listings(stock, new_quantity):
    """
    Return active listings that would no longer fit after a stock reduction.

    This is the read-only implementation used before committing a lower stock
    quantity.
    """
    new_quantity = _quantize_quantity(new_quantity)
    reserved_quantity = _quantize_quantity(getattr(stock, "reserved_quantity", 0))

    if not stock:
        return {
            "total_published": ZERO,
            "reserved_quantity": reserved_quantity,
            "min_required": reserved_quantity,
            "deficit": ZERO,
            "affected_listings": [],
        }

    listings = list(
        MarketplaceListing.objects
        .filter(
            stock=stock,
            status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
            quantity_available__gt=0,
        )
        .select_related("product", "need", "need__producer")
        .order_by("-quantity_available", "-created_at")
    )

    total_published = _quantize_quantity(
        sum((Decimal(str(listing.quantity_available or 0)) for listing in listings), Decimal("0"))
    )
    min_required = _quantize_quantity(reserved_quantity + total_published)
    deficit = _quantize_quantity(max(min_required - new_quantity, ZERO))

    return {
        "total_published": total_published,
        "reserved_quantity": reserved_quantity,
        "min_required": min_required,
        "deficit": deficit,
        "affected_listings": [
            {
                "listing": listing,
                "quantity_available": _quantize_quantity(listing.quantity_available),
            }
            for listing in listings
        ],
    }


def reconcile_listings_for_stock_reduction(
    stock,
    new_quantity,
    *,
    mode="inspect",
    listing_ids_to_cancel=None,
    acting_user=None,
):
    """
    Inspect or adjust active marketplace listings for a lower stock quantity.

    ``inspect`` returns the listings that would no longer fit.
    ``proportional`` reduces every active listing proportionally.
    ``cancel_selected`` retires selected listings and leaves the rest intact
    whenever the remaining free capacity covers them.
    """
    if mode == "inspect":
        return _inspect_stock_reduction_listings(stock, new_quantity)
    return _apply_stock_reduction_reconciliation(
        stock,
        new_quantity,
        mode=mode,
        listing_ids_to_cancel=listing_ids_to_cancel,
        acting_user=acting_user,
    )


@transaction.atomic
def _apply_stock_reduction_reconciliation(
    stock,
    new_quantity,
    *,
    mode,
    listing_ids_to_cancel=None,
    acting_user=None,
):
    new_quantity = _quantize_quantity(new_quantity)
    reserved_quantity = _quantize_quantity(getattr(stock, "reserved_quantity", 0))
    listings = list(
        MarketplaceListing.objects
        .select_for_update()
        .filter(
            stock=stock,
            status__in=[ListingStatus.ACTIVE, ListingStatus.RESERVED],
            quantity_available__gt=0,
        )
        .select_related("product")
        .order_by("-quantity_available", "-created_at")
    )

    if mode not in {"proportional", "cancel_selected"}:
        raise ValueError(f"Modo de reconciliação desconhecido: {mode}")

    cancelled_ids = []
    if mode == "cancel_selected":
        target_ids = {str(listing_id) for listing_id in (listing_ids_to_cancel or [])}
        remaining = []
        for listing in listings:
            if str(listing.id) in target_ids:
                retire_listing(listing=listing, acting_user=acting_user)
                cancelled_ids.append(str(listing.id))
            else:
                remaining.append(listing)
        listings = remaining

    target_available_total = _quantize_quantity(
        max(new_quantity - reserved_quantity, ZERO)
    )
    current_available_total = _quantize_quantity(
        sum((Decimal(str(listing.quantity_available or 0)) for listing in listings), Decimal("0"))
    )

    reduced_log = []
    if current_available_total <= ZERO or current_available_total <= target_available_total:
        return {"cancelled": cancelled_ids, "reduced": reduced_log}

    ratio = (
        Decimal("0") if target_available_total <= ZERO
        else target_available_total / current_available_total
    )
    running_total = Decimal("0.000")
    for index, listing in enumerate(listings):
        old_values = listing_audit_values(listing)
        old_quantity = _quantize_quantity(listing.quantity_available)
        if target_available_total <= ZERO:
            new_listing_quantity = Decimal("0.000")
        elif index == len(listings) - 1:
            new_listing_quantity = _quantize_quantity(target_available_total - running_total)
        else:
            new_listing_quantity = _quantize_quantity(old_quantity * ratio)
        new_listing_quantity = max(new_listing_quantity, Decimal("0.000"))
        running_total = _quantize_quantity(running_total + new_listing_quantity)

        if new_listing_quantity == old_quantity:
            continue

        listing.quantity_available = new_listing_quantity
        listing.updated_at = timezone.now()
        update_fields = ["quantity_available", "updated_at"]
        if (
            new_listing_quantity <= ZERO
            and _quantize_quantity(listing.quantity_reserved) <= ZERO
            and listing.status == ListingStatus.ACTIVE
        ):
            listing.status = ListingStatus.CLOSED
            update_fields.append("status")
        listing.save(update_fields=update_fields)
        log_audit_event(
            actor=acting_user,
            action="LISTING_AUTO_RECONCILED",
            entity_type="marketplace_listings",
            entity_id=listing.id,
            notes="Anúncio reduzido para caber no novo stock disponível.",
            old_values=old_values,
            new_values=listing_audit_values(listing),
        )
        reduced_log.append(
            {
                "listing_id": str(listing.id),
                "from": str(old_quantity),
                "to": str(new_listing_quantity),
            }
        )

    return {"cancelled": cancelled_ids, "reduced": reduced_log}
