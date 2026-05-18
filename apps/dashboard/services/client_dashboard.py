from datetime import timedelta

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.alerts.models import Alert, AlertSeverity, AlertStatus
from apps.dashboard.models import AuditLog
from apps.dashboard.services.weather import get_dashboard_weather_snapshot
from apps.inventory.models import ProducerProfile, Stock
from apps.inventory.services import producer_has_active_inventory_products
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.orders.models import DeliveryMethod, Order, OrderStatus


def build_client_dashboard_context(user):
    producer = ProducerProfile.objects.select_related("user").get(user_id=user.id)
    has_active_inventory_products = producer_has_active_inventory_products(producer)

    active_alerts_qs = Alert.objects.filter(
        producer=producer,
        status=AlertStatus.ACTIVE,
    )
    active_alerts_count = active_alerts_qs.count()
    critical_alerts_count = active_alerts_qs.filter(
        severity=AlertSeverity.CRITICAL
    ).count()

    available_qty_expr = models.ExpressionWrapper(
        models.F("current_quantity") - models.F("reserved_quantity"),
        output_field=models.DecimalField(max_digits=14, decimal_places=3),
    )
    real_surplus_expr = models.ExpressionWrapper(
        available_qty_expr - models.F("safety_stock"),
        output_field=models.DecimalField(max_digits=14, decimal_places=3),
    )

    stocks_with_state = (
        Stock.objects.select_related("product")
        .filter(producer=producer)
        .annotate(
            available_quantity_calc=available_qty_expr,
            real_surplus_calc=real_surplus_expr,
        )
    )
    critical_stock_qs = stocks_with_state.filter(
        available_quantity_calc__lte=models.F("safety_stock"),
    ).order_by("available_quantity_calc")
    critical_stock_count = critical_stock_qs.count()

    pending_orders_qs = Order.objects.filter(
        buyer_producer=producer,
        status__in=[
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
            OrderStatus.IN_PROGRESS,
            OrderStatus.DELIVERING,
        ],
    ).order_by("-created_at")
    pending_orders_count = pending_orders_qs.count()

    active_listings_qs = MarketplaceListing.objects.select_related("product").filter(
        producer=producer,
        status=ListingStatus.ACTIVE,
    ).order_by("-created_at")
    surplus_listings_count = active_listings_qs.count()

    listed_product_ids = active_listings_qs.values_list("product_id", flat=True)
    surplus_stock_candidate = (
        stocks_with_state.filter(
            available_quantity_calc__gt=models.F("safety_stock"),
            real_surplus_calc__gte=models.F("surplus_threshold"),
        )
        .exclude(product_id__in=listed_product_ids)
        .order_by("-real_surplus_calc", "-available_quantity_calc")
        .first()
    )

    recommended_actions = _build_recommended_actions(
        critical_alerts_count=critical_alerts_count,
        critical_stock_count=critical_stock_count,
        critical_stock_qs=critical_stock_qs,
        pending_orders_count=pending_orders_count,
        pending_orders_qs=pending_orders_qs,
        surplus_listings_count=surplus_listings_count,
        surplus_stock_candidate=surplus_stock_candidate,
    )

    return {
        "producer": producer,
        "active_alerts_count": active_alerts_count,
        "critical_alerts_count": critical_alerts_count,
        "critical_stock_count": critical_stock_count,
        "pending_orders_count": pending_orders_count,
        "surplus_listings_count": surplus_listings_count,
        "has_active_inventory_products": has_active_inventory_products,
        "priority_alerts": active_alerts_qs.order_by("-created_at")[:3],
        "recommended_actions": recommended_actions,
        "low_stock_preview": critical_stock_qs[:3],
        "recent_activity": AuditLog.objects.filter(user=user).order_by("-created_at")[:5],
    }


def _build_recommended_actions(
    *,
    critical_alerts_count,
    critical_stock_count,
    critical_stock_qs,
    pending_orders_count,
    pending_orders_qs,
    surplus_listings_count,
    surplus_stock_candidate,
):
    actions = []

    if critical_alerts_count > 0:
        actions.append(
            {
                "variant": "danger",
                "icon": "exclamation-triangle-fill",
                "title": "Resolver alertas críticos",
                "description": (
                    f"Tem {critical_alerts_count} alerta(s) crítico(s) que exigem atenção imediata."
                ),
                "url": "/alertas/",
                "button_label": "Ver alertas",
            }
        )

    if critical_stock_count > 0:
        low_stock = critical_stock_qs.first()
        if low_stock and low_stock.product:
            actions.append(
                {
                    "variant": "warning",
                    "icon": "boxes",
                    "title": f"Reforçar stock de {low_stock.product.name}",
                    "description": (
                        f"Stock atual: {low_stock.current_quantity} {low_stock.product.unit} | "
                        f"Stock de segurança: {low_stock.safety_stock} {low_stock.product.unit}"
                    ),
                    "url": "/inventario/produtos/?tab=stock",
                    "button_label": "Ver stocks",
                }
            )

    if pending_orders_count > 0:
        latest_order = pending_orders_qs.first()
        actions.append(
            {
                "variant": "primary",
                "icon": "truck",
                "title": "Acompanhar encomendas pendentes",
                "description": (
                    f"Tem {pending_orders_count} encomenda(s) em aberto. "
                    f"Última encomenda: #{latest_order.order_number}"
                    if latest_order
                    else f"Tem {pending_orders_count} encomenda(s) em aberto."
                ),
                "url": "/encomendas/",
                "button_label": "Ver encomendas",
            }
        )

    if surplus_stock_candidate and surplus_listings_count == 0:
        actions.append(
            {
                "variant": "success",
                "icon": "shop",
                "title": "Publicar um possível excedente",
                "description": (
                    f"O produto {surplus_stock_candidate.product.name} parece ter stock acima "
                    "do stock de segurança e ainda não está anunciado no marketplace."
                ),
                "url": "/marketplace/",
                "button_label": "Ir ao marketplace",
            }
        )

    if not actions:
        actions.append(
            {
                "variant": "secondary",
                "icon": "check-circle",
                "title": "Tudo controlado",
                "description": (
                    "Não existem ações urgentes neste momento. Continue a acompanhar o seu painel."
                ),
                "url": "/inventario/produtos/?tab=stock",
                "button_label": "Ver stocks",
            }
        )

    return actions


def build_weather_card_context(user):
    producer = ProducerProfile.objects.only("id", "city", "district").get(user_id=user.id)
    weather_needs_location = not bool((producer.city or "").strip() or (producer.district or "").strip())
    weather = get_dashboard_weather_snapshot(city=producer.city, district=producer.district)

    active_operations_qs = (
        Order.objects.filter(
            Q(buyer_producer=producer) | Q(items__seller_producer=producer),
            status__in=[OrderStatus.IN_PROGRESS, OrderStatus.DELIVERING],
        )
        .distinct()
    )
    active_delivery_orders_count = active_operations_qs.count()
    active_delivery_or_mixed_exists = active_operations_qs.filter(
        delivery_method__in=[DeliveryMethod.DELIVERY, DeliveryMethod.MIXED]
    ).exists()

    today = timezone.localdate()
    presale_window_end = today + timedelta(days=3)
    presale_starting_soon_count = MarketplaceListing.objects.filter(
        producer=producer,
        status=ListingStatus.ACTIVE,
        forecast_id__isnull=False,
        forecast__period_start__isnull=False,
        forecast__period_start__date__gte=today,
        forecast__period_start__date__lte=presale_window_end,
    ).count()

    return {
        "weather": weather,
        "weather_state": weather.get("state", "degraded"),
        "active_delivery_orders_count": active_delivery_orders_count,
        "presale_starting_soon_count": presale_starting_soon_count,
        "weather_needs_location": weather_needs_location,
        "weather_operational_summary": build_weather_operational_summary(
            weather=weather,
            active_delivery_orders_count=active_delivery_orders_count,
            active_delivery_or_mixed_exists=active_delivery_or_mixed_exists,
            presale_starting_soon_count=presale_starting_soon_count,
        ),
        "weather_operational_hints": build_weather_operational_hints(
            weather=weather,
            active_delivery_orders_count=active_delivery_orders_count,
            active_delivery_or_mixed_exists=active_delivery_or_mixed_exists,
            presale_starting_soon_count=presale_starting_soon_count,
        ),
        "weather_actions": build_weather_quick_actions(
            active_delivery_orders_count=active_delivery_orders_count,
            presale_starting_soon_count=presale_starting_soon_count,
        ),
    }


def build_weather_operational_summary(
    *,
    weather,
    active_delivery_orders_count,
    active_delivery_or_mixed_exists,
    presale_starting_soon_count,
):
    if weather.get("state") != "success":
        return {
            "key": "unknown",
            "icon": "bi-cloud-slash",
            "label": "Sem análise operacional",
            "description": "A previsão IPMA não está disponível para cruzar com a operação.",
        }

    daily_forecast = weather.get("daily_forecast") or []
    today = next((day for day in daily_forecast if day.get("is_today")), None)
    tomorrow = next((day for day in daily_forecast if day.get("offset_days") == 1), None)
    today_wet_risk = bool(today and today.get("is_wet_risk"))
    tomorrow_wet_risk = bool(tomorrow and tomorrow.get("is_wet_risk"))
    has_weather_risk = today_wet_risk or tomorrow_wet_risk
    has_active_work = active_delivery_orders_count > 0 or presale_starting_soon_count > 0

    if has_weather_risk and active_delivery_or_mixed_exists:
        return {
            "key": "risk",
            "icon": "bi-cloud-rain-heavy",
            "label": "Atenção à logística",
            "description": "Há risco de chuva a cruzar com entregas em curso.",
        }

    if has_weather_risk:
        return {
            "key": "watch",
            "icon": "bi-cloud-rain",
            "label": "Monitorizar chuva",
            "description": "A previsão indica chuva hoje ou amanhã.",
        }

    if has_active_work:
        return {
            "key": "good",
            "icon": "bi-check2-circle",
            "label": "Condições favoráveis",
            "description": "Sem risco meteorológico relevante para a operação registada.",
        }

    return {
        "key": "neutral",
        "icon": "bi-brightness-alt-high",
        "label": "Sem pressão operacional",
        "description": "Não há entregas ou pré-vendas próximas a cruzar com a previsão.",
    }


def build_weather_operational_hints(
    *,
    weather,
    active_delivery_orders_count,
    active_delivery_or_mixed_exists,
    presale_starting_soon_count,
):
    hints = []
    if weather.get("state") != "success":
        return hints

    daily_forecast = weather.get("daily_forecast") or []
    tomorrow = next((day for day in daily_forecast if day.get("offset_days") == 1), None)
    tomorrow_wet_risk = bool(tomorrow and tomorrow.get("is_wet_risk"))

    if active_delivery_orders_count > 0:
        if tomorrow_wet_risk and active_delivery_or_mixed_exists:
            hints.append(
                "Risco de chuva amanhã: priorize levantamentos e confirme janelas de entrega."
            )
        elif active_delivery_or_mixed_exists:
            hints.append("Janela favorável para entregas em curso nas próximas 24h.")
        else:
            hints.append(
                "Tens encomendas em curso: valida disponibilidade e comunicação com a contraparte."
            )

    if presale_starting_soon_count > 0:
        hints.append(
            "Pré-venda a iniciar em breve: valida disponibilidade e logística com antecedência."
        )

    return hints


def build_weather_quick_actions(*, active_delivery_orders_count, presale_starting_soon_count):
    actions = []

    if active_delivery_orders_count > 0:
        actions.append(
            {
                "label": "Ver encomendas",
                "url": "/encomendas/?status=DELIVERING",
                "style": "primary",
                "icon": "bi-truck",
            }
        )

    if presale_starting_soon_count > 0:
        actions.append(
            {
                "label": "Ver pré-vendas",
                "url": "/encomendas/?tab=pre_vendas",
                "style": "ghost",
                "icon": "bi-calendar-week",
            }
        )

    return actions
