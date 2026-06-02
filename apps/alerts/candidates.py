"""Alert domain services: candidates."""

import logging
from apps.alerts.models import AlertCategory, AlertSeverity, AlertType
from apps.inventory.models import ProductionForecast, Stock
from apps.inventory.services import calculate_inventory_commitment_state
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.marketplace.services import get_forecast_available_quantity, get_max_publishable_quantity
from apps.needs.models import Need, NeedResponseStatus, NeedSourceSystem, NeedStatus
from apps.needs.services import calculate_need_coverage
from apps.orders.models import Order, OrderStatus
from decimal import Decimal
from django.utils import formats, timezone
from apps.alerts.constants import LISTING_EXPIRING_WINDOW, NEED_DEADLINE_WINDOW, ORDER_CONFIRMATION_GRACE, ORDER_DELIVERY_GRACE
from apps.alerts.utils import _as_decimal, _build_context_key, _candidate, _money_label, _quantity_label

logger = logging.getLogger(__name__)


def _stock_commitment_rows(producer):
    rows = []
    stocks = (
        Stock.objects
        .select_related("product")
        .filter(
            producer=producer,
            product__is_active=True,
            product__producer_links__producer=producer,
            product__producer_links__is_active=True,
        )
        .distinct()
    )

    for stock in stocks:
        try:
            commitment_state = calculate_inventory_commitment_state(
                producer,
                stock.product,
                stock=stock,
            )
        except Exception:
            logger.exception(
                "Falha ao calcular estado temporal para alertas producer_id=%s product_id=%s.",
                getattr(producer, "id", None),
                getattr(stock, "product_id", None),
            )
            continue
        rows.append((stock, commitment_state))
    return rows


def _critical_stock_candidates(producer, *, stock_commitment_rows=None):
    rows = []
    if stock_commitment_rows is None:
        stock_commitment_rows = _stock_commitment_rows(producer)

    for stock, commitment_state in stock_commitment_rows:
        if commitment_state.get("max_deficit", Decimal("0.000")) <= Decimal("0.000"):
            continue

        unit = getattr(stock.product, "unit", "") or ""
        available_label = _quantity_label(commitment_state.get("available_stock_now"), unit)
        forecast_label = _quantity_label(commitment_state.get("useful_forecast_total"), unit)
        safety_label = _quantity_label(commitment_state.get("total_external_demand"), unit)
        deficit_label = _quantity_label(commitment_state.get("max_deficit"), unit)
        first_external_deadline = commitment_state.get("first_deficit_date")
        deadline_label = (
            formats.date_format(first_external_deadline, "SHORT_DATE_FORMAT")
            if first_external_deadline
            else None
        )
        rows.append(
            _candidate(
                alert_type=AlertType.CRITICAL_STOCK,
                severity=AlertSeverity.CRITICAL,
                category=AlertCategory.STOCK,
                product=stock.product,
                title=f"Falta produto a tempo: {stock.product.name}",
                description=(
                    f"Faltam {deficit_label} para cumprir pedidos externos"
                    + (f" até {deadline_label}." if deadline_label else ".")
                    + f" Disponível: {available_label} · Previsão útil: {forecast_label} · Necessário: {safety_label}."
                ),
                payload={
                    "available_quantity": str(commitment_state.get("available_stock_now")),
                    "useful_forecast_total": str(commitment_state.get("useful_forecast_total")),
                    "safety_stock": str(commitment_state.get("total_external_demand")),
                    "max_deficit": str(commitment_state.get("max_deficit")),
                    "first_deficit_date": str(first_external_deadline or ""),
                    "action_url": f"/inventario/stock/{stock.product_id}/",
                    "action_label": "Ver detalhe do stock",
                    "secondary_action_url": f"/recomendacoes/?product={stock.product_id}",
                    "secondary_action_label": "Abrir recomendações",
                    "impact_label": f"Falta produto a tempo para cumprir pedidos externos de {stock.product.name}",
                    "reason": "O stock atual e a produção prevista útil não chegam a tempo dos pedidos externos.",
                },
                requires_action=True,
                priority=10,
            )
        )
    return rows


def _surplus_candidates(producer, *, stock_commitment_rows=None):
    rows = []
    if stock_commitment_rows is None:
        stock_commitment_rows = _stock_commitment_rows(producer)

    for stock, commitment_state in stock_commitment_rows:
        try:
            if commitment_state.get("has_external_demands"):
                publishable_quantity = _as_decimal(commitment_state.get("temporal_sellable_quantity"))
            else:
                publishable_quantity = _as_decimal(get_max_publishable_quantity(stock))
        except Exception:
            logger.exception(
                "Falha ao calcular quantidade publicavel para alerta producer_id=%s product_id=%s.",
                getattr(producer, "id", None),
                getattr(stock, "product_id", None),
            )
            continue
        if publishable_quantity <= Decimal("0.000"):
            continue

        total_external_demand = _as_decimal(commitment_state.get("total_external_demand"))
        if total_external_demand > 0 and publishable_quantity <= (total_external_demand * Decimal("0.10")):
            continue

        unit = getattr(stock.product, "unit", "") or ""
        surplus_label = _quantity_label(publishable_quantity, unit)
        rows.append(
            _candidate(
                alert_type=AlertType.SURPLUS_AVAILABLE,
                severity=AlertSeverity.INFO,
                category=AlertCategory.MARKETPLACE,
                product=stock.product,
                title=f"Quantidade disponível para anunciar: {stock.product.name}",
                description=(
                    f"Pode publicar até {surplus_label} sem comprometer pedidos externos "
                    "nem quantidades já anunciadas ou propostas."
                ),
                payload={
                    "real_surplus": str(publishable_quantity),
                    "publishable_quantity": str(publishable_quantity),
                    "useful_forecast_total": str(commitment_state.get("useful_forecast_total")),
                    "total_external_demand": str(total_external_demand),
                    "action_url": (
                        f"/marketplace/publicar/?source=stock&product={stock.product_id}&from=inventory"
                    ),
                    "action_label": "Publicar no marketplace",
                    "reason": "Existe quantidade ainda publicável depois de considerar compromissos e anúncios/propostas ativos.",
                },
                requires_action=False,
                priority=55,
            )
        )
    return rows


def _need_candidates(producer):
    rows = []
    needs = (
        Need.objects
        .select_related("product")
        .filter(
            producer=producer,
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    for need in needs:
        coverage = calculate_need_coverage(need)
        remaining_to_plan = _as_decimal(coverage.get("remaining_to_plan"))

        if need.status == NeedStatus.PARTIALLY_COVERED and remaining_to_plan <= Decimal("0.000"):
            continue

        unit = getattr(need.product, "unit", "") or ""
        remaining_label = _quantity_label(remaining_to_plan, unit)
        is_customer_demand = getattr(need, "source_system", None) == NeedSourceSystem.CUSTOMER_DEMAND
        if is_customer_demand:
            commitment_state = calculate_inventory_commitment_state(
                producer,
                need.product,
            )
            if _as_decimal(commitment_state.get("max_deficit")) > Decimal("0.000"):
                continue
        rows.append(
            _candidate(
                alert_type=AlertType.NEED_UNDERCOVERED,
                severity=AlertSeverity.WARNING,
                category=AlertCategory.NEEDS,
                product=need.product,
                need=need,
                title=(
                    f"Procura de clientes por cobrir: {need.product.name}"
                    if is_customer_demand
                    else f"Necessidade por cobrir: {need.product.name}"
                ),
                description=(
                    f"Faltam {remaining_label} para cumprir pedidos externos de clientes."
                    if is_customer_demand
                    else f"Em falta para planear: {remaining_label}."
                ),
                payload={
                    "required_quantity": str(coverage.get("required_quantity")),
                    "planned_qty": str(coverage.get("planned_qty")),
                    "completed_qty": str(coverage.get("completed_qty")),
                    "remaining_to_plan": str(remaining_to_plan),
                    "action_url": f"/necessidades/?need={need.id}",
                    "action_label": "Ver necessidade",
                    "secondary_action_url": f"/recomendacoes/?product={need.product_id}",
                    "secondary_action_label": "Abrir recomendações",
                    "reason": (
                        "A procura gerada por pedidos externos ainda não tem quantidade suficiente planeada."
                        if is_customer_demand
                        else "A necessidade ainda não tem quantidade suficiente planeada."
                    ),
                },
                requires_action=True,
                priority=30,
            )
        )
    return rows


def _sell_suggestion_candidates(producer):
    rows = []
    forecasts = (
        ProductionForecast.objects
        .select_related("product")
        .filter(
            producer=producer,
            is_marketplace_enabled=True,
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    for forecast in forecasts:
        saleable = _as_decimal(get_forecast_available_quantity(forecast))
        if saleable <= Decimal("0.000"):
            continue

        unit = getattr(forecast.product, "unit", "") or ""
        saleable_label = _quantity_label(saleable, unit)
        rows.append(
            _candidate(
                alert_type=AlertType.SELL_SUGGESTION,
                severity=AlertSeverity.INFO,
                category=AlertCategory.MARKETPLACE,
                product=forecast.product,
                forecast=forecast,
                title="Pré-venda disponível para publicar",
                description=(
                    f"{forecast.product.name}: {saleable_label} disponíveis para pré-venda."
                ),
                payload={
                    "saleable_quantity": str(saleable),
                    "action_url": (
                        f"/marketplace/publicar/?source=forecast&product={forecast.product_id}&forecast={forecast.id}"
                    ),
                    "action_label": "Publicar pré-venda",
                    "reason": "Existe produção futura marcada como disponível para marketplace.",
                },
                requires_action=False,
                priority=60,
            )
        )
    return rows


def _need_response_candidates(producer):
    rows = []
    listings = (
        MarketplaceListing.objects
        .select_related("producer", "producer__user", "product", "need", "forecast")
        .filter(
            need__producer=producer,
            need_response_status=NeedResponseStatus.PENDING,
            status=ListingStatus.ACTIVE,
            quantity_available__gt=0,
        )
        .filter(order_items__isnull=True)
        .order_by("-published_at", "-created_at")
        .distinct()
    )

    for listing in listings:
        unit = getattr(listing.product, "unit", "") or ""
        quantity_label = _quantity_label(listing.quantity_available, unit)
        price_label = _money_label(listing.unit_price)
        producer_label = (
            getattr(listing.producer, "display_name", None)
            or getattr(listing.producer, "company_name", None)
            or "Outro produtor"
        )
        rows.append(
            _candidate(
                alert_type=AlertType.NEED_RESPONSE_RECEIVED,
                severity=AlertSeverity.WARNING,
                category=AlertCategory.NEEDS,
                product=listing.product,
                need=listing.need,
                forecast=getattr(listing, "forecast", None),
                listing=listing,
                title=f"Nova oferta para {listing.product.name}",
                description=(
                    f"{producer_label} ofereceu {quantity_label} "
                    f"a {price_label}/{unit}".rstrip("/")
                ),
                payload={
                    "listing_id": str(listing.id),
                    "need_id": str(listing.need_id),
                    "quantity_available": str(listing.quantity_available),
                    "unit_price": str(listing.unit_price),
                    "action_url": f"/marketplace/propostas/{listing.id}/",
                    "action_label": "Ver oferta",
                    "secondary_action_url": f"/necessidades/?need={listing.need_id}",
                    "secondary_action_label": "Ver necessidade",
                    "reason": "Um produtor respondeu a uma necessidade sua.",
                },
                requires_action=True,
                priority=18,
            )
        )
    return rows


def _need_deadline_candidates(producer):
    rows = []
    now = timezone.now()
    deadline_limit = now + NEED_DEADLINE_WINDOW
    needs = (
        Need.objects
        .select_related("product")
        .filter(
            producer=producer,
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            needed_by_date__isnull=False,
            needed_by_date__lte=deadline_limit,
            product__is_active=True,
        )
        .order_by("needed_by_date", "-updated_at")
    )

    for need in needs:
        coverage = calculate_need_coverage(need)
        remaining_to_receive = _as_decimal(coverage.get("remaining_to_receive"))
        if remaining_to_receive <= Decimal("0.000"):
            continue

        unit = getattr(need.product, "unit", "") or ""
        remaining_label = _quantity_label(remaining_to_receive, unit)
        is_overdue = need.needed_by_date and need.needed_by_date <= now
        rows.append(
            _candidate(
                alert_type=AlertType.NEED_DEADLINE_APPROACHING,
                severity=AlertSeverity.CRITICAL if is_overdue else AlertSeverity.WARNING,
                category=AlertCategory.NEEDS,
                product=need.product,
                need=need,
                title=(
                    f"Prazo ultrapassado: {need.product.name}"
                    if is_overdue
                    else f"Prazo próximo para necessidade: {need.product.name}"
                ),
                description=f"Ainda faltam receber {remaining_label}.",
                payload={
                    "remaining_to_receive": str(remaining_to_receive),
                    "needed_by_date": need.needed_by_date.isoformat() if need.needed_by_date else "",
                    "action_url": f"/necessidades/?need={need.id}",
                    "action_label": "Ver necessidade",
                    "secondary_action_url": f"/recomendacoes/?product={need.product_id}",
                    "secondary_action_label": "Abrir recomendações",
                    "reason": "O prazo da necessidade está próximo e a quantidade ainda não foi recebida.",
                },
                requires_action=True,
                due_at=need.needed_by_date,
                priority=12 if is_overdue else 22,
            )
        )
    return rows


def _buy_opportunity_candidates(producer):
    rows = []
    needs = (
        Need.objects
        .select_related("product")
        .filter(
            producer=producer,
            status__in=[NeedStatus.OPEN, NeedStatus.PARTIALLY_COVERED],
            product__is_active=True,
        )
        .order_by("-updated_at", "-created_at")
    )

    for need in needs:
        coverage = calculate_need_coverage(need)
        remaining_to_receive = _as_decimal(coverage.get("remaining_to_receive"))
        if remaining_to_receive <= Decimal("0.000"):
            continue

        matching_listings = (
            MarketplaceListing.objects
            .filter(
                product=need.product,
                status=ListingStatus.ACTIVE,
                quantity_available__gt=0,
                need_id__isnull=True,
            )
            .exclude(producer=producer)
            .order_by("unit_price", "-published_at")
        )
        first_listing = matching_listings.first()
        if not first_listing:
            continue

        total_available = Decimal("0.000")
        count = 0
        for listing in matching_listings.only("quantity_available"):
            total_available += _as_decimal(listing.quantity_available)
            count += 1

        unit = getattr(need.product, "unit", "") or ""
        available_label = _quantity_label(total_available, unit)
        remaining_label = _quantity_label(remaining_to_receive, unit)
        rows.append(
            _candidate(
                alert_type=AlertType.BUY_OPPORTUNITY,
                severity=AlertSeverity.INFO,
                category=AlertCategory.MARKETPLACE,
                product=need.product,
                need=need,
                listing=first_listing,
                context_key=_build_context_key(AlertType.BUY_OPPORTUNITY, need_id=need.id),
                title=f"Oportunidade para cobrir {need.product.name}",
                description=(
                    f"Existem {available_label} disponíveis no marketplace "
                    f"para uma necessidade com {remaining_label} por receber."
                ),
                payload={
                    "remaining_to_receive": str(remaining_to_receive),
                    "available_quantity": str(total_available),
                    "matching_listings_count": str(count),
                    "action_url": f"/recomendacoes/?product={need.product_id}",
                    "action_label": "Ver recomendações",
                    "secondary_action_url": f"/marketplace/?q={need.product.name}",
                    "secondary_action_label": "Ver marketplace",
                    "reason": "Há ofertas públicas que podem ajudar a cobrir uma necessidade sua.",
                },
                requires_action=False,
                priority=45,
            )
        )
    return rows


def _order_confirmation_candidates(producer):
    rows = []
    now = timezone.now()
    orders = (
        Order.objects
        .filter(
            items__seller_producer=producer,
            status=OrderStatus.PENDING,
        )
        .order_by("created_at")
        .distinct()
    )

    for order in orders:
        due_at = order.created_at + ORDER_CONFIRMATION_GRACE if order.created_at else None
        is_overdue = bool(due_at and due_at <= now)
        rows.append(
            _candidate(
                alert_type=AlertType.ORDER_REQUIRES_CONFIRMATION,
                severity=AlertSeverity.CRITICAL if is_overdue else AlertSeverity.WARNING,
                category=AlertCategory.ORDERS,
                title=f"Encomenda #{order.order_number} por confirmar",
                description=(
                    "A encomenda já ultrapassou o tempo recomendado para confirmação."
                    if is_overdue
                    else "Tem uma encomenda recebida a aguardar confirmação."
                ),
                payload={
                    "order_id": str(order.id),
                    "order_number": order.order_number,
                    "action_url": f"/encomendas/{order.id}/",
                    "action_label": "Gerir encomenda",
                    "reason": "O vendedor deve confirmar, preparar ou cancelar a encomenda.",
                },
                context_key=f"{AlertType.ORDER_REQUIRES_CONFIRMATION}:order:{order.id}",
                requires_action=True,
                due_at=due_at,
                priority=14 if is_overdue else 24,
            )
        )
    return rows


def _order_delivery_overdue_candidates(producer):
    rows = []
    now = timezone.now()
    cutoff = now - ORDER_DELIVERY_GRACE
    orders = (
        Order.objects
        .filter(
            buyer_producer=producer,
            status=OrderStatus.DELIVERING,
            updated_at__lte=cutoff,
        )
        .order_by("updated_at")
    )

    for order in orders:
        rows.append(
            _candidate(
                alert_type=AlertType.ORDER_DELIVERY_OVERDUE,
                severity=AlertSeverity.WARNING,
                category=AlertCategory.ORDERS,
                title=f"Entrega por confirmar na encomenda #{order.order_number}",
                description="A encomenda está em entrega há vários dias. Confirme receção ou contacte o produtor.",
                payload={
                    "order_id": str(order.id),
                    "order_number": order.order_number,
                    "action_url": f"/encomendas/{order.id}/?force_single=1",
                    "action_label": "Ver encomenda",
                    "secondary_action_url": f"/mensagens/encomenda/{order.id}/iniciar/",
                    "secondary_action_label": "Contactar produtor",
                    "reason": "A encomenda está em entrega há mais tempo do que o esperado.",
                },
                context_key=f"{AlertType.ORDER_DELIVERY_OVERDUE}:order:{order.id}",
                requires_action=True,
                due_at=order.updated_at + ORDER_DELIVERY_GRACE if order.updated_at else None,
                priority=16,
            )
        )
    return rows


def _listing_expiring_candidates(producer):
    rows = []
    now = timezone.now()
    cutoff = now + LISTING_EXPIRING_WINDOW
    listings = (
        MarketplaceListing.objects
        .select_related("product", "forecast", "stock")
        .filter(
            producer=producer,
            status=ListingStatus.ACTIVE,
            need_id__isnull=True,
            expires_at__isnull=False,
            expires_at__gt=now,
            expires_at__lte=cutoff,
        )
        .order_by("expires_at", "-updated_at")
    )

    for listing in listings:
        rows.append(
            _candidate(
                alert_type=AlertType.LISTING_EXPIRING_SOON,
                severity=AlertSeverity.WARNING,
                category=AlertCategory.MARKETPLACE,
                product=listing.product,
                forecast=getattr(listing, "forecast", None),
                listing=listing,
                title=f"Anúncio a expirar: {listing.product.name}",
                description="Este anúncio termina em breve. Reveja a oferta se ainda estiver disponível.",
                payload={
                    "listing_id": str(listing.id),
                    "expires_at": listing.expires_at.isoformat() if listing.expires_at else "",
                    "action_url": f"/marketplace/{listing.id}/editar/",
                    "action_label": "Rever anúncio",
                    "secondary_action_url": f"/marketplace/{listing.id}/",
                    "secondary_action_label": "Ver detalhe",
                    "reason": "A data de expiração do anúncio está próxima.",
                },
                requires_action=False,
                due_at=listing.expires_at,
                expires_at=listing.expires_at,
                priority=50,
            )
        )
    return rows


def _candidate_rows(producer):
    rows = []
    stock_commitment_rows = _stock_commitment_rows(producer)
    rows.extend(_critical_stock_candidates(producer, stock_commitment_rows=stock_commitment_rows))
    rows.extend(_surplus_candidates(producer, stock_commitment_rows=stock_commitment_rows))
    rows.extend(_need_candidates(producer))
    rows.extend(_need_response_candidates(producer))
    rows.extend(_need_deadline_candidates(producer))
    rows.extend(_buy_opportunity_candidates(producer))
    rows.extend(_sell_suggestion_candidates(producer))
    rows.extend(_order_confirmation_candidates(producer))
    rows.extend(_order_delivery_overdue_candidates(producer))
    rows.extend(_listing_expiring_candidates(producer))
    return rows
