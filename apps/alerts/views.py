from urllib.parse import urlencode

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.common.decorators import client_only_required, login_required
from apps.common.htmx import with_htmx_toast
from apps.inventory.models import ProducerProfile
from apps.alerts.services import (
    expire_ignored_alerts_for_producer,
    get_client_alerts_badge_state,
    get_alert_for_producer,
    get_alert_tab_counts,
    get_alert_type_label,
    get_alert_type_filter_options,
    ignore_alert,
    ignore_all_active_alerts,
    list_alerts_for_producer,
    mark_client_alerts_seen,
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


def _alerts_index_url(*, tab="active", alert_type=""):
    query = {"tab": _normalize_tab(tab)}
    normalized_type = normalize_alert_type(alert_type)
    if normalized_type:
        query["type"] = normalized_type
    return f"{reverse('alerts:index')}?{urlencode(query)}"


def _get_producer(request):
    user = getattr(request, "current_user", None)
    if not user:
        return None
    return ProducerProfile.objects.filter(user=user).first()


def _render_alerts_page(request, producer, tab, alert_type=""):
    active_type = normalize_alert_type(alert_type)
    alerts = list_alerts_for_producer(
        producer=producer,
        tab=tab,
        alert_type=active_type,
    )
    context = {
        "page_title": "Alertas",
        "active_tab": tab,
        "active_type": active_type,
        "active_type_label": get_alert_type_label(active_type) if active_type else "",
        "alerts": alerts,
        "filtered_count": len(alerts),
        "tab_counts": get_alert_tab_counts(producer=producer),
        "type_options": get_alert_type_filter_options(
            producer=producer,
            tab=tab,
            selected_type=active_type,
        ),
        "tab_urls": {
            "active": _alerts_index_url(tab="active", alert_type=active_type),
            "ignored": _alerts_index_url(tab="ignored", alert_type=active_type),
            "resolved": _alerts_index_url(tab="resolved", alert_type=active_type),
        },
        "current_url": _alerts_index_url(tab=tab, alert_type=active_type),
    }
    return render(request, "alerts/index.html", context)


def _expire_ignored_alerts(producer, acting_user=None):
    expire_ignored_alerts_for_producer(producer=producer, acting_user=acting_user)


@login_required
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
    return _render_alerts_page(request, producer, tab, alert_type)


@login_required
@client_only_required
def alerts_sidebar_state_view(request):
    return JsonResponse(get_client_alerts_badge_state(request))


@login_required
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
    if not alert:
        response = _render_alerts_page(request, producer, tab, alert_type)
        if _is_htmx(request):
            return with_htmx_toast(response, "error", "Alerta não encontrado.")
        messages.error(request, "Alerta não encontrado.")
        return response

    reason = (request.POST.get("reason") or "").strip()
    changed = ignore_alert(alert, user=request.current_user, reason=reason)
    message = "Alerta ignorado durante 30 minutos." if changed else "O alerta já estava ignorado."

    response = _render_alerts_page(request, producer, tab, alert_type)
    if _is_htmx(request):
        return with_htmx_toast(response, "success" if changed else "info", message)
    messages.success(request, message) if changed else messages.info(request, message)
    return response


@login_required
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
    if not alert:
        response = _render_alerts_page(request, producer, tab, alert_type)
        if _is_htmx(request):
            return with_htmx_toast(response, "error", "Alerta não encontrado.")
        messages.error(request, "Alerta não encontrado.")
        return response

    notes = (request.POST.get("notes") or "").strip()
    changed = resolve_alert(alert, user=request.current_user, notes=notes)
    if changed:
        message = "Alerta resolvido. Se for automático, fica oculto enquanto a mesma condição persistir."
    else:
        message = "O alerta já estava resolvido."

    response = _render_alerts_page(request, producer, tab, alert_type)
    if _is_htmx(request):
        return with_htmx_toast(response, "success" if changed else "info", message)
    messages.success(request, message) if changed else messages.info(request, message)
    return response


@login_required
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
    if not alert:
        response = _render_alerts_page(request, producer, tab, alert_type)
        if _is_htmx(request):
            return with_htmx_toast(response, "error", "Alerta não encontrado.")
        messages.error(request, "Alerta não encontrado.")
        return response

    changed = reactivate_ignored_alert(alert, user=request.current_user)
    message = "Alerta reativado." if changed else "O alerta já não estava ignorado."

    response = _render_alerts_page(request, producer, tab, alert_type)
    if _is_htmx(request):
        return with_htmx_toast(response, "success" if changed else "info", message)
    messages.success(request, message) if changed else messages.info(request, message)
    return response


@login_required
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
    reason = (request.POST.get("reason") or "").strip()
    ignored_count = ignore_all_active_alerts(
        producer=producer,
        user=request.current_user,
        reason=reason,
        alert_type=alert_type,
    )
    scope_label = "visível" if alert_type else "ativo"
    plural_scope_label = "visíveis" if alert_type else "ativos"
    if ignored_count == 1:
        message = f"Foi ignorado 1 alerta {scope_label} durante 30 minutos."
    elif ignored_count > 1:
        message = f"Foram ignorados {ignored_count} alertas {plural_scope_label} durante 30 minutos."
    else:
        message = f"Não existiam alertas {plural_scope_label} para ignorar."

    response = _render_alerts_page(request, producer, tab, alert_type)
    if _is_htmx(request):
        return with_htmx_toast(response, "success" if ignored_count else "info", message)
    messages.success(request, message) if ignored_count else messages.info(request, message)
    return response
