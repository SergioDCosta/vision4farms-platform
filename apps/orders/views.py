from decimal import Decimal, InvalidOperation
from django.http import Http404
from django.contrib import messages
from django.shortcuts import redirect, render, get_object_or_404

from apps.common.decorators import login_required, client_only_required
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.orders.models import OrderStatus, OrderItemStatus
from apps.orders.services import (
    OrderServiceError,
    confirm_order_receipt,
    create_order_from_listing,
    get_current_producer_for_user,
    get_order_detail_for_buyer,
    get_order_detail_for_seller,
    get_orders_for_buyer,
    get_orders_for_seller,
    seller_update_order_status,
)


def _is_orders_panel_request(request):
    return request.headers.get("HX-Request") == "true" and request.headers.get("HX-Target") == "orders-panel"


@login_required
@client_only_required
def orders_index_view(request):
    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    tab = (request.GET.get("tab") or "compras").strip()
    if tab not in {"compras", "recebidas"}:
        tab = "compras"

    status = (request.GET.get("status") or "").strip()

    if tab == "recebidas":
        orders = get_orders_for_seller(seller_producer=producer, status=status)
    else:
        orders = get_orders_for_buyer(buyer_producer=producer, status=status)

    context = {
        "page_title": "Encomendas",
        "orders": orders,
        "selected_status": status,
        "selected_tab": tab,
        "status_choices": OrderStatus.choices,
    }

    if _is_orders_panel_request(request):
        return render(request, "orders/partials/orders_panel.html", context)

    return render(request, "orders/index.html", context)

@login_required
@client_only_required
def order_detail_view(request, order_id):
    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    try:
        order = get_order_detail_for_buyer(buyer_producer=producer, order_id=order_id)
        role = "buyer"
    except Http404:
        order = get_order_detail_for_seller(seller_producer=producer, order_id=order_id)
        role = "seller"

    seller_items = list(order.items.filter(seller_producer=producer)) if role == "seller" else []
    active_seller_items = [item for item in seller_items if item.item_status != OrderItemStatus.CANCELLED]
    active_order_items = list(order.items.exclude(item_status=OrderItemStatus.CANCELLED))

    can_confirm_receipt = (
        role == "buyer"
        and order.status == OrderStatus.DELIVERING
        and active_order_items
        and all(item.item_status in {OrderItemStatus.IN_DELIVERY, OrderItemStatus.COMPLETED} for item in active_order_items)
    )

    can_seller_confirm = (
        role == "seller"
        and order.status not in {OrderStatus.COMPLETED, OrderStatus.CANCELLED}
        and any(item.item_status == OrderItemStatus.PENDING for item in active_seller_items)
    )
    can_seller_start = (
        role == "seller"
        and order.status in {OrderStatus.CONFIRMED, OrderStatus.IN_PROGRESS}
        and active_seller_items
        and not any(item.item_status == OrderItemStatus.PENDING for item in active_seller_items)
        and any(item.item_status == OrderItemStatus.CONFIRMED for item in active_seller_items)
    )
    can_seller_deliver = (
        role == "seller"
        and order.status in {OrderStatus.IN_PROGRESS, OrderStatus.DELIVERING}
        and active_seller_items
        and not any(item.item_status == OrderItemStatus.PENDING for item in active_seller_items)
        and any(item.item_status == OrderItemStatus.CONFIRMED for item in active_seller_items)
    )
    can_seller_cancel = (
        role == "seller"
        and order.status not in {OrderStatus.COMPLETED, OrderStatus.CANCELLED}
        and any(item.item_status not in {OrderItemStatus.CANCELLED, OrderItemStatus.COMPLETED} for item in seller_items)
    )

    context = {
        "page_title": f"Encomenda #{order.order_number}",
        "order": order,
        "order_role": role,
        "seller_items": seller_items,
        "can_confirm_receipt": can_confirm_receipt,
        "can_seller_confirm": can_seller_confirm,
        "can_seller_start": can_seller_start,
        "can_seller_deliver": can_seller_deliver,
        "can_seller_cancel": can_seller_cancel,
    }
    return render(request, "orders/detail.html", context)

@login_required
@client_only_required
def create_order_from_listing_view(request, listing_id):
    if request.method != "POST":
        return redirect("marketplace:detail", listing_id=listing_id)

    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    listing = get_object_or_404(
        MarketplaceListing.objects.select_related("product", "producer", "stock", "forecast"),
        id=listing_id,
        status=ListingStatus.ACTIVE,
    )

    quantity_raw = (request.POST.get("quantity") or request.POST.get("qty") or "").strip()
    buyer_notes = (request.POST.get("buyer_notes") or "").strip()

    try:
        quantity = Decimal(quantity_raw)
    except (InvalidOperation, TypeError):
        messages.error(request, "Quantidade inválida.")
        return redirect("marketplace:detail", listing_id=listing.id)

    try:
        order = create_order_from_listing(
            buyer_producer=producer,
            listing=listing,
            quantity=quantity,
            acting_user=request.current_user,
            buyer_notes=buyer_notes,
        )
    except OrderServiceError as exc:
        messages.error(request, str(exc))
        return redirect("marketplace:detail", listing_id=listing.id)

    messages.success(request, f"Encomenda #{order.order_number} criada com sucesso.")
    return redirect("orders:detail", order_id=order.id)


@login_required
@client_only_required
def confirm_order_receipt_view(request, order_id):
    if request.method != "POST":
        return redirect("orders:detail", order_id=order_id)

    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    order = get_order_detail_for_buyer(buyer_producer=producer, order_id=order_id)

    try:
        confirm_order_receipt(order=order, acting_user=request.current_user)
    except OrderServiceError as exc:
        messages.error(request, str(exc))
        return redirect("orders:detail", order_id=order.id)

    messages.success(request, "Receção da encomenda confirmada com sucesso.")
    return redirect("orders:detail", order_id=order.id)


@login_required
@client_only_required
def seller_update_order_status_view(request, order_id, status):
    if request.method != "POST":
        return redirect("orders:detail", order_id=order_id)

    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    order = get_order_detail_for_seller(seller_producer=producer, order_id=order_id)

    notes = (request.POST.get("notes") or "").strip()

    if status == OrderStatus.CANCELLED:
        cancel_reason = (request.POST.get("cancel_reason") or "").strip()
        parts = []
        if cancel_reason:
            parts.append(f"Motivo: {cancel_reason}")
        if notes:
            parts.append(notes)
        notes = " | ".join(parts) if parts else "Pedido cancelado pelo vendedor."

    try:
        seller_update_order_status(
            order=order,
            seller_producer=producer,
            new_status=status,
            acting_user=request.current_user,
            notes=notes,
        )
    except OrderServiceError as exc:
        messages.error(request, str(exc))
        return redirect("orders:detail", order_id=order.id)

    messages.success(request, "Estado da encomenda atualizado com sucesso.")
    return redirect("orders:detail", order_id=order.id)
