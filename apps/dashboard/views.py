from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import models, transaction, IntegrityError
from django.db.models import Q, Sum, Count
from django.db.models.deletion import ProtectedError, RestrictedError
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.common.decorators import admin_required, client_only_required
from apps.common.htmx import with_htmx_toast
from apps.accounts.models import (
    User,
    UserRole,
    RegistrationSource,
    AccountStatus,
)
from apps.accounts.services import send_admin_invite_email, create_admin_invite_token
from apps.inventory.models import ProducerProfile, ProducerProduct, Stock
from apps.alerts.models import Alert, AlertStatus, AlertSeverity
from apps.marketplace.models import MarketplaceListing, ListingStatus
from apps.orders.models import Order, OrderStatus
from apps.catalog.models import Product, ProductCategory
from apps.dashboard.models import AuditLog
from apps.dashboard.forms import AdminUserCreateForm, AdminCategoryForm, AdminProductForm

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

def _normalize_text(value):
    return " ".join((value or "").split()).strip()


def _htmx_target(request):
    return (request.headers.get("HX-Target") or "").lstrip("#")


def _build_unique_product_slug(base_slug, exclude_id=None):
    slug = base_slug or "produto"
    candidate = slug
    counter = 2

    while True:
        qs = Product.objects.filter(slug=candidate)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        if not qs.exists():
            return candidate
        candidate = f"{slug}-{counter}"
        counter += 1


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


def _product_snapshot(product):
    return {
        "id": str(product.id),
        "name": product.name,
        "slug": product.slug,
        "category_id": str(product.category_id) if product.category_id else None,
        "category_name": product.category.name if product.category else None,
        "unit": product.unit,
        "description": product.description,
        "is_active": product.is_active,
        "created_at": product.created_at.isoformat() if product.created_at else None,
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
    }


def _build_unique_category_slug(base_slug, exclude_id=None):
    slug = base_slug or "categoria"
    candidate = slug
    counter = 2

    while True:
        qs = ProductCategory.objects.filter(slug=candidate)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        if not qs.exists():
            return candidate
        candidate = f"{slug}-{counter}"
        counter += 1


def _category_snapshot(category):
    return {
        "id": str(category.id),
        "name": category.name,
        "slug": category.slug,
        "is_active": category.is_active,
        "created_at": category.created_at.isoformat() if category.created_at else None,
        "updated_at": category.updated_at.isoformat() if category.updated_at else None,
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
        name = _normalize_text(form.cleaned_data["name"])
        unit = _normalize_text(form.cleaned_data["unit"])
        category = form.cleaned_data["category"]
        description = (form.cleaned_data.get("description") or "").strip() or None
        is_active = form.cleaned_data["is_active"]

        existing_by_name = Product.objects.filter(name__iexact=name).first()
        if existing_by_name:
            form.add_error("name", "Já existe um produto com esse nome.")
        else:
            slug = _build_unique_product_slug(slugify(name))

            product = Product.objects.create(
                category=category,
                name=name,
                slug=slug,
                unit=unit,
                description=description,
                is_active=is_active,
            )

            _log_admin_action(
                request=request,
                action="PRODUCT_CREATED",
                entity_type="products",
                entity_id=product.id,
                notes=f"Administrador criou o produto {product.name}.",
                new_values=_product_snapshot(product),
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
        form = AdminProductForm(request.POST)
        if form.is_valid():
            name = _normalize_text(form.cleaned_data["name"])
            unit = _normalize_text(form.cleaned_data["unit"])
            category = form.cleaned_data["category"]
            description = (form.cleaned_data.get("description") or "").strip() or None
            is_active = form.cleaned_data["is_active"]

            existing_by_name = Product.objects.filter(name__iexact=name).exclude(id=product.id).first()
            if existing_by_name:
                form.add_error("name", "Já existe outro produto com esse nome.")
            else:
                old_snapshot = _product_snapshot(product)
                changed_fields = []

                if product.category_id != category.id:
                    product.category = category
                    changed_fields.append("category")

                if product.name != name:
                    product.name = name
                    changed_fields.append("name")

                    new_slug = _build_unique_product_slug(slugify(name), exclude_id=product.id)
                    if product.slug != new_slug:
                        product.slug = new_slug
                        changed_fields.append("slug")

                if product.unit != unit:
                    product.unit = unit
                    changed_fields.append("unit")

                if product.description != description:
                    product.description = description
                    changed_fields.append("description")

                if product.is_active != is_active:
                    product.is_active = is_active
                    changed_fields.append("is_active")

                if changed_fields:
                    product.save(update_fields=changed_fields + ["updated_at"])

                    _log_admin_action(
                        request=request,
                        action="PRODUCT_UPDATED",
                        entity_type="products",
                        entity_id=product.id,
                        notes=f"Administrador atualizou o produto {product.name}.",
                        old_values=old_snapshot,
                        new_values=_product_snapshot(product),
                    )

                    messages.success(request, "Produto atualizado com sucesso.")
                else:
                    messages.info(request, "Não foram detetadas alterações.")

                return redirect("dashboard:gestor_produto_detalhe", product_id=product.id)
    else:
        form = AdminProductForm(initial={
            "category": product.category,
            "name": product.name,
            "unit": product.unit,
            "description": product.description,
            "is_active": product.is_active,
        })

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
    old_snapshot = _product_snapshot(product)

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
        name = _normalize_text(form.cleaned_data["name"])
        is_active = form.cleaned_data["is_active"]

        existing_by_name = ProductCategory.objects.filter(name__iexact=name).first()
        if existing_by_name:
            form.add_error("name", "Já existe uma categoria com esse nome.")
        else:
            slug = _build_unique_category_slug(slugify(name))

            category = ProductCategory.objects.create(
                name=name,
                slug=slug,
                is_active=is_active,
            )

            _log_admin_action(
                request=request,
                action="CATEGORY_CREATED",
                entity_type="categories",
                entity_id=category.id,
                notes=f"Administrador criou a categoria {category.name}.",
                new_values=_category_snapshot(category),
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
            name = _normalize_text(form.cleaned_data["name"])
            is_active = form.cleaned_data["is_active"]

            existing_by_name = ProductCategory.objects.filter(name__iexact=name).exclude(id=category.id).first()
            if existing_by_name:
                form.add_error("name", "Já existe outra categoria com esse nome.")
            else:
                old_snapshot = _category_snapshot(category)
                changed_fields = []

                if category.name != name:
                    category.name = name
                    changed_fields.append("name")

                    new_slug = _build_unique_category_slug(slugify(name), exclude_id=category.id)
                    if category.slug != new_slug:
                        category.slug = new_slug
                        changed_fields.append("slug")

                if category.is_active != is_active:
                    category.is_active = is_active
                    changed_fields.append("is_active")

                if changed_fields:
                    category.save(update_fields=changed_fields + ["updated_at"])

                    _log_admin_action(
                        request=request,
                        action="CATEGORY_UPDATED",
                        entity_type="categories",
                        entity_id=category.id,
                        notes=f"Administrador atualizou a categoria {category.name}.",
                        old_values=old_snapshot,
                        new_values=_category_snapshot(category),
                    )

                    messages.success(request, "Categoria atualizada com sucesso.")
                else:
                    messages.info(request, "Não foram detetadas alterações.")

                return redirect("dashboard:gestor_categorias")
    else:
        form = AdminCategoryForm(initial={
            "name": category.name,
            "is_active": category.is_active,
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
@require_POST
def admin_category_toggle_status_view(request, category_id):
    category = get_object_or_404(ProductCategory, id=category_id)
    q = request.POST.get("q", "").strip()
    old_snapshot = _category_snapshot(category)

    if category.is_active:
        category.is_active = False
        action = "CATEGORY_DEACTIVATED"
        note = f"Administrador desativou a categoria {category.name}."
        success_msg = "Categoria desativada com sucesso."
    else:
        category.is_active = True
        action = "CATEGORY_REACTIVATED"
        note = f"Administrador reativou a categoria {category.name}."
        success_msg = "Categoria reativada com sucesso."

    category.updated_at = timezone.now()
    category.save(update_fields=["is_active", "updated_at"])

    _log_admin_action(
        request=request,
        action=action,
        entity_type="categories",
        entity_id=category.id,
        notes=note,
        old_values=old_snapshot,
        new_values=_category_snapshot(category),
    )

    if request.htmx:
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
        response = render(request, "dashboard/admin/partials/categories_table.html", context)
        return with_htmx_toast(response, "success", success_msg)

    messages.success(request, success_msg)

    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)

    return redirect("dashboard:gestor_categorias")


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
    ).select_related("user").order_by("-created_at")[:10]

    context = {
        "admin_tab": "utilizadores",
        "user_obj": user_obj,
        "producer_profile": producer_profile,
        "related_logs": related_logs,
    }
    return render(request, "dashboard/admin/user_detail.html", context)


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

    logs = AuditLog.objects.select_related("user").order_by("-created_at")

    if q:
        logs = logs.filter(
            Q(action__icontains=q)
            | Q(entity_type__icontains=q)
            | Q(notes__icontains=q)
            | Q(ip_address__icontains=q)
            | Q(user__first_name__icontains=q)
            | Q(user__last_name__icontains=q)
            | Q(user__email__icontains=q)
        )

    logs = logs[:100]

    context = {
        "admin_tab": "auditoria",
        "logs": logs,
        "q": q,
    }

    if request.htmx and _htmx_target(request) == "audit-table":
        return render(request, "dashboard/admin/partials/audit_table.html", context)

    return render(request, "dashboard/admin/audit.html", context)
