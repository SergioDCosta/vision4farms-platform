from urllib.parse import urlencode

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.common.decorators import client_only_required
from apps.common.htmx import with_htmx_toast
from apps.inventory.models import ProducerProfile
from apps.notifications_app.services import clear_recent_notifications_for_user, list_recent_notifications_for_user
from apps.alerts.services import (
    build_alert_sections,
    expire_ignored_alerts_for_producer,
    get_alert_category_filter_options,
    get_alert_category_label,
    get_client_alerts_badge_state,
    get_alert_for_producer,
    get_alert_tab_counts,
    get_alert_type_label,
    get_alert_type_filter_options,
    ignore_alert,
    ignore_all_active_alerts,
    list_alerts_for_producer,
    mark_client_alerts_seen,
    normalize_alert_category,
    normalize_alert_type,
    reactivate_ignored_alert,
    resolve_alert,
    sync_alerts_for_producer,
)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _normalize_tab(raw_tab):
    tab = (raw_tab or "active").strip().lower()
    if tab not in {"active", "ignored", "resolved"}:
        tab = "active"
    return tab


def _alerts_index_url(*, tab="active", alert_type="", category="", q="", action_only=False):
    query = {"tab": _normalize_tab(tab)}
    normalized_type = normalize_alert_type(alert_type)
    if normalized_type:
        query["type"] = normalized_type
    normalized_category = normalize_alert_category(category)
    if normalized_category:
        query["category"] = normalized_category
    q = (q or "").strip()
    if q:
        query["q"] = q
    if action_only:
        query["action"] = "1"
    return f"{reverse('alerts:index')}?{urlencode(query)}"


def _get_producer(request):
    user = getattr(request, "current_user", None)
    if not user:
        return None
    return ProducerProfile.objects.filter(user=user).first()


def _render_alerts_page(request, producer, tab, alert_type="", category="", q="", action_only=False):
    active_type = normalize_alert_type(alert_type)
    active_category = normalize_alert_category(category)
    q = (q or "").strip()
    alerts = list_alerts_for_producer(
        producer=producer,
        tab=tab,
        alert_type=active_type,
        category=active_category,
        q=q,
        requires_action=action_only,
    )
    context = {
        "page_title": "Alertas",
        "active_tab": tab,
        "active_type": active_type,
        "active_type_label": get_alert_type_label(active_type) if active_type else "",
        "active_category": active_category,
        "active_category_label": get_alert_category_label(active_category) if active_category else "",
        "search_query": q,
        "action_only": action_only,
        "alerts": alerts,
        "alert_sections": build_alert_sections(alerts, active_tab=tab),
        "recent_notifications": list_recent_notifications_for_user(
            user=request.current_user,
            limit=6,
        ),
        "filtered_count": len(alerts),
        "tab_counts": get_alert_tab_counts(producer=producer),
        "type_options": get_alert_type_filter_options(
            producer=producer,
            tab=tab,
            selected_type=active_type,
        ),
        "category_options": get_alert_category_filter_options(
            producer=producer,
            tab=tab,
            selected_category=active_category,
        ),
        "tab_urls": {
            "active": _alerts_index_url(tab="active", alert_type=active_type, category=active_category, q=q, action_only=action_only),
            "ignored": _alerts_index_url(tab="ignored", alert_type=active_type, category=active_category, q=q, action_only=action_only),
            "resolved": _alerts_index_url(tab="resolved", alert_type=active_type, category=active_category, q=q, action_only=action_only),
        },
        "current_url": _alerts_index_url(tab=tab, alert_type=active_type, category=active_category, q=q, action_only=action_only),
    }
    return render(request, "alerts/index.html", context)


def _expire_ignored_alerts(producer, acting_user=None):
    expire_ignored_alerts_for_producer(producer=producer, acting_user=acting_user)


@client_only_required
def alerts_index_view(request):
    producer = _get_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    _expire_ignored_alerts(producer, acting_user=request.current_user)
    sync_alerts_for_producer(producer, acting_user=request.current_user)
    mark_client_alerts_seen(request)
    tab = _normalize_tab(request.GET.get("tab"))
    alert_type = normalize_alert_type(request.GET.get("type"))
    category = normalize_alert_category(request.GET.get("category"))
    q = (request.GET.get("q") or "").strip()
    action_only = request.GET.get("action") == "1"
    return _render_alerts_page(request, producer, tab, alert_type, category, q, action_only)


@client_only_required
def alerts_sidebar_state_view(request):
    return JsonResponse(get_client_alerts_badge_state(request))


@client_only_required
def alert_ignore_view(request, alert_id):
    if request.method != "POST":
        return redirect("alerts:index")

    producer = _get_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    _expire_ignored_alerts(producer, acting_user=request.current_user)
    alert = get_alert_for_producer(producer=producer, alert_id=alert_id)
    tab = _normalize_tab(request.POST.get("tab"))
    alert_type = normalize_alert_type(request.POST.get("type"))
    category = normalize_alert_category(request.POST.get("category"))
    q = (request.POST.get("q") or "").strip()
    action_only = request.POST.get("action") == "1"
    if not alert:
        response = _render_alerts_page(request, producer, tab, alert_type, category, q, action_only)
        if _is_htmx(request):
            return with_htmx_toast(response, "error", "Alerta não encontrado.")
        messages.error(request, "Alerta não encontrado.")
        return response

    reason = (request.POST.get("reason") or "").strip()
    snooze_key = (request.POST.get("snooze") or "").strip()
    changed = ignore_alert(alert, user=request.current_user, reason=reason, snooze_key=snooze_key)
    message = "Alerta adiado." if changed else "O alerta não pode ser adiado neste estado."

    response = _render_alerts_page(request, producer, tab, alert_type, category, q, action_only)
    if _is_htmx(request):
        return with_htmx_toast(response, "success" if changed else "info", message)
    messages.success(request, message) if changed else messages.info(request, message)
    return response


@client_only_required
def alert_resolve_view(request, alert_id):
    if request.method != "POST":
        return redirect("alerts:index")

    producer = _get_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    _expire_ignored_alerts(producer, acting_user=request.current_user)
    alert = get_alert_for_producer(producer=producer, alert_id=alert_id)
    tab = _normalize_tab(request.POST.get("tab"))
    alert_type = normalize_alert_type(request.POST.get("type"))
    category = normalize_alert_category(request.POST.get("category"))
    q = (request.POST.get("q") or "").strip()
    action_only = request.POST.get("action") == "1"
    if not alert:
        response = _render_alerts_page(request, producer, tab, alert_type, category, q, action_only)
        if _is_htmx(request):
            return with_htmx_toast(response, "error", "Alerta não encontrado.")
        messages.error(request, "Alerta não encontrado.")
        return response

    notes = (request.POST.get("notes") or "").strip()
    changed = resolve_alert(alert, user=request.current_user, notes=notes)
    if changed:
        message = "Alerta resolvido. Se for automático, fica oculto enquanto a mesma condição persistir."
    else:
        message = "O alerta não pode ser resolvido neste estado."

    response = _render_alerts_page(request, producer, tab, alert_type, category, q, action_only)
    if _is_htmx(request):
        return with_htmx_toast(response, "success" if changed else "info", message)
    messages.success(request, message) if changed else messages.info(request, message)
    return response


@client_only_required
def alert_reactivate_view(request, alert_id):
    if request.method != "POST":
        return redirect("alerts:index")

    producer = _get_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    _expire_ignored_alerts(producer, acting_user=request.current_user)
    alert = get_alert_for_producer(producer=producer, alert_id=alert_id)
    tab = _normalize_tab(request.POST.get("tab"))
    alert_type = normalize_alert_type(request.POST.get("type"))
    category = normalize_alert_category(request.POST.get("category"))
    q = (request.POST.get("q") or "").strip()
    action_only = request.POST.get("action") == "1"
    if not alert:
        response = _render_alerts_page(request, producer, tab, alert_type, category, q, action_only)
        if _is_htmx(request):
            return with_htmx_toast(response, "error", "Alerta não encontrado.")
        messages.error(request, "Alerta não encontrado.")
        return response

    changed = reactivate_ignored_alert(alert, user=request.current_user)
    message = "Alerta reativado." if changed else "O alerta já não estava ignorado."

    response = _render_alerts_page(request, producer, tab, alert_type, category, q, action_only)
    if _is_htmx(request):
        return with_htmx_toast(response, "success" if changed else "info", message)
    messages.success(request, message) if changed else messages.info(request, message)
    return response


@client_only_required
def alert_ignore_all_view(request):
    if request.method != "POST":
        return redirect("alerts:index")

    producer = _get_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    _expire_ignored_alerts(producer, acting_user=request.current_user)
    tab = _normalize_tab(request.POST.get("tab"))
    alert_type = normalize_alert_type(request.POST.get("type"))
    category = normalize_alert_category(request.POST.get("category"))
    q = (request.POST.get("q") or "").strip()
    action_only = request.POST.get("action") == "1"
    reason = (request.POST.get("reason") or "").strip()
    ignored_count = ignore_all_active_alerts(
        producer=producer,
        user=request.current_user,
        reason=reason,
        alert_type=alert_type,
        category=category,
        q=q,
        requires_action=action_only,
    )
    scope_label = "visível" if alert_type or category or q or action_only else "ativo"
    plural_scope_label = "visíveis" if alert_type or category or q or action_only else "ativos"
    if ignored_count == 1:
        message = f"Foi adiado 1 alerta {scope_label}."
    elif ignored_count > 1:
        message = f"Foram adiados {ignored_count} alertas {plural_scope_label}."
    else:
        message = f"Não existiam alertas {plural_scope_label} para adiar."

    response = _render_alerts_page(request, producer, tab, alert_type, category, q, action_only)
    if _is_htmx(request):
        return with_htmx_toast(response, "success" if ignored_count else "info", message)
    messages.success(request, message) if ignored_count else messages.info(request, message)
    return response


@client_only_required
def clear_recent_notifications_view(request):
    if request.method != "POST":
        return redirect("alerts:index")

    producer = _get_producer(request)
    if not producer:
        messages.error(request, "Perfil de produtor não encontrado.")
        return redirect("dashboard:painel")

    tab = _normalize_tab(request.POST.get("tab"))
    alert_type = normalize_alert_type(request.POST.get("type"))
    category = normalize_alert_category(request.POST.get("category"))
    q = (request.POST.get("q") or "").strip()
    action_only = request.POST.get("action") == "1"
    deleted_count = clear_recent_notifications_for_user(user=request.current_user)
    message = "Notificações recentes limpas." if deleted_count else "Não havia notificações recentes para limpar."

    response = _render_alerts_page(request, producer, tab, alert_type, category, q, action_only)
    if _is_htmx(request):
        return with_htmx_toast(response, "success" if deleted_count else "info", message)
    messages.success(request, message) if deleted_count else messages.info(request, message)
    return response
