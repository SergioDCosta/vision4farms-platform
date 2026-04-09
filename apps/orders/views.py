from decimal import Decimal, InvalidOperation
from django.http import Http404
from django.contrib import messages
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse

from apps.common.decorators import login_required, client_only_required
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.orders.models import OrderStatus, OrderItemStatus
from apps.orders.services import (
    compute_order_group_status,
    OrderServiceError,
    confirm_order_receipt,
    create_order_from_listing,
    get_buyer_purchase_entries,
    get_current_producer_for_user,
    get_order_group_detail_for_buyer,
    get_order_group_status_label,
    get_order_detail_for_buyer,
    get_order_detail_for_seller,
    get_orders_for_seller,
    get_order_source_label,
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
    orders = []
    purchase_entries = []

    if tab == "recebidas":
        orders = get_orders_for_seller(seller_producer=producer, status=status)
        for order in orders:
            order.order_source_label = get_order_source_label(order)
    else:
        purchase_entries = get_buyer_purchase_entries(buyer_producer=producer, status=status)

    context = {
        "page_title": "Encomendas",
        "orders": orders,
        "purchase_entries": purchase_entries,
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

    force_single = (request.GET.get("force_single") or "").strip() == "1"
    if role == "buyer" and order.group_id and not force_single:
        return redirect("orders:group_detail", group_id=order.group_id)

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
        "order_source_label": get_order_source_label(order),
        "back_to_group_url": (
            reverse("orders:group_detail", kwargs={"group_id": order.group_id})
            if role == "buyer" and order.group_id and force_single
            else None
        ),
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
def order_group_detail_view(request, group_id):
    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    group = get_order_group_detail_for_buyer(buyer_producer=producer, group_id=group_id)
    group_orders = list(group.orders.all())
    aggregated_status = compute_order_group_status([order.status for order in group_orders])

    sub_orders = []
    for order in group_orders:
        items = list(order.items.all())
        first_seller = items[0].seller_producer if items else None
        seller_label = (
            first_seller.display_name
            if first_seller and first_seller.display_name
            else first_seller.company_name
            if first_seller and first_seller.company_name
            else first_seller.user.email
            if first_seller and first_seller.user
            else "Vendedor"
        )

        sub_orders.append(
            {
                "order": order,
                "seller_label": seller_label,
                "source_label": get_order_source_label(order),
                "status_label": order.get_status_display(),
                "item_count": len(items),
                "total_amount": order.total_amount,
                "detail_url": f"{reverse('orders:detail', kwargs={'order_id': order.id})}?force_single=1",
            }
        )

    total_amount = sum((Decimal(str(order.total_amount or 0)) for order in group_orders), Decimal("0.00"))
    total_items = sum(sub["item_count"] for sub in sub_orders)

    context = {
        "page_title": f"Grupo #{group.group_number}",
        "group": group,
        "group_orders": group_orders,
        "group_status": aggregated_status,
        "group_status_label": get_order_group_status_label(aggregated_status),
        "group_total_amount": total_amount,
        "group_total_items": total_items,
        "sub_orders": sub_orders,
    }
    return render(request, "orders/group_detail.html", context)


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
        order_group, order = create_order_from_listing(
            buyer_producer=producer,
            listing=listing,
            quantity=quantity,
            acting_user=request.current_user,
            buyer_notes=buyer_notes,
        )
    except OrderServiceError as exc:
        messages.error(request, str(exc))
        return redirect("marketplace:detail", listing_id=listing.id)

    messages.success(request, f"Encomenda #{order.order_number} criada com sucesso no grupo #{order_group.group_number}.")
    return redirect("orders:group_detail", group_id=order_group.id)


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
