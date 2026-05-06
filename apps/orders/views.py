from decimal import Decimal, InvalidOperation
from django.http import Http404
from django.contrib import messages
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse

from apps.common.decorators import login_required, client_only_required
from apps.needs.models import NeedResponseStatus, NeedStatus
from apps.needs.services import get_need_for_producer
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.orders.models import OrderStatus, OrderItemStatus
from apps.orders.models import OrderItem
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
    get_presale_order_entries_for_producer,
    get_order_source_label,
    is_order_from_need_response,
    is_order_forecast_only,
    build_presale_timeline_context,
    seller_update_order_status,
)


def _is_orders_panel_request(request):
    return request.headers.get("HX-Request") == "true" and request.headers.get("HX-Target") == "orders-panel"


def _is_presale_purchase_entry(entry):
    if not entry:
        return False

    if entry.get("kind") == "group":
        orders = list(entry.get("orders") or [])
        return bool(orders) and all(is_order_forecast_only(order) for order in orders)

    order = entry.get("order")
    return bool(order and is_order_forecast_only(order))


@login_required
@client_only_required
def orders_index_view(request):
    producer = get_current_producer_for_user(request.current_user)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    tab = (request.GET.get("tab") or "compras").strip()
    if tab not in {"compras", "recebidas", "pre_vendas"}:
        tab = "compras"

    status = (request.GET.get("status") or "").strip()
    orders = []
    purchase_entries = []
    presale_buyer_orders = []
    presale_seller_orders = []

    if tab == "recebidas":
        all_orders = list(get_orders_for_seller(seller_producer=producer, status=status))
        orders = [order for order in all_orders if not is_order_forecast_only(order)]
        for order in orders:
            order.order_source_label = get_order_source_label(order)
    elif tab == "pre_vendas":
        presale_entries = get_presale_order_entries_for_producer(
            producer=producer,
            status=status,
        )
        presale_buyer_orders = presale_entries["buyer_entries"]
        presale_seller_orders = presale_entries["seller_entries"]
        for entry in [*presale_buyer_orders, *presale_seller_orders]:
            order = entry["order"]
            detail_url = reverse("orders:detail", kwargs={"order_id": order.id})
            if entry["viewer_role"] == "buyer":
                detail_url = f"{detail_url}?force_single=1"
            entry["detail_url"] = detail_url
    else:
        all_purchase_entries = get_buyer_purchase_entries(buyer_producer=producer, status=status)
        purchase_entries = [
            entry
            for entry in all_purchase_entries
            if not _is_presale_purchase_entry(entry)
        ]

    context = {
        "page_title": "Encomendas",
        "orders": orders,
        "purchase_entries": purchase_entries,
        "presale_buyer_orders": presale_buyer_orders,
        "presale_seller_orders": presale_seller_orders,
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
    has_pending_seller_items = any(item.item_status == OrderItemStatus.PENDING for item in active_seller_items)
    has_confirmed_seller_items = any(item.item_status == OrderItemStatus.CONFIRMED for item in active_seller_items)
    seller_has_started = (
        role == "seller"
        and any(
            event.status == OrderStatus.IN_PROGRESS and event.changed_by_id == request.current_user.id
            for event in order.status_history.all()
        )
    )

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
        and not has_pending_seller_items
        and has_confirmed_seller_items
        and not seller_has_started
    )
    can_seller_deliver = (
        role == "seller"
        and active_seller_items
        and not has_pending_seller_items
        and has_confirmed_seller_items
        and (seller_has_started or order.status in {OrderStatus.IN_PROGRESS, OrderStatus.DELIVERING})
    )
    can_seller_cancel = (
        role == "seller"
        and order.status not in {OrderStatus.COMPLETED, OrderStatus.CANCELLED}
        and any(item.item_status not in {OrderItemStatus.CANCELLED, OrderItemStatus.COMPLETED} for item in seller_items)
    )
    is_presale_order = is_order_forecast_only(order)
    is_need_response_order = is_order_from_need_response(order)
    presale_timeline = build_presale_timeline_context(order) if is_presale_order else None

    context = {
        "page_title": f"Encomenda #{order.order_number}",
        "order": order,
        "order_role": role,
        "order_source_label": get_order_source_label(order),
        "is_need_response_order": is_need_response_order,
        "is_presale_order": is_presale_order,
        "back_to_orders_url": (
            f"{reverse('orders:index')}?tab=pre_vendas"
            if is_presale_order
            else f"{reverse('orders:index')}?tab=recebidas"
            if role == "seller"
            else reverse("orders:index")
        ),
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
        "presale_timeline_steps": presale_timeline["steps"] if presale_timeline else [],
        "presale_timeline_state": presale_timeline["state"] if presale_timeline else "normal",
        "presale_timeline_cancelled": presale_timeline["cancelled"] if presale_timeline else False,
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
        active_order_items = [item for item in items if item.item_status != OrderItemStatus.CANCELLED]
        can_confirm_receipt = (
            order.status == OrderStatus.DELIVERING
            and active_order_items
            and all(item.item_status in {OrderItemStatus.IN_DELIVERY, OrderItemStatus.COMPLETED} for item in active_order_items)
        )
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
                "status_key": str(order.status).lower(),
                "status_label": order.get_status_display(),
                "item_count": len(items),
                "total_amount": order.total_amount,
                "detail_url": f"{reverse('orders:detail', kwargs={'order_id': order.id})}?force_single=1",
                "can_confirm_receipt": can_confirm_receipt,
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
        MarketplaceListing.objects.select_related("product", "producer", "stock", "forecast", "need", "need__producer"),
        id=listing_id,
        status=ListingStatus.ACTIVE,
    )

    quantity_raw = (request.POST.get("quantity") or request.POST.get("qty") or "").strip()
    buyer_notes = (request.POST.get("buyer_notes") or "").strip()
    need_id = (request.POST.get("need_id") or "").strip()

    need = None
    if listing.need_id:
        if listing.need_response_status == NeedResponseStatus.REJECTED:
            messages.error(request, "Esta oferta foi rejeitada e já não pode ser comprada.")
            return redirect("needs:response_detail", listing_id=listing.id)
        if OrderItem.objects.filter(listing_id=listing.id, need_id=listing.need_id).exists():
            messages.error(request, "Esta oferta já originou uma encomenda e não pode ser comprada novamente.")
            return redirect("needs:response_detail", listing_id=listing.id)
        if not listing.need or listing.need.producer_id != producer.id:
            messages.error(request, "Esta oferta é dirigida ao produtor da necessidade e não está disponível para esta conta.")
            return redirect("marketplace:index")
        need = listing.need
    elif need_id:
        need = get_need_for_producer(producer=producer, need_id=need_id)
        if not need:
            messages.error(request, "Necessidade inválida para associar a esta compra.")
            return redirect("marketplace:detail", listing_id=listing.id)
        if need.status == NeedStatus.IGNORED:
            messages.error(request, "Esta necessidade já foi ignorada e não pode ser associada.")
            return redirect("marketplace:detail", listing_id=listing.id)
        if need.product_id != listing.product_id:
            messages.error(request, "A necessidade selecionada não corresponde ao produto deste anúncio.")
            return redirect("marketplace:detail", listing_id=listing.id)

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
            need=need,
        )
    except OrderServiceError as exc:
        messages.error(request, str(exc))
        return redirect("marketplace:detail", listing_id=listing.id)

    if is_order_forecast_only(order):
        messages.success(
            request,
            (
                f"Pré-venda #{order.order_number} criada com sucesso no grupo "
                f"#{order_group.group_number}."
            ),
        )
        return redirect(f"{reverse('orders:index')}?tab=pre_vendas")

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
    next_url = (request.POST.get("next") or "").strip()

    def _redirect_after_action():
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect("orders:detail", order_id=order.id)

    try:
        confirm_order_receipt(order=order, acting_user=request.current_user)
    except OrderServiceError as exc:
        messages.error(request, str(exc))
        return _redirect_after_action()

    messages.success(request, "Receção da encomenda confirmada com sucesso.")
    return _redirect_after_action()


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
