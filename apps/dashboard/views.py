import unicodedata
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import models, transaction, IntegrityError
from django.db.models import Q, Sum, Count
from django.db.models.functions import Cast, TruncWeek
from django.db.models.deletion import ProtectedError, RestrictedError
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.common.audit import describe_user_agent, get_client_ip
from apps.common.decorators import admin_required, client_only_required
from apps.common.htmx import with_htmx_toast
from apps.accounts.models import (
    User,
    UserRole,
    RegistrationSource,
    AccountStatus,
    AccountVerificationToken,
    VerificationPurpose,
)
from apps.accounts.services import send_admin_invite_email, create_admin_invite_token
from apps.inventory.models import ProducerProfile, ProducerProduct, Stock
from apps.alerts.models import Alert, AlertStatus, AlertSeverity
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.orders.models import Order, OrderStatus, OrderItem, OrderItemStatus, OrderSourceType, DeliveryMethod
from apps.catalog.models import Product, ProductCategory
from apps.catalog.forms import AdminCategoryForm, AdminProductForm
from apps.catalog.services import (
    CatalogValidationError,
    category_snapshot,
    create_category,
    create_product,
    product_snapshot,
    update_category,
    update_product,
)
from apps.dashboard.models import AuditLog
from apps.dashboard.forms import AdminUserCreateForm
from apps.dashboard.services.weather import get_dashboard_weather_snapshot

def _log_admin_action(request, action, entity_type, entity_id=None, notes=None, old_values=None, new_values=None):
    AuditLog.objects.create(
        user=request.current_user,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_values=old_values,
        new_values=new_values,
        ip_address=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT"),
        notes=notes,
    )


AUDIT_ACTION_LABELS = {
    "USER_LOGIN": "Iniciou sessão",
    "USER_PROFILE_UPDATED": "Alterou dados da conta",
    "USER_PRODUCER_PROFILE_UPDATED": "Alterou perfil de produtor",
    "USER_PREFERENCES_UPDATED": "Alterou preferências",
    "USER_PROFILE_PHOTO_REMOVED": "Removeu foto de perfil",
    "USER_PASSWORD_CHANGED": "Alterou palavra-passe",
    "USER_PASSWORD_RESET_COMPLETED": "Redefiniu palavra-passe",
    "USER_INVITED": "Convite criado",
    "USER_EMAIL_CONFIRMED_BY_ADMIN": "Email confirmado por admin",
    "USER_STATUS_UPDATED": "Estado alterado",
    "USER_SUSPENDED": "Utilizador suspenso",
    "USER_REACTIVATED": "Utilizador reativado",
    "SUPPORT_TICKET_CREATED": "Pedido de suporte criado",
    "SUPPORT_TICKET_UPDATED": "Pedido de suporte atualizado",
    "SUPPORT_TICKET_CLAIMED": "Pedido de suporte assumido",
    "SUPPORT_TICKET_REPLIED": "Resposta ao suporte",
    "SUPPORT_TICKET_CLOSED": "Pedido de suporte fechado",
}

AUDIT_FIELD_LABELS = {
    "first_name": "Primeiro nome",
    "last_name": "Último nome",
    "email": "Email",
    "role": "Perfil",
    "registration_source": "Origem de registo",
    "account_status": "Estado da conta",
    "email_verified_at": "Email verificado em",
    "is_active": "Conta ativa",
    "is_staff": "Staff",
    "company_name": "Empresa",
    "display_name": "Nome público",
    "phone": "Telefone",
    "nif": "NIF",
    "address_line": "Morada",
    "postal_code": "Código postal",
    "city": "Cidade",
    "district": "Distrito",
    "user_type": "Tipo de produtor",
    "alerts_in_app": "Alertas na app",
    "alerts_email": "Alertas por email",
    "alerts_sms": "Alertas por SMS",
    "profile_photo": "Foto de perfil",
    "remember_me": "Sessão persistente",
    "password_changed": "Palavra-passe",
    "password_reset_completed": "Recuperação de palavra-passe",
    "sessions_invalidated": "Sessões terminadas",
}


def _display_audit_value(value):
    if value is None:
        return "—"
    if value is True:
        return "Sim"
    if value is False:
        return "Não"
    if isinstance(value, dict):
        return value.get("label") or "—"
    return str(value)


def _audit_change_rows(log):
    old_values = log.old_values or {}
    new_values = log.new_values or {}
    if not isinstance(old_values, dict) or not isinstance(new_values, dict):
        return []
    ignored_keys = {"id", "device"}
    rows = []

    for key in sorted((set(old_values) | set(new_values)) - ignored_keys):
        old_value = old_values.get(key)
        new_value = new_values.get(key)
        if old_value == new_value:
            continue
        rows.append(
            {
                "label": AUDIT_FIELD_LABELS.get(key, key.replace("_", " ").title()),
                "old": _display_audit_value(old_value),
                "new": _display_audit_value(new_value),
            }
        )

    return rows


def _actor_label(user):
    if not user:
        return "Sistema"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return full_name or user.email or "Utilizador"


def _build_user_activity_rows(logs):
    rows = []
    for log in logs:
        device = describe_user_agent(log.user_agent)
        rows.append(
            {
                "log": log,
                "action_label": AUDIT_ACTION_LABELS.get(log.action, log.action),
                "changes": _audit_change_rows(log),
                "device_label": device["label"] if log.user_agent else "—",
                "actor_label": _actor_label(log.user),
            }
        )
    return rows


def _normalize_search_text(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char)).lower()
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def _audit_action_matches(query):
    query = _normalize_search_text(query)
    return [
        action
        for action, label in AUDIT_ACTION_LABELS.items()
        if query in _normalize_search_text(action) or query in _normalize_search_text(label)
    ]


def _audit_field_matches(query):
    query = _normalize_search_text(query)
    return [
        field
        for field, label in AUDIT_FIELD_LABELS.items()
        if query in _normalize_search_text(field) or query in _normalize_search_text(label)
    ]


def _audit_device_terms(query):
    query = _normalize_search_text(query)
    device_map = {
        "telemovel": ["mobile", "iphone", "android"],
        "mobile": ["mobile", "iphone", "android"],
        "tablet": ["tablet", "ipad"],
        "computador": ["windows", "macintosh", "linux", "x11"],
        "desktop": ["windows", "macintosh", "linux", "x11"],
        "chrome": ["chrome"],
        "edge": ["edg/", "edge/"],
        "firefox": ["firefox"],
        "safari": ["safari"],
        "windows": ["windows"],
        "mac": ["macintosh", "mac os"],
        "android": ["android"],
        "ios": ["iphone", "ipad"],
        "linux": ["linux"],
    }
    terms = []
    for label, user_agent_terms in device_map.items():
        if query in label:
            terms.extend(user_agent_terms)
    return terms


def _audit_per_page(value):
    try:
        per_page = int(value)
    except (TypeError, ValueError):
        return 25
    return per_page if per_page in {10, 25, 50} else 25


def _user_snapshot(user, producer_profile=None):
    return {
        "id": str(user.id),
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "role": user.role,
        "registration_source": user.registration_source,
        "account_status": user.account_status,
        "email_verified_at": user.email_verified_at.isoformat() if user.email_verified_at else None,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "company_name": producer_profile.company_name if producer_profile else None,
        "user_type": getattr(producer_profile, "user_type", None) if producer_profile else None,
    }

def _htmx_target(request):
    return (request.headers.get("HX-Target") or "").lstrip("#")


def _get_admin_products_queryset(q=""):
    products = (
        Product.objects
        .select_related("category")
        .annotate(
            active_producers_count=Count(
                "producer_links",
                filter=Q(producer_links__is_active=True),
                distinct=True,
            ),
            producers_count=Count("producer_links", distinct=True),
        )
        .order_by("name")
    )

    if q:
        products = products.filter(
            Q(name__icontains=q)
            | Q(slug__icontains=q)
            | Q(unit__icontains=q)
            | Q(category__name__icontains=q)
        )

    return products


def _build_weather_operational_hints(
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
            hints.append(
                "Janela favorável para entregas em curso nas próximas 24h."
            )
        else:
            hints.append(
                "Tens encomendas em curso: valida disponibilidade e comunicação com a contraparte."
            )

    if presale_starting_soon_count > 0:
        hints.append(
            "Pré-venda a iniciar em breve: valida disponibilidade e logística com antecedência."
        )

    return hints


def _build_weather_quick_actions(*, active_delivery_orders_count, presale_starting_soon_count):
    actions = []

    if active_delivery_orders_count > 0:
        actions.append(
            {
                "label": "Ver encomendas",
                "url": "/encomendas/?status=DELIVERING",
                "style": "primary",
            }
        )

    if presale_starting_soon_count > 0:
        actions.append(
            {
                "label": "Ver pré-vendas",
                "url": "/encomendas/?tab=pre_vendas",
                "style": "ghost",
            }
        )

    actions.append(
        {
            "label": "Abrir marketplace",
            "url": "/marketplace/",
            "style": "ghost",
        }
    )

    return actions


@client_only_required
def dashboard_view(request):
    user = request.current_user

    try:
        producer = ProducerProfile.objects.select_related("user").get(user_id=user.id)
    except ProducerProfile.DoesNotExist:
        request.session.flush()
        return redirect("accounts:login")

    active_alerts_qs = Alert.objects.filter(
        producer=producer,
        status=AlertStatus.ACTIVE,
    )

    active_alerts_count = active_alerts_qs.count()

    critical_alerts_qs = active_alerts_qs.filter(
        severity=AlertSeverity.CRITICAL
    )
    critical_alerts_count = critical_alerts_qs.count()

    available_qty_expr = models.ExpressionWrapper(
        models.F("current_quantity") - models.F("reserved_quantity"),
        output_field=models.DecimalField(max_digits=14, decimal_places=3),
    )
    real_surplus_expr = models.ExpressionWrapper(
        available_qty_expr - models.F("safety_stock"),
        output_field=models.DecimalField(max_digits=14, decimal_places=3),
    )

    stocks_with_state = (
        Stock.objects
        .select_related("product")
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

    priority_alerts = active_alerts_qs.order_by("-created_at")[:3]
    low_stock_preview = critical_stock_qs[:3]
    recent_activity = AuditLog.objects.filter(user=user).order_by("-created_at")[:5]

    listed_product_ids = active_listings_qs.values_list("product_id", flat=True)

    surplus_stock_candidate = (
        stocks_with_state
        .filter(
            available_quantity_calc__gt=models.F("safety_stock"),
            real_surplus_calc__gte=models.F("surplus_threshold"),
        )
        .exclude(product_id__in=listed_product_ids)
        .order_by("-real_surplus_calc", "-available_quantity_calc")
        .first()
    )

    recommended_actions = []

    if critical_alerts_count > 0:
        recommended_actions.append({
            "variant": "danger",
            "icon": "exclamation-triangle-fill",
            "title": "Resolver alertas críticos",
            "description": f"Tem {critical_alerts_count} alerta(s) crítico(s) que exigem atenção imediata.",
            "url": "/alertas/",
            "button_label": "Ver alertas",
        })

    if critical_stock_count > 0:
        low_stock = critical_stock_qs.first()
        if low_stock and low_stock.product:
            recommended_actions.append({
                "variant": "warning",
                "icon": "boxes",
                "title": f"Reforçar stock de {low_stock.product.name}",
                "description": (
                    f"Stock atual: {low_stock.current_quantity} {low_stock.product.unit} | "
                    f"Stock de segurança: {low_stock.safety_stock} {low_stock.product.unit}"
                ),
                "url": "/inventario/produtos/?tab=stock",
                "button_label": "Ver stocks",
            })

    if pending_orders_count > 0:
        latest_order = pending_orders_qs.first()
        recommended_actions.append({
            "variant": "primary",
            "icon": "truck",
            "title": "Acompanhar encomendas pendentes",
            "description": (
                f"Tem {pending_orders_count} encomenda(s) em aberto. "
                f"Última encomenda: #{latest_order.order_number}" if latest_order else
                f"Tem {pending_orders_count} encomenda(s) em aberto."
            ),
            "url": "/encomendas/",
            "button_label": "Ver encomendas",
        })

    if surplus_stock_candidate and surplus_listings_count == 0:
        recommended_actions.append({
            "variant": "success",
            "icon": "shop",
            "title": "Publicar um possível excedente",
            "description": (
                f"O produto {surplus_stock_candidate.product.name} parece ter stock acima do stock de segurança "
                f"e ainda não está anunciado no marketplace."
            ),
            "url": "/marketplace/",
            "button_label": "Ir ao marketplace",
        })

    if not recommended_actions:
        recommended_actions.append({
            "variant": "secondary",
            "icon": "check-circle",
            "title": "Tudo controlado",
            "description": "Não existem ações urgentes neste momento. Continue a acompanhar o seu painel.",
            "url": "/inventario/produtos/?tab=stock",
            "button_label": "Ver stocks",
        })

    context = {
        "producer": producer,
        "active_alerts_count": active_alerts_count,
        "critical_alerts_count": critical_alerts_count,
        "critical_stock_count": critical_stock_count,
        "pending_orders_count": pending_orders_count,
        "surplus_listings_count": surplus_listings_count,
        "priority_alerts": priority_alerts,
        "recommended_actions": recommended_actions,
        "low_stock_preview": low_stock_preview,
        "recent_activity": recent_activity,
    }
    return render(request, "dashboard/painel.html", context)


@client_only_required
def dashboard_weather_card_view(request):
    user = request.current_user

    try:
        producer = ProducerProfile.objects.only(
            "id",
            "city",
            "district",
        ).get(user_id=user.id)
    except ProducerProfile.DoesNotExist:
        request.session.flush()
        return redirect("accounts:login")

    weather = get_dashboard_weather_snapshot(
        city=producer.city,
        district=producer.district,
    )

    active_operations_qs = (
        Order.objects
        .filter(
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
    presale_starting_soon_count = (
        MarketplaceListing.objects
        .filter(
            producer=producer,
            status=ListingStatus.ACTIVE,
            forecast_id__isnull=False,
            forecast__period_start__isnull=False,
            forecast__period_start__date__gte=today,
            forecast__period_start__date__lte=presale_window_end,
        )
        .count()
    )

    weather_operational_hints = _build_weather_operational_hints(
        weather=weather,
        active_delivery_orders_count=active_delivery_orders_count,
        active_delivery_or_mixed_exists=active_delivery_or_mixed_exists,
        presale_starting_soon_count=presale_starting_soon_count,
    )
    weather_actions = _build_weather_quick_actions(
        active_delivery_orders_count=active_delivery_orders_count,
        presale_starting_soon_count=presale_starting_soon_count,
    )

    context = {
        "weather": weather,
        "weather_state": weather.get("state", "degraded"),
        "active_delivery_orders_count": active_delivery_orders_count,
        "presale_starting_soon_count": presale_starting_soon_count,
        "weather_operational_hints": weather_operational_hints,
        "weather_actions": weather_actions,
    }
    return render(request, "dashboard/partials/weather_card.html", context)


@admin_required
def admin_dashboard_view(request):
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    chart_start = week_start - timedelta(weeks=11)
    source_types = [OrderSourceType.MARKETPLACE, OrderSourceType.RECOMMENDATION]

    active_listings_count = MarketplaceListing.objects.filter(
        status=ListingStatus.ACTIVE
    ).count()

    monthly_orders_qs = Order.objects.filter(
        created_at__gte=month_start,
        source_type__in=source_types,
    )
    monthly_orders_count = monthly_orders_qs.count()
    monthly_volume = monthly_orders_qs.aggregate(
        total=Sum("total_amount")
    )["total"] or Decimal("0.00")

    active_users_qs = User.objects.filter(
        is_active=True,
        account_status=AccountStatus.ACTIVE,
    )
    active_users_count = active_users_qs.count()
    online_threshold = now - timedelta(minutes=15)
    online_users_count = active_users_qs.filter(last_login__gte=online_threshold).count()
    offline_users_count = max(active_users_count - online_users_count, 0)

    critical_alerts_count = Alert.objects.filter(
        severity=AlertSeverity.CRITICAL,
        status=AlertStatus.ACTIVE,
    ).count()

    completed_items_qs = OrderItem.objects.filter(
        order__source_type__in=source_types,
        item_status=OrderItemStatus.COMPLETED,
        updated_at__gte=chart_start,
    )
    category_rows = list(
        completed_items_qs
        .values("product__category_id", "product__category__name")
        .annotate(total_qty=Sum("quantity"))
        .order_by("-total_qty")
    )

    palette = ["#16a34a", "#2563eb", "#d97706", "#7c3aed", "#0ea5e9", "#94a3b8"]
    category_pie_slices = []
    max_pie_slices = 5
    top_rows = category_rows[:max_pie_slices]
    remaining_rows = category_rows[max_pie_slices:]
    if remaining_rows:
        others_qty = sum((Decimal(str(row.get("total_qty") or 0)) for row in remaining_rows), Decimal("0"))
        if others_qty > 0:
            top_rows.append(
                {
                    "product__category_id": None,
                    "product__category__name": "Outras categorias",
                    "total_qty": others_qty,
                }
            )

    pie_total = sum((Decimal(str(row.get("total_qty") or 0)) for row in top_rows), Decimal("0"))
    pie_cursor = Decimal("0")
    pie_segments = []
    for idx, row in enumerate(top_rows):
        quantity = Decimal(str(row.get("total_qty") or 0))
        if quantity <= 0:
            continue

        category_label = row.get("product__category__name") or "Sem categoria"
        percentage = (quantity / pie_total * Decimal("100")) if pie_total > 0 else Decimal("0")
        start_pct = pie_cursor
        end_pct = pie_cursor + percentage
        color = palette[idx % len(palette)]
        pie_segments.append(f"{color} {start_pct:.2f}% {end_pct:.2f}%")
        pie_cursor = end_pct

        category_pie_slices.append(
            {
                "label": category_label,
                "quantity": quantity,
                "percentage": float(percentage),
                "color": color,
            }
        )

    category_pie_gradient = f"conic-gradient({', '.join(pie_segments)})" if pie_segments else None
    top_category_row = category_rows[0] if category_rows else None
    top_category_label = (top_category_row.get("product__category__name") or "Sem categoria") if top_category_row else None
    top_category_total_qty = Decimal(str(top_category_row.get("total_qty") or 0)) if top_category_row else Decimal("0")
    top_category_id = top_category_row.get("product__category_id") if top_category_row else None

    top_category_products = []
    if top_category_row is not None:
        top_products_qs = completed_items_qs
        if top_category_id:
            top_products_qs = top_products_qs.filter(product__category_id=top_category_id)
        else:
            top_products_qs = top_products_qs.filter(product__category__isnull=True)

        top_category_products = list(
            top_products_qs
            .values("product_id", "product__name", "product__unit")
            .annotate(total_qty=Sum("quantity"))
            .order_by("-total_qty", "product__name")[:5]
        )

    purchases_by_week = {
        row["week"]: row["total"]
        for row in (
            Order.objects
            .filter(created_at__gte=chart_start, source_type__in=source_types)
            .annotate(week=TruncWeek("created_at"))
            .values("week")
            .annotate(total=Count("id"))
        )
    }
    sales_by_week = {
        row["week"]: row["total"]
        for row in (
            OrderItem.objects
            .filter(
                updated_at__gte=chart_start,
                item_status=OrderItemStatus.COMPLETED,
                order__source_type__in=source_types,
            )
            .annotate(week=TruncWeek("updated_at"))
            .values("week")
            .annotate(total=Count("id"))
        )
    }

    weekly_market_points = []
    for idx in range(12):
        week_ref = chart_start + timedelta(weeks=idx)
        purchase_count = int(purchases_by_week.get(week_ref, 0) or 0)
        sales_count = int(sales_by_week.get(week_ref, 0) or 0)
        weekly_market_points.append(
            {
                "label": week_ref.strftime("%d/%m"),
                "purchases": purchase_count,
                "sales": sales_count,
            }
        )

    max_weekly_value = max(
        [max(point["purchases"], point["sales"]) for point in weekly_market_points] or [0]
    )
    for point in weekly_market_points:
        if max_weekly_value > 0:
            point["purchases_height"] = round((point["purchases"] / max_weekly_value) * 100, 2)
            point["sales_height"] = round((point["sales"] / max_weekly_value) * 100, 2)
        else:
            point["purchases_height"] = 0
            point["sales_height"] = 0

    weekly_purchases_total = sum(point["purchases"] for point in weekly_market_points)
    weekly_sales_total = sum(point["sales"] for point in weekly_market_points)

    recent_alerts = Alert.objects.select_related("producer", "product").order_by("-created_at")[:5]
    recent_users = User.objects.order_by("-created_at")[:5]

    context = {
        "admin_tab": "dashboard",
        "active_listings_count": active_listings_count,
        "monthly_orders_count": monthly_orders_count,
        "monthly_volume": monthly_volume,
        "critical_alerts_count": critical_alerts_count,
        "active_users_count": active_users_count,
        "online_users_count": online_users_count,
        "offline_users_count": offline_users_count,
        "category_pie_slices": category_pie_slices,
        "category_pie_gradient": category_pie_gradient,
        "top_category_label": top_category_label,
        "top_category_total_qty": top_category_total_qty,
        "top_category_products": top_category_products,
        "weekly_market_points": weekly_market_points,
        "weekly_purchases_total": weekly_purchases_total,
        "weekly_sales_total": weekly_sales_total,
        "recent_alerts": recent_alerts,
        "recent_users": recent_users,
    }
    return render(request, "dashboard/admin/dashboard.html", context)


@admin_required
def admin_products_view(request):
    q = request.GET.get("q", "").strip()
    products = _get_admin_products_queryset(q=q)

    context = {
        "admin_tab": "produtos",
        "products": products,
        "q": q,
    }

    if request.htmx and _htmx_target(request) == "products-table":
        return render(request, "dashboard/admin/partials/products_table.html", context)

    return render(request, "dashboard/admin/products.html", context)

@admin_required
def admin_product_detail_view(request, product_id):
    product = get_object_or_404(
        Product.objects.select_related("category"),
        id=product_id,
    )

    producer_links = (
        ProducerProduct.objects
        .filter(product=product, is_active=True)
        .select_related("producer", "producer__user")
        .order_by("producer__display_name", "producer__company_name")
    )

    stocks_by_producer_id = {
        stock.producer_id: stock
        for stock in Stock.objects.filter(product=product).select_related("producer")
    }

    producer_rows = []
    for link in producer_links:
        producer_rows.append({
            "link": link,
            "producer": link.producer,
            "stock": stocks_by_producer_id.get(link.producer_id),
        })

    context = {
        "admin_tab": "produtos",
        "product_obj": product,
        "producer_rows": producer_rows,
        "active_producers_count": len(producer_rows),
        "can_hard_delete": not ProducerProduct.objects.filter(product=product).exists(),
    }
    return render(request, "dashboard/admin/product_detail.html", context)


@admin_required
def admin_product_create_view(request):
    form = AdminProductForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            product = create_product(
                category=form.cleaned_data["category"],
                name=form.cleaned_data["name"],
                unit=form.cleaned_data["unit"],
                description=form.cleaned_data.get("description"),
                is_active=form.cleaned_data["is_active"],
            )
        except CatalogValidationError as exc:
            form.add_error(exc.field, exc.message)
        else:
            _log_admin_action(
                request=request,
                action="PRODUCT_CREATED",
                entity_type="products",
                entity_id=product.id,
                notes=f"Administrador criou o produto {product.name}.",
                new_values=product_snapshot(product),
            )

            messages.success(request, "Produto criado com sucesso.")
            return redirect("dashboard:gestor_produto_detalhe", product_id=product.id)

    context = {
        "admin_tab": "produtos",
        "form": form,
        "page_title": "Novo Produto",
        "submit_label": "Criar produto",
        "is_create": True,
    }
    return render(request, "dashboard/admin/product_form.html", context)


@admin_required
def admin_product_update_view(request, product_id):
    product = get_object_or_404(Product.objects.select_related("category"), id=product_id)

    if request.method == "POST":
        form = AdminProductForm(request.POST, product=product)
        if form.is_valid():
            old_snapshot = product_snapshot(product)
            try:
                product, changed_fields = update_product(
                    product=product,
                    category=form.cleaned_data["category"],
                    name=form.cleaned_data["name"],
                    unit=form.cleaned_data["unit"],
                    description=form.cleaned_data.get("description"),
                    is_active=form.cleaned_data["is_active"],
                )
            except CatalogValidationError as exc:
                form.add_error(exc.field, exc.message)
            else:
                if changed_fields:
                    _log_admin_action(
                        request=request,
                        action="PRODUCT_UPDATED",
                        entity_type="products",
                        entity_id=product.id,
                        notes=f"Administrador atualizou o produto {product.name}.",
                        old_values=old_snapshot,
                        new_values=product_snapshot(product),
                    )

                    messages.success(request, "Produto atualizado com sucesso.")
                else:
                    messages.info(request, "Não foram detetadas alterações.")

                return redirect("dashboard:gestor_produto_detalhe", product_id=product.id)
    else:
        form = AdminProductForm(
            product=product,
            initial={
                "category": product.category,
                "name": product.name,
                "unit": product.unit,
                "description": product.description,
                "is_active": product.is_active,
            },
        )

    context = {
        "admin_tab": "produtos",
        "form": form,
        "product_obj": product,
        "page_title": f"Editar Produto — {product.name}",
        "submit_label": "Guardar alterações",
        "is_create": False,
    }
    return render(request, "dashboard/admin/product_form.html", context)


@admin_required
@require_POST
def admin_product_delete_view(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    q = request.POST.get("q", "").strip()
    next_url = request.POST.get("next")

    has_associated_producers = ProducerProduct.objects.filter(product=product).exists()
    if has_associated_producers:
        error_msg = (
            "Este produto já está associado a produtores. "
            "Só pode ser desativado, não removido."
        )

        if request.htmx:
            context = {
                "admin_tab": "produtos",
                "products": _get_admin_products_queryset(q=q),
                "q": q,
            }
            response = render(request, "dashboard/admin/partials/products_table.html", context)
            return with_htmx_toast(response, "error", error_msg)

        messages.error(request, error_msg)
        if next_url:
            return redirect(next_url)
        return redirect("dashboard:gestor_produto_detalhe", product_id=product.id)

    product_name = product.name
    old_snapshot = product_snapshot(product)

    try:
        with transaction.atomic():
            product.delete()
    except (ProtectedError, RestrictedError, IntegrityError):
        error_msg = (
            "Não foi possível remover este produto porque existem registos "
            "relacionados. Pode desativá-lo em vez de remover."
        )

        if request.htmx:
            context = {
                "admin_tab": "produtos",
                "products": _get_admin_products_queryset(q=q),
                "q": q,
            }
            response = render(request, "dashboard/admin/partials/products_table.html", context)
            return with_htmx_toast(response, "error", error_msg)

        messages.error(request, error_msg)
        if next_url:
            return redirect(next_url)
        return redirect("dashboard:gestor_produto_detalhe", product_id=product_id)

    _log_admin_action(
        request=request,
        action="PRODUCT_DELETED",
        entity_type="products",
        entity_id=product_id,
        notes=f"Administrador removeu o produto {product_name}.",
        old_values=old_snapshot,
        new_values=None,
    )

    success_msg = f"Produto {product_name} removido com sucesso."

    if request.htmx:
        context = {
            "admin_tab": "produtos",
            "products": _get_admin_products_queryset(q=q),
            "q": q,
        }
        response = render(request, "dashboard/admin/partials/products_table.html", context)
        return with_htmx_toast(response, "success", success_msg)

    messages.success(request, success_msg)
    if next_url:
        return redirect(next_url)
    return redirect("dashboard:gestor_produtos")


@admin_required
def admin_categories_view(request):
    q = request.GET.get("q", "").strip()

    categories = ProductCategory.objects.order_by("name")

    if q:
        categories = categories.filter(
            Q(name__icontains=q)
            | Q(slug__icontains=q)
        )

    context = {
        "admin_tab": "categorias",
        "categories": categories,
        "q": q,
    }

    if request.htmx and _htmx_target(request) == "categories-table":
        return render(request, "dashboard/admin/partials/categories_table.html", context)

    return render(request, "dashboard/admin/categories.html", context)


@admin_required
def admin_category_create_view(request):
    form = AdminCategoryForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            category = create_category(name=form.cleaned_data["name"])
        except CatalogValidationError as exc:
            form.add_error(exc.field, exc.message)
        else:
            _log_admin_action(
                request=request,
                action="CATEGORY_CREATED",
                entity_type="categories",
                entity_id=category.id,
                notes=f"Administrador criou a categoria {category.name}.",
                new_values=category_snapshot(category),
            )

            messages.success(request, "Categoria criada com sucesso.")
            return redirect("dashboard:gestor_categorias")

    context = {
        "admin_tab": "categorias",
        "form": form,
        "page_title": "Nova Categoria",
        "submit_label": "Criar categoria",
        "is_create": True,
    }
    return render(request, "dashboard/admin/category_form.html", context)


@admin_required
def admin_category_update_view(request, category_id):
    category = get_object_or_404(ProductCategory, id=category_id)

    if request.method == "POST":
        form = AdminCategoryForm(request.POST)
        if form.is_valid():
            old_snapshot = category_snapshot(category)
            try:
                category, changed_fields = update_category(
                    category=category,
                    name=form.cleaned_data["name"],
                )
            except CatalogValidationError as exc:
                form.add_error(exc.field, exc.message)
            else:
                if changed_fields:
                    _log_admin_action(
                        request=request,
                        action="CATEGORY_UPDATED",
                        entity_type="categories",
                        entity_id=category.id,
                        notes=f"Administrador atualizou a categoria {category.name}.",
                        old_values=old_snapshot,
                        new_values=category_snapshot(category),
                    )

                    messages.success(request, "Categoria atualizada com sucesso.")
                else:
                    messages.info(request, "Não foram detetadas alterações.")

                return redirect("dashboard:gestor_categorias")
    else:
        form = AdminCategoryForm(initial={
            "name": category.name,
        })

    context = {
        "admin_tab": "categorias",
        "form": form,
        "category_obj": category,
        "page_title": f"Editar Categoria — {category.name}",
        "submit_label": "Guardar alterações",
        "is_create": False,
    }
    return render(request, "dashboard/admin/category_form.html", context)


@admin_required
def admin_users_view(request):
    q = request.GET.get("q", "").strip()

    users_qs = User.objects.all().order_by("-created_at")

    if q:
        users_qs = users_qs.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(email__icontains=q)
            | Q(role__icontains=q)
        )

    paginator = Paginator(users_qs, 10)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "admin_tab": "utilizadores",
        "page_obj": page_obj,
        "q": q,
    }

    if request.htmx and _htmx_target(request) == "users-table":
        return render(request, "dashboard/admin/partials/users_table.html", context)

    return render(request, "dashboard/admin/users.html", context)


@admin_required
def admin_user_create_view(request):
    form = AdminUserCreateForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                role = form.cleaned_data["role"]

                user = User.objects.create(
                    email=form.cleaned_data["email"],
                    password="",
                    first_name="",
                    last_name="",
                    role=role,
                    registration_source=RegistrationSource.ADMIN_CREATED,
                    account_status=AccountStatus.PENDING_EMAIL_CONFIRMATION,
                    is_active=False,
                    is_staff=(role == UserRole.ADMIN),
                )

                verification = create_admin_invite_token(user)
                send_admin_invite_email(request, user, verification)

                _log_admin_action(
                    request=request,
                    action="USER_INVITED",
                    entity_type="users",
                    entity_id=user.id,
                    notes=f"Administrador convidou utilizador {user.email}.",
                    new_values=_user_snapshot(user),
                )
        except IntegrityError:
            form.add_error("email", "Este email já está registado.")
        else:
            messages.success(request, "Convite enviado com sucesso.")
            return redirect("dashboard:gestor_utilizadores")

    context = {
        "admin_tab": "utilizadores",
        "form": form,
        "page_title": "Novo Utilizador",
        "submit_label": "Enviar convite",
        "is_create": True,
    }
    return render(request, "dashboard/admin/user_form.html", context)

@admin_required
def admin_user_detail_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)
    producer_profile = ProducerProfile.objects.filter(user=user_obj).first()

    related_logs = AuditLog.objects.filter(
        Q(entity_type="users", entity_id=user_obj.id) | Q(user=user_obj)
    ).select_related("user").order_by("-created_at")[:50]

    context = {
        "admin_tab": "utilizadores",
        "user_obj": user_obj,
        "producer_profile": producer_profile,
        "related_logs": related_logs,
        "related_activity_rows": _build_user_activity_rows(related_logs),
    }
    return render(request, "dashboard/admin/user_detail.html", context)


@admin_required
@require_POST
def admin_user_confirm_email_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)

    if request.current_user and request.current_user.id == user_obj.id:
        messages.error(request, "Não pode confirmar manualmente a sua própria conta.")
        return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)

    if user_obj.account_status != AccountStatus.PENDING_EMAIL_CONFIRMATION:
        messages.info(request, "Esta conta já não está pendente de confirmação por email.")
        return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)

    old_snapshot = _user_snapshot(user_obj, ProducerProfile.objects.filter(user=user_obj).first())
    now = timezone.now()

    with transaction.atomic():
        user_obj.email_verified_at = now
        user_obj.account_status = AccountStatus.ACTIVE
        user_obj.is_active = True
        user_obj.updated_at = now
        user_obj.save(
            update_fields=["email_verified_at", "account_status", "is_active", "updated_at"]
        )

        AccountVerificationToken.objects.filter(
            user=user_obj,
            purpose__in=[
                VerificationPurpose.SIGNUP_CONFIRMATION,
                VerificationPurpose.ADMIN_INVITE,
            ],
            used_at__isnull=True,
        ).update(used_at=now)

    new_snapshot = _user_snapshot(user_obj, ProducerProfile.objects.filter(user=user_obj).first())
    _log_admin_action(
        request=request,
        action="USER_EMAIL_CONFIRMED_BY_ADMIN",
        entity_type="users",
        entity_id=user_obj.id,
        notes=f"Administrador confirmou manualmente a conta de {user_obj.email}.",
        old_values=old_snapshot,
        new_values=new_snapshot,
    )

    messages.success(request, "Conta confirmada manualmente com sucesso.")
    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)


@admin_required
@require_POST
def admin_user_toggle_status_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)

    if user_obj.id == request.current_user.id:
        error_msg = "Não pode suspender ou reativar a sua própria conta."

        if request.htmx:
            context = {
                "user": user_obj,
                "q": request.POST.get("q", "").strip(),
            }
            response = render(request, "dashboard/admin/partials/user_row.html", context)
            return with_htmx_toast(response, "error", error_msg)

        messages.error(request, error_msg)
        return redirect("dashboard:gestor_utilizadores")

    if not user_obj.is_active and user_obj.account_status == AccountStatus.PENDING_EMAIL_CONFIRMATION:
        error_msg = (
            "Esta conta está pendente de confirmação de email. "
            "Só ficará ativa depois do utilizador confirmar a conta."
        )

        if request.htmx:
            context = {
                "user": user_obj,
                "q": request.POST.get("q", "").strip(),
            }
            response = render(request, "dashboard/admin/partials/user_row.html", context)
            return with_htmx_toast(response, "error", error_msg)

        messages.error(request, error_msg)
        return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)

    old_snapshot = _user_snapshot(user_obj, ProducerProfile.objects.filter(user=user_obj).first())
    now = timezone.now()

    if user_obj.is_active:
        user_obj.is_active = False
        if user_obj.account_status == AccountStatus.ACTIVE:
            user_obj.account_status = AccountStatus.SUSPENDED
        action = "USER_SUSPENDED"
        note = f"Administrador suspendeu utilizador {user_obj.email}."
        success_msg = "Utilizador suspenso com sucesso."
    else:
        user_obj.is_active = True
        if user_obj.account_status == AccountStatus.SUSPENDED:
            user_obj.account_status = AccountStatus.ACTIVE
        action = "USER_REACTIVATED"
        note = f"Administrador reativou utilizador {user_obj.email}."
        success_msg = "Utilizador reativado com sucesso."

    user_obj.updated_at = now
    user_obj.save(update_fields=["is_active", "account_status", "updated_at"])

    new_snapshot = _user_snapshot(user_obj, ProducerProfile.objects.filter(user=user_obj).first())

    _log_admin_action(
        request=request,
        action=action,
        entity_type="users",
        entity_id=user_obj.id,
        notes=note,
        old_values=old_snapshot,
        new_values=new_snapshot,
    )

    if request.htmx:
        context = {
            "user": user_obj,
            "q": request.POST.get("q", "").strip(),
        }
        response = render(request, "dashboard/admin/partials/user_row.html", context)
        return with_htmx_toast(response, "success", success_msg)

    messages.success(request, success_msg)

    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)

    return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)

@admin_required
def admin_audit_view(request):
    q = request.GET.get("q", "").strip()
    per_page = _audit_per_page(request.GET.get("per_page"))

    logs = (
        AuditLog.objects
        .select_related("user")
        .annotate(
            old_values_text=Cast("old_values", models.TextField()),
            new_values_text=Cast("new_values", models.TextField()),
        )
        .order_by("-created_at")
    )

    if q:
        search_filter = (
            Q(action__icontains=q)
            | Q(entity_type__icontains=q)
            | Q(notes__icontains=q)
            | Q(ip_address__icontains=q)
            | Q(user_agent__icontains=q)
            | Q(old_values_text__icontains=q)
            | Q(new_values_text__icontains=q)
            | Q(user__first_name__icontains=q)
            | Q(user__last_name__icontains=q)
            | Q(user__email__icontains=q)
        )

        matching_actions = _audit_action_matches(q)
        if matching_actions:
            search_filter |= Q(action__in=matching_actions)

        for field in _audit_field_matches(q):
            search_filter |= Q(old_values_text__icontains=field) | Q(new_values_text__icontains=field)

        for term in _audit_device_terms(q):
            search_filter |= Q(user_agent__icontains=term)

        logs = logs.filter(search_filter)

    paginator = Paginator(logs, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_range = [
        page_number if isinstance(page_number, int) else None
        for page_number in paginator.get_elided_page_range(
            page_obj.number,
            on_each_side=2,
            on_ends=1,
        )
    ]

    context = {
        "admin_tab": "auditoria",
        "logs": page_obj.object_list,
        "audit_rows": _build_user_activity_rows(page_obj.object_list),
        "page_obj": page_obj,
        "page_range": page_range,
        "per_page": per_page,
        "per_page_options": [10, 25, 50],
        "q": q,
    }

    if request.htmx and _htmx_target(request) == "audit-table":
        return render(request, "dashboard/admin/partials/audit_table.html", context)

    return render(request, "dashboard/admin/audit.html", context)

