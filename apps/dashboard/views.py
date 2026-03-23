from decimal import Decimal


from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.core.paginator import Paginator
from django.db import models, transaction
from django.db.models import Q, Sum
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.common.decorators import login_required, admin_required, client_only_required
from apps.accounts.models import User, UserRole, RegistrationSource, AccountStatus
from apps.inventory.models import ProducerProfile, Stock
from apps.alerts.models import Alert, AlertStatus, AlertSeverity
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.orders.models import Order, OrderStatus
from apps.catalog.models import Product, ProductCategory
from apps.dashboard.models import AuditLog
from apps.dashboard.forms import AdminUserCreateForm, AdminUserUpdateForm
from apps.dashboard.models import AuditLog

def _get_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _log_admin_action(request, action, entity_type, entity_id=None, notes=None, old_values=None, new_values=None):
    AuditLog.objects.create(
        user=request.current_user,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_values=old_values,
        new_values=new_values,
        ip_address=_get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT"),
        notes=notes,
    )


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
        status__in=[AlertStatus.ACTIVE, AlertStatus.READ],
    )

    active_alerts_count = active_alerts_qs.count()

    critical_alerts_qs = active_alerts_qs.filter(
        severity=AlertSeverity.CRITICAL
    )
    critical_alerts_count = critical_alerts_qs.count()

    critical_stock_qs = Stock.objects.select_related("product").filter(
        producer=producer,
        current_quantity__lte=models.F("minimum_threshold"),
    ).order_by("current_quantity")

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
    recent_orders = pending_orders_qs[:3]
    low_stock_preview = critical_stock_qs[:3]

    recent_activity = AuditLog.objects.filter(user=user).order_by("-created_at")[:5]

    listed_product_ids = active_listings_qs.values_list("product_id", flat=True)

    surplus_stock_candidate = (
        Stock.objects.select_related("product")
        .filter(
            producer=producer,
            current_quantity__gt=models.F("minimum_threshold"),
        )
        .exclude(product_id__in=listed_product_ids)
        .order_by("-current_quantity")
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
                    f"Mínimo: {low_stock.minimum_threshold} {low_stock.product.unit}"
                ),
                "url": "/stocks/",
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
                f"O produto {surplus_stock_candidate.product.name} parece ter stock acima do mínimo "
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
            "url": "/stocks/",
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
        "recent_orders": recent_orders,
        "low_stock_preview": low_stock_preview,
        "recent_activity": recent_activity,
    }
    return render(request, "dashboard/painel.html", context)


@admin_required
def admin_dashboard_view(request):
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    active_listings_count = MarketplaceListing.objects.filter(
        status=ListingStatus.ACTIVE
    ).count()

    monthly_orders_qs = Order.objects.filter(created_at__gte=month_start)
    monthly_orders_count = monthly_orders_qs.count()
    monthly_volume = monthly_orders_qs.aggregate(
        total=Sum("total_amount")
    )["total"] or Decimal("0.00")

    critical_alerts_count = Alert.objects.filter(
        severity=AlertSeverity.CRITICAL,
        status__in=[AlertStatus.ACTIVE, AlertStatus.READ],
    ).count()

    recent_alerts = Alert.objects.select_related("producer", "product").order_by("-created_at")[:5]
    recent_users = User.objects.order_by("-created_at")[:5]

    context = {
        "admin_tab": "dashboard",
        "active_listings_count": active_listings_count,
        "monthly_orders_count": monthly_orders_count,
        "monthly_volume": monthly_volume,
        "critical_alerts_count": critical_alerts_count,
        "recent_alerts": recent_alerts,
        "recent_users": recent_users,
    }
    return render(request, "dashboard/admin/dashboard.html", context)


@admin_required
def admin_products_view(request):
    products = Product.objects.select_related("category").order_by("name")

    context = {
        "admin_tab": "produtos",
        "products": products,
    }
    return render(request, "dashboard/admin/products.html", context)


@admin_required
def admin_categories_view(request):
    categories = ProductCategory.objects.order_by("name")

    context = {
        "admin_tab": "categorias",
        "categories": categories,
    }
    return render(request, "dashboard/admin/categories.html", context)


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
    return render(request, "dashboard/admin/users.html", context)


@admin_required
def admin_user_create_view(request):
    form = AdminUserCreateForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            now = timezone.now()
            role = form.cleaned_data["role"]

            user = User.objects.create(
                email=form.cleaned_data["email"],
                password=make_password(form.cleaned_data["password"]),
                first_name=form.cleaned_data["first_name"].strip(),
                last_name=form.cleaned_data["last_name"].strip(),
                role=role,
                registration_source=RegistrationSource.ADMIN_CREATED,
                account_status=AccountStatus.ACTIVE,
                email_verified_at=now,
                is_active=True,
                is_staff=(role == UserRole.ADMIN),
            )

            producer_profile = None
            if role == UserRole.CLIENTE:
                producer_profile = ProducerProfile.objects.create(
                    user=user,
                    display_name=f"{user.first_name} {user.last_name}".strip(),
                    company_name=form.cleaned_data.get("company_name") or None,
                    user_type=form.cleaned_data["user_type"],
                    member_since=now,
                    completed_transactions_count=0,
                    is_active_marketplace=True,
                )

            _log_admin_action(
                request=request,
                action="USER_CREATED",
                entity_type="users",
                entity_id=user.id,
                notes=f"Administrador criou utilizador {user.email}.",
                new_values=_user_snapshot(user, producer_profile),
            )

        messages.success(request, "Utilizador criado com sucesso.")
        return redirect("dashboard:gestor_utilizador_detalhe", user_id=user.id)

    context = {
        "admin_tab": "utilizadores",
        "form": form,
        "page_title": "Novo Utilizador",
        "submit_label": "Criar utilizador",
        "is_create": True,
    }
    return render(request, "dashboard/admin/user_form.html", context)


@admin_required
def admin_user_detail_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)
    producer_profile = ProducerProfile.objects.filter(user=user_obj).first()

    related_logs = AuditLog.objects.filter(
        Q(entity_type="users", entity_id=user_obj.id) | Q(user=user_obj)
    ).select_related("user").order_by("-created_at")[:10]

    context = {
        "admin_tab": "utilizadores",
        "user_obj": user_obj,
        "producer_profile": producer_profile,
        "related_logs": related_logs,
    }
    return render(request, "dashboard/admin/user_detail.html", context)


@admin_required
def admin_user_update_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)
    producer_profile = ProducerProfile.objects.filter(user=user_obj).first()

    form = AdminUserUpdateForm(
        request.POST or None,
        user_instance=user_obj,
        producer_profile=producer_profile,
    )

    if request.method == "POST" and form.is_valid():
        old_snapshot = _user_snapshot(user_obj, producer_profile)

        with transaction.atomic():
            now = timezone.now()

            user_obj.first_name = form.cleaned_data["first_name"].strip()
            user_obj.last_name = form.cleaned_data["last_name"].strip()
            user_obj.email = form.cleaned_data["email"]
            user_obj.role = form.cleaned_data["role"]
            user_obj.account_status = form.cleaned_data["account_status"]
            user_obj.is_active = form.cleaned_data["is_active"]
            user_obj.is_staff = user_obj.role == UserRole.ADMIN

            update_fields = [
                "first_name",
                "last_name",
                "email",
                "role",
                "account_status",
                "is_active",
                "is_staff",
                "updated_at",
            ]

            if user_obj.account_status == AccountStatus.ACTIVE and not user_obj.email_verified_at:
                user_obj.email_verified_at = now
                update_fields.append("email_verified_at")

            new_password = form.cleaned_data.get("new_password")
            if new_password:
                user_obj.password = make_password(new_password)
                update_fields.append("password")

            user_obj.updated_at = now
            user_obj.save(update_fields=update_fields)

            if user_obj.role == UserRole.CLIENTE:
                if producer_profile:
                    producer_profile.display_name = f"{user_obj.first_name} {user_obj.last_name}".strip()
                    producer_profile.company_name = form.cleaned_data.get("company_name") or None
                    producer_profile.user_type = form.cleaned_data["user_type"]
                    producer_profile.updated_at = now
                    producer_profile.save(
                        update_fields=["display_name", "company_name", "user_type", "updated_at"]
                    )
                else:
                    producer_profile = ProducerProfile.objects.create(
                        user=user_obj,
                        display_name=f"{user_obj.first_name} {user_obj.last_name}".strip(),
                        company_name=form.cleaned_data.get("company_name") or None,
                        user_type=form.cleaned_data["user_type"],
                        member_since=now,
                        completed_transactions_count=0,
                        is_active_marketplace=True,
                    )

            new_snapshot = _user_snapshot(user_obj, producer_profile)

            _log_admin_action(
                request=request,
                action="USER_UPDATED",
                entity_type="users",
                entity_id=user_obj.id,
                notes=f"Administrador editou utilizador {user_obj.email}.",
                old_values=old_snapshot,
                new_values=new_snapshot,
            )

        messages.success(request, "Utilizador atualizado com sucesso.")
        return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)

    context = {
        "admin_tab": "utilizadores",
        "form": form,
        "user_obj": user_obj,
        "page_title": "Editar Utilizador",
        "submit_label": "Guardar alterações",
        "is_create": False,
    }
    return render(request, "dashboard/admin/user_form.html", context)


@admin_required
@require_POST
def admin_user_toggle_status_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)

    if user_obj.id == request.current_user.id:
        messages.error(request, "Não pode suspender ou reativar a sua própria conta.")
        return redirect("dashboard:gestor_utilizadores")

    old_snapshot = _user_snapshot(user_obj, ProducerProfile.objects.filter(user=user_obj).first())

    if not user_obj.is_active and user_obj.account_status == AccountStatus.PENDING_EMAIL_CONFIRMATION:
        messages.error(
            request,
            "Esta conta está pendente de confirmação de email. Edite o utilizador se quiser ativá-lo manualmente."
        )
        return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)

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

    messages.success(request, success_msg)

    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)

    return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)


@admin_required
def admin_audit_view(request):
    logs = AuditLog.objects.select_related("user").order_by("-created_at")[:100]

    context = {
        "admin_tab": "auditoria",
        "logs": logs,
    }
    return render(request, "dashboard/admin/audit.html", context)