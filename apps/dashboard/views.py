import csv
import json

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError, RestrictedError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.catalog.forms import AdminCategoryForm, AdminProductForm
from apps.catalog.models import Product, ProductCategory
from apps.catalog.services import (
    CatalogValidationError,
    can_delete_category,
    category_snapshot,
    create_category,
    create_product,
    delete_category,
    product_snapshot,
    update_category,
    update_product,
)
from apps.common.decorators import admin_required, client_only_required
from apps.common.htmx import with_htmx_toast
from apps.common.redirects import get_safe_next_url
from apps.dashboard.forms import AdminManualConfirmationForm, AdminUserCreateForm
from apps.dashboard.services.admin_audit import (
    build_admin_audit_context,
    build_admin_audit_entity_context,
    get_admin_audit_export_rows,
)
from apps.dashboard.services.admin_catalog import (
    get_admin_categories_queryset,
    get_admin_products_queryset,
)
from apps.dashboard.services.admin_metrics import build_admin_dashboard_context
from apps.dashboard.services.admin_users import (
    build_admin_user_detail_context,
    build_admin_users_context,
    confirm_user_email_by_admin,
    create_invited_user_from_admin_form,
    log_admin_action,
    resend_admin_invite,
    revoke_admin_invite,
    toggle_user_status_by_admin,
)
from apps.dashboard.services.client_dashboard import (
    build_client_dashboard_context,
    build_weather_card_context,
)
from apps.inventory.models import ProducerProduct, ProducerProfile, Stock
from apps.marketplace.services import get_public_listings


def _htmx_target(request):
    return (request.headers.get("HX-Target") or "").lstrip("#")


def _safe_next(request, field_name="next"):
    return get_safe_next_url(request, request.POST.get(field_name))


@client_only_required
def dashboard_view(request):
    try:
        context = build_client_dashboard_context(request.current_user)
    except ProducerProfile.DoesNotExist:
        request.session.flush()
        return redirect("accounts:login")

    return render(request, "dashboard/painel.html", context)


@client_only_required
def dashboard_weather_card_view(request):
    try:
        context = build_weather_card_context(request.current_user)
    except ProducerProfile.DoesNotExist:
        request.session.flush()
        return redirect("accounts:login")

    return render(request, "dashboard/partials/weather_card.html", context)


@client_only_required
def marketplace_map_listings_view(request):
    try:
        producer = ProducerProfile.objects.get(user=request.current_user)
    except ProducerProfile.DoesNotExist:
        return JsonResponse({"listings": []})

    listings = (
        get_public_listings()
        .filter(
            show_location_on_map=True,
            producer__latitude__isnull=False,
            producer__longitude__isnull=False,
        )
        .select_related("product", "product__category", "producer")
        .order_by("-published_at")[:300]
    )

    data = []
    for listing in listings:
        p = listing.producer
        try:
            latitude = float(p.latitude)
            longitude = float(p.longitude)
        except (TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue

        cat = getattr(listing.product, "category", None)
        is_own = getattr(listing, "producer_id", getattr(p, "id", None)) == producer.id
        location = (
            f"{p.city}, {p.district}" if p.city and p.district
            else p.city or p.district or ""
        )
        data.append({
            "id": str(listing.id),
            "product_name": listing.product.name,
            "category_id": str(cat.id) if cat else "",
            "category_name": cat.name if cat else "Sem categoria",
            "producer_name": p.display_name or getattr(p, "company_name", None) or "Produtor",
            "producer_location": location,
            "lat": latitude,
            "lon": longitude,
            "quantity": str(listing.quantity_available),
            "unit": listing.product.unit,
            "unit_price": str(listing.unit_price),
            "delivery_mode": listing.delivery_mode,
            "delivery_radius_km": float(listing.delivery_radius_km) if listing.delivery_radius_km else None,
            "source": "forecast" if listing.forecast_id and not listing.stock_id else "stock",
            "is_own": is_own,
            "url": reverse(
                "marketplace:owner_detail" if is_own else "marketplace:public_detail",
                kwargs={"listing_id": listing.id},
            ),
        })

    return JsonResponse({"listings": data})


@admin_required
def admin_dashboard_view(request):
    return render(
        request,
        "dashboard/admin/dashboard.html",
        build_admin_dashboard_context(),
    )


@admin_required
def admin_products_view(request):
    q = request.GET.get("q", "").strip()
    context = {
        "admin_tab": "produtos",
        "products": get_admin_products_queryset(q=q),
        "q": q,
    }

    if request.htmx and _htmx_target(request) == "products-table":
        return render(request, "dashboard/admin/partials/products_table.html", context)

    return render(request, "dashboard/admin/products.html", context)


@admin_required
def admin_product_detail_view(request, product_id):
    product = get_object_or_404(Product.objects.select_related("category"), id=product_id)
    producer_links = (
        ProducerProduct.objects.filter(product=product, is_active=True)
        .select_related("producer", "producer__user")
        .order_by("producer__display_name", "producer__company_name")
    )
    stocks_by_producer_id = {
        stock.producer_id: stock
        for stock in Stock.objects.filter(product=product).select_related("producer")
    }
    producer_rows = [
        {
            "link": link,
            "producer": link.producer,
            "stock": stocks_by_producer_id.get(link.producer_id),
        }
        for link in producer_links
    ]

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
                description=form.cleaned_data.get("description"),
                is_active=form.cleaned_data["is_active"],
            )
        except CatalogValidationError as exc:
            form.add_error(exc.field, exc.message)
        else:
            log_admin_action(
                request=request,
                action="PRODUCT_CREATED",
                entity_type="products",
                entity_id=product.id,
                notes=f"Administrador criou o produto {product.name}.",
                new_values=product_snapshot(product),
            )
            messages.success(request, "Produto criado com sucesso.")
            return redirect("dashboard:gestor_produto_detalhe", product_id=product.id)

    return render(
        request,
        "dashboard/admin/product_form.html",
        {
            "admin_tab": "produtos",
            "form": form,
            "page_title": "Novo Produto",
            "submit_label": "Criar produto",
            "is_create": True,
        },
    )


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
                    description=form.cleaned_data.get("description"),
                    is_active=form.cleaned_data["is_active"],
                )
            except CatalogValidationError as exc:
                form.add_error(exc.field, exc.message)
            else:
                if changed_fields:
                    log_admin_action(
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
                "description": product.description,
                "is_active": product.is_active,
            },
        )

    return render(
        request,
        "dashboard/admin/product_form.html",
        {
            "admin_tab": "produtos",
            "form": form,
            "product_obj": product,
            "page_title": f"Editar Produto — {product.name}",
            "submit_label": "Guardar alterações",
            "is_create": False,
        },
    )


@admin_required
@require_POST
def admin_product_delete_view(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    q = request.POST.get("q", "").strip()
    next_url = _safe_next(request)

    if ProducerProduct.objects.filter(product=product).exists():
        error_msg = (
            "Este produto já está associado a produtores. "
            "Só pode ser desativado, não removido."
        )
        if request.htmx:
            response = render(
                request,
                "dashboard/admin/partials/products_table.html",
                {
                    "admin_tab": "produtos",
                    "products": get_admin_products_queryset(q=q),
                    "q": q,
                },
            )
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
            response = render(
                request,
                "dashboard/admin/partials/products_table.html",
                {
                    "admin_tab": "produtos",
                    "products": get_admin_products_queryset(q=q),
                    "q": q,
                },
            )
            return with_htmx_toast(response, "error", error_msg)
        messages.error(request, error_msg)
        if next_url:
            return redirect(next_url)
        return redirect("dashboard:gestor_produto_detalhe", product_id=product_id)

    log_admin_action(
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
        response = render(
            request,
            "dashboard/admin/partials/products_table.html",
            {
                "admin_tab": "produtos",
                "products": get_admin_products_queryset(q=q),
                "q": q,
            },
        )
        return with_htmx_toast(response, "success", success_msg)

    messages.success(request, success_msg)
    if next_url:
        return redirect(next_url)
    return redirect("dashboard:gestor_produtos")


@admin_required
def admin_categories_view(request):
    q = request.GET.get("q", "").strip()
    context = {
        "admin_tab": "categorias",
        "categories": get_admin_categories_queryset(q=q),
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
            log_admin_action(
                request=request,
                action="CATEGORY_CREATED",
                entity_type="categories",
                entity_id=category.id,
                notes=f"Administrador criou a categoria {category.name}.",
                new_values=category_snapshot(category),
            )
            messages.success(request, "Categoria criada com sucesso.")
            return redirect("dashboard:gestor_categorias")

    return render(
        request,
        "dashboard/admin/category_form.html",
        {
            "admin_tab": "categorias",
            "form": form,
            "page_title": "Nova Categoria",
            "submit_label": "Criar categoria",
            "is_create": True,
        },
    )


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
                    log_admin_action(
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
        form = AdminCategoryForm(initial={"name": category.name})

    return render(
        request,
        "dashboard/admin/category_form.html",
        {
            "admin_tab": "categorias",
            "form": form,
            "category_obj": category,
            "page_title": f"Editar Categoria — {category.name}",
            "submit_label": "Guardar alterações",
            "is_create": False,
            "can_delete_category": can_delete_category(category),
        },
    )


@admin_required
@require_POST
def admin_category_delete_view(request, category_id):
    category = get_object_or_404(ProductCategory, id=category_id)
    q = request.POST.get("q", "").strip()
    category_name = category.name
    old_snapshot = category_snapshot(category)

    try:
        with transaction.atomic():
            delete_category(category)
    except CatalogValidationError as exc:
        error_msg = exc.message
        if request.htmx:
            response = render(
                request,
                "dashboard/admin/partials/categories_table.html",
                {
                    "admin_tab": "categorias",
                    "categories": get_admin_categories_queryset(q=q),
                    "q": q,
                },
            )
            return with_htmx_toast(response, "error", error_msg)
        messages.error(request, error_msg)
        return redirect("dashboard:gestor_categoria_editar", category_id=category_id)
    except (ProtectedError, RestrictedError, IntegrityError):
        error_msg = "Não foi possível remover esta categoria porque existem registos relacionados."
        if request.htmx:
            response = render(
                request,
                "dashboard/admin/partials/categories_table.html",
                {
                    "admin_tab": "categorias",
                    "categories": get_admin_categories_queryset(q=q),
                    "q": q,
                },
            )
            return with_htmx_toast(response, "error", error_msg)
        messages.error(request, error_msg)
        return redirect("dashboard:gestor_categoria_editar", category_id=category_id)

    log_admin_action(
        request=request,
        action="CATEGORY_DELETED",
        entity_type="categories",
        entity_id=category_id,
        notes=f"Administrador removeu a categoria {category_name}.",
        old_values=old_snapshot,
        new_values=None,
    )

    success_msg = f"Categoria {category_name} removida com sucesso."
    if request.htmx:
        response = render(
            request,
            "dashboard/admin/partials/categories_table.html",
            {
                "admin_tab": "categorias",
                "categories": get_admin_categories_queryset(q=q),
                "q": q,
            },
        )
        return with_htmx_toast(response, "success", success_msg)

    messages.success(request, success_msg)
    return redirect("dashboard:gestor_categorias")


@admin_required
def admin_users_view(request):
    context = build_admin_users_context(
        q=request.GET.get("q", ""),
        page_number=request.GET.get("page"),
    )

    if request.htmx and _htmx_target(request) == "users-table":
        return render(request, "dashboard/admin/partials/users_table.html", context)

    return render(request, "dashboard/admin/users.html", context)


@admin_required
def admin_user_create_view(request):
    form = AdminUserCreateForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        user = create_invited_user_from_admin_form(request=request, form=form)
        if user:
            messages.success(request, "Convite enviado com sucesso.")
            return redirect("dashboard:gestor_utilizadores")

    return render(
        request,
        "dashboard/admin/user_form.html",
        {
            "admin_tab": "utilizadores",
            "form": form,
            "page_title": "Novo Utilizador",
            "submit_label": "Enviar convite",
            "is_create": True,
        },
    )


@admin_required
def admin_user_detail_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)
    return render(
        request,
        "dashboard/admin/user_detail.html",
        build_admin_user_detail_context(user_obj),
    )


@admin_required
@require_POST
def admin_user_confirm_email_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)
    form = AdminManualConfirmationForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Indica uma justificação válida para confirmar o email manualmente.")
        return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)

    result = confirm_user_email_by_admin(
        request=request,
        user_obj=user_obj,
        justification=form.cleaned_data["justification"],
    )

    if result.ok:
        messages.success(request, result.message)
    elif result.message.startswith("Esta conta"):
        messages.info(request, result.message)
    else:
        messages.error(request, result.message)

    next_url = _safe_next(request)
    if next_url:
        return redirect(next_url)
    return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)


@admin_required
@require_POST
def admin_user_resend_invite_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)
    result = resend_admin_invite(request=request, user_obj=user_obj)
    if result.ok:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message)
    return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)


@admin_required
@require_POST
def admin_user_revoke_invite_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)
    result = revoke_admin_invite(request=request, user_obj=user_obj)
    if result.ok:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message)
    return redirect("dashboard:gestor_utilizador_detalhe", user_id=user_obj.id)


@admin_required
@require_POST
def admin_user_toggle_status_view(request, user_id):
    user_obj = get_object_or_404(User, id=user_id)
    result = toggle_user_status_by_admin(request=request, user_obj=user_obj)

    if request.htmx:
        response = render(
            request,
            "dashboard/admin/partials/user_row.html",
            {"user": result.user, "q": request.POST.get("q", "").strip()},
        )
        return with_htmx_toast(response, "success" if result.ok else "error", result.message)

    if result.ok:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message)

    next_url = _safe_next(request)
    if next_url:
        return redirect(next_url)
    return redirect("dashboard:gestor_utilizador_detalhe", user_id=result.user.id)


@admin_required
def admin_audit_view(request):
    context = build_admin_audit_context(
        q=request.GET.get("q", ""),
        module=request.GET.get("module", ""),
        action=request.GET.get("action", ""),
        user_id=request.GET.get("user_id", ""),
        date_from=request.GET.get("date_from", ""),
        date_to=request.GET.get("date_to", ""),
        entity_type=request.GET.get("entity_type", ""),
        per_page_value=request.GET.get("per_page"),
        page_number=request.GET.get("page"),
    )

    if request.htmx and _htmx_target(request) == "audit-table":
        return render(request, "dashboard/admin/partials/audit_table.html", context)

    return render(request, "dashboard/admin/audit.html", context)


@admin_required
def admin_audit_export_view(request):
    rows = get_admin_audit_export_rows(
        q=request.GET.get("q", ""),
        module=request.GET.get("module", ""),
        action=request.GET.get("action", ""),
        user_id=request.GET.get("user_id", ""),
        date_from=request.GET.get("date_from", ""),
        date_to=request.GET.get("date_to", ""),
        entity_type=request.GET.get("entity_type", ""),
    )
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="auditoria.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow([
        "Data/hora",
        "Utilizador",
        "Acao",
        "Modulo",
        "Entidade",
        "ID entidade",
        "Detalhes",
        "IP",
        "Dispositivo",
        "Valores anteriores",
        "Valores novos",
    ])
    for row in rows:
        log = row["log"]
        writer.writerow([
            log.created_at.strftime("%d/%m/%Y %H:%M:%S") if log.created_at else "",
            row["actor_label"],
            row["action_label"],
            row["action_meta"]["category"],
            row["entity"]["label"],
            str(log.entity_id or ""),
            row["event_description"],
            log.ip_address or "",
            row["device_label"],
            json.dumps(log.old_values or {}, ensure_ascii=False),
            json.dumps(log.new_values or {}, ensure_ascii=False),
        ])
    return response


@admin_required
def admin_audit_entity_view(request, entity_type, entity_id):
    context = build_admin_audit_entity_context(entity_type=entity_type, entity_id=entity_id)
    if context is None:
        raise Http404("Tipo de entidade não suportado para consulta.")
    return render(request, "dashboard/admin/audit_entity_detail.html", context)
