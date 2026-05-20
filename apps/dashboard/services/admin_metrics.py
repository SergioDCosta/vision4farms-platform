from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncWeek
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountStatus, User
from apps.alerts.models import Alert
from apps.marketplace.models import ListingStatus, MarketplaceListing
from apps.orders.models import Order, OrderItem, OrderItemStatus, OrderSourceType, OrderStatus
from apps.support.models import SupportTicket, SupportTicketStatus


def build_admin_dashboard_context():
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    chart_start = week_start - timedelta(weeks=11)
    source_types = [OrderSourceType.MARKETPLACE, OrderSourceType.RECOMMENDATION]

    monthly_orders_qs = Order.objects.filter(
        created_at__gte=month_start,
        source_type__in=source_types,
    )
    active_users_qs = User.objects.filter(
        is_active=True,
        account_status=AccountStatus.ACTIVE,
    )
    online_threshold = now - timedelta(minutes=15)

    completed_items_qs = OrderItem.objects.filter(
        order__source_type__in=source_types,
        item_status=OrderItemStatus.COMPLETED,
        updated_at__gte=chart_start,
    )

    sales_context = _build_sales_category_context(completed_items_qs)
    weekly_context = _build_weekly_market_context(
        chart_start=chart_start,
        source_types=source_types,
    )
    support_context = _build_support_context()
    user_context = _build_user_context(now=now)

    active_users_count = active_users_qs.count()
    online_users_count = active_users_qs.filter(last_login__gte=online_threshold).count()
    total_users_count = User.objects.count()
    suspended_users_count = User.objects.filter(
        account_status=AccountStatus.SUSPENDED,
    ).count()

    return {
        "admin_tab": "dashboard",
        "active_listings_count": MarketplaceListing.objects.filter(
            status=ListingStatus.ACTIVE
        ).count(),
        "monthly_orders_count": monthly_orders_qs.count(),
        "monthly_volume": monthly_orders_qs.aggregate(total=Sum("total_amount"))["total"]
        or Decimal("0.00"),
        "active_support_tickets_count": SupportTicket.objects.filter(
            status__in=[SupportTicketStatus.OPEN, SupportTicketStatus.CLAIMED],
        ).count(),
        "active_users_count": active_users_count,
        "total_users_count": total_users_count,
        "suspended_users_count": suspended_users_count,
        "online_users_count": online_users_count,
        "offline_users_count": max(active_users_count - online_users_count, 0),
        "orders_in_progress_count": Order.objects.filter(
            status__in=[
                OrderStatus.PENDING,
                OrderStatus.CONFIRMED,
                OrderStatus.IN_PROGRESS,
                OrderStatus.DELIVERING,
            ],
            source_type__in=source_types,
        ).count(),
        "critical_operational_alerts_count": Alert.objects.filter(
            status="ACTIVE",
            severity="CRITICAL",
        ).count(),
        "recent_users": User.objects.order_by("-created_at")[:5],
        **sales_context,
        **weekly_context,
        **support_context,
        **user_context,
    }


def _build_sales_category_context(completed_items_qs):
    category_rows = list(
        completed_items_qs.values("product__category_id", "product__category__name")
        .annotate(total_qty=Sum("quantity"))
        .order_by("-total_qty")
    )

    palette = ["#16a34a", "#2563eb", "#d97706", "#7c3aed", "#0ea5e9", "#94a3b8"]
    max_pie_slices = 5
    top_rows = category_rows[:max_pie_slices]
    remaining_rows = category_rows[max_pie_slices:]
    if remaining_rows:
        others_qty = sum(
            (Decimal(str(row.get("total_qty") or 0)) for row in remaining_rows),
            Decimal("0"),
        )
        if others_qty > 0:
            top_rows.append(
                {
                    "product__category_id": None,
                    "product__category__name": "Outras categorias",
                    "total_qty": others_qty,
                }
            )

    pie_total = sum(
        (Decimal(str(row.get("total_qty") or 0)) for row in top_rows),
        Decimal("0"),
    )
    pie_cursor = Decimal("0")
    pie_segments = []
    category_pie_slices = []
    for idx, row in enumerate(top_rows):
        quantity = Decimal(str(row.get("total_qty") or 0))
        if quantity <= 0:
            continue

        percentage = (quantity / pie_total * Decimal("100")) if pie_total > 0 else Decimal("0")
        start_pct = pie_cursor
        end_pct = pie_cursor + percentage
        color = palette[idx % len(palette)]
        pie_segments.append(f"{color} {start_pct:.2f}% {end_pct:.2f}%")
        pie_cursor = end_pct

        category_pie_slices.append(
            {
                "label": row.get("product__category__name") or "Sem categoria",
                "quantity": quantity,
                "percentage": float(percentage),
                "color": color,
            }
        )

    top_category_row = category_rows[0] if category_rows else None
    top_category_id = top_category_row.get("product__category_id") if top_category_row else None
    top_category_products = []
    if top_category_row is not None:
        top_products_qs = completed_items_qs
        if top_category_id:
            top_products_qs = top_products_qs.filter(product__category_id=top_category_id)
        else:
            top_products_qs = top_products_qs.filter(product__category__isnull=True)
        top_category_products = list(
            top_products_qs.values("product_id", "product__name", "product__unit")
            .annotate(total_qty=Sum("quantity"))
            .order_by("-total_qty", "product__name")[:5]
        )

    return {
        "category_pie_slices": category_pie_slices,
        "category_sales_chart_data": {
            "labels": [slice["label"] for slice in category_pie_slices],
            "values": [float(slice["quantity"]) for slice in category_pie_slices],
            "colors": [slice["color"] for slice in category_pie_slices],
        },
        "category_pie_gradient": f"conic-gradient({', '.join(pie_segments)})"
        if pie_segments
        else None,
        "top_category_label": (
            top_category_row.get("product__category__name") or "Sem categoria"
        )
        if top_category_row
        else None,
        "top_category_total_qty": Decimal(str(top_category_row.get("total_qty") or 0))
        if top_category_row
        else Decimal("0"),
        "top_category_products": top_category_products,
    }


def _build_weekly_market_context(*, chart_start, source_types):
    purchases_by_week = {
        row["week"].date(): row["total"]
        for row in (
            Order.objects.filter(created_at__gte=chart_start, source_type__in=source_types)
            .annotate(week=TruncWeek("created_at"))
            .values("week")
            .annotate(total=Count("id"))
        )
    }
    sales_by_week = {
        row["week"].date(): row["total"]
        for row in (
            OrderItem.objects.filter(
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
        week_key = timezone.localtime(week_ref).date()
        weekly_market_points.append(
            {
                "label": week_ref.strftime("%d/%m"),
                "purchases": int(purchases_by_week.get(week_key, 0) or 0),
                "sales": int(sales_by_week.get(week_key, 0) or 0),
            }
        )

    max_weekly_value = max(
        [max(point["purchases"], point["sales"]) for point in weekly_market_points] or [0]
    )
    for point in weekly_market_points:
        if max_weekly_value > 0:
            point["purchases_height"] = round(
                (point["purchases"] / max_weekly_value) * 100,
                2,
            )
            point["sales_height"] = round((point["sales"] / max_weekly_value) * 100, 2)
        else:
            point["purchases_height"] = 0
            point["sales_height"] = 0

    return {
        "weekly_market_points": weekly_market_points,
        "weekly_market_chart_data": {
            "labels": [point["label"] for point in weekly_market_points],
            "purchases": [point["purchases"] for point in weekly_market_points],
            "sales": [point["sales"] for point in weekly_market_points],
        },
        "weekly_purchases_total": sum(point["purchases"] for point in weekly_market_points),
        "weekly_sales_total": sum(point["sales"] for point in weekly_market_points),
    }


def _build_support_context():
    support_by_status = {
        row["status"]: row["total"]
        for row in SupportTicket.objects.values("status").annotate(total=Count("id"))
    }
    support_chart_data = {
        "labels": ["Abertos", "Em tratamento", "Fechados"],
        "values": [
            int(support_by_status.get(SupportTicketStatus.OPEN, 0) or 0),
            int(support_by_status.get(SupportTicketStatus.CLAIMED, 0) or 0),
            int(support_by_status.get(SupportTicketStatus.CLOSED, 0) or 0),
        ],
        "colors": ["#ef4444", "#f59e0b", "#22c55e"],
    }
    return {
        "support_chart_data": support_chart_data,
        "admin_attention_items": _build_admin_attention_items(),
    }


def _build_user_context(*, now):
    month_labels = []
    active_counts = []
    pending_counts = []
    for idx in range(5, -1, -1):
        month_ref = _month_start_months_ago(now, idx)
        next_month = (month_ref.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_labels.append(month_ref.strftime("%m/%Y"))
        active_counts.append(
            User.objects.filter(
                created_at__lt=next_month,
                is_active=True,
                account_status=AccountStatus.ACTIVE,
            ).count()
        )
        pending_counts.append(
            User.objects.filter(
                created_at__lt=next_month,
                account_status=AccountStatus.PENDING_EMAIL_CONFIRMATION,
            ).count()
        )

    return {
        "user_growth_chart_data": {
            "labels": month_labels,
            "active": active_counts,
            "pending": pending_counts,
        }
    }


def _month_start_months_ago(now, months_ago):
    month_index = now.year * 12 + now.month - 1 - months_ago
    year = month_index // 12
    month = month_index % 12 + 1
    return now.replace(
        year=year,
        month=month,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _build_admin_attention_items():
    items = []

    support_tickets = (
        SupportTicket.objects.values(
            "id",
            "ticket_number",
            "subject",
            "status",
            "updated_at",
            "created_at",
        )
        .filter(
            status__in=[SupportTicketStatus.OPEN, SupportTicketStatus.CLAIMED],
        )
        .order_by("-updated_at")[:4]
    )
    for ticket in support_tickets:
        title = f"Ticket #{ticket['ticket_number']}: {ticket['subject']}"
        if ticket["status"] == SupportTicketStatus.OPEN:
            meta = "Novo pedido de suporte por responder"
            tone = "red"
        else:
            meta = "Pedido em tratamento"
            tone = "amber"
        items.append(
            {
                "icon": "bi-life-preserver",
                "title": title,
                "meta": meta,
                "url": reverse(
                    "support:admin_ticket_detail",
                    kwargs={"ticket_id": ticket["id"]},
                ),
                "label": "Suporte",
                "tone": tone,
                "created_at": ticket["updated_at"] or ticket["created_at"],
            }
        )

    pending_users = User.objects.filter(
        account_status=AccountStatus.PENDING_EMAIL_CONFIRMATION,
    ).order_by("-created_at")[:3]
    for user in pending_users:
        items.append(
            {
                "icon": "bi-person-check",
                "title": user.full_name or user.email,
                "meta": "Conta pendente de confirmação",
                "url": reverse("dashboard:gestor_utilizador_detalhe", kwargs={"user_id": user.id}),
                "label": "Utilizador",
                "tone": "blue",
                "created_at": user.created_at,
            }
        )

    critical_alerts = (
        Alert.objects.select_related("producer", "product")
        .filter(status="ACTIVE", severity="CRITICAL")
        .order_by("-created_at")[:3]
    )
    for alert in critical_alerts:
        producer_name = getattr(alert.producer, "display_name", "") or "Produtor"
        items.append(
            {
                "icon": "bi-exclamation-triangle",
                "title": alert.title,
                "meta": f"Alerta crítico em {producer_name}",
                "url": None,
                "label": "Operacional",
                "tone": "red",
                "created_at": alert.created_at,
            }
        )

    items.sort(key=lambda item: item.get("created_at") or timezone.now(), reverse=True)
    return items[:6]
