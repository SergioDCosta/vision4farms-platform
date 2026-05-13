import re
import unicodedata

from django.core.paginator import Paginator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Cast

from apps.common.audit import describe_user_agent
from apps.dashboard.models import AuditLog


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
    "PRODUCT_CREATED": "Produto criado",
    "PRODUCT_UPDATED": "Produto atualizado",
    "PRODUCT_DELETED": "Produto removido",
    "CATEGORY_CREATED": "Categoria criada",
    "CATEGORY_UPDATED": "Categoria atualizada",
    "CATEGORY_DELETED": "Categoria removida",
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


def audit_change_rows(log):
    old_values = log.old_values or {}
    new_values = log.new_values or {}
    if not isinstance(old_values, dict) or not isinstance(new_values, dict):
        return []

    rows = []
    for key in sorted((set(old_values) | set(new_values)) - {"id", "device"}):
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


def actor_label(user):
    if not user:
        return "Sistema"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return full_name or user.email or "Utilizador"


def build_user_activity_rows(logs):
    rows = []
    for log in logs:
        device = describe_user_agent(log.user_agent)
        rows.append(
            {
                "log": log,
                "action_label": AUDIT_ACTION_LABELS.get(log.action, log.action),
                "changes": audit_change_rows(log),
                "device_label": device["label"] if log.user_agent else "—",
                "actor_label": actor_label(log.user),
            }
        )
    return rows


def _normalize_search_text(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    without_accents = "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).lower()
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def _audit_action_matches(query):
    query = _normalize_search_text(query)
    return [
        action
        for action, label in AUDIT_ACTION_LABELS.items()
        if query in _normalize_search_text(action)
        or query in _normalize_search_text(label)
    ]


def _audit_field_matches(query):
    query = _normalize_search_text(query)
    return [
        field
        for field, label in AUDIT_FIELD_LABELS.items()
        if query in _normalize_search_text(field)
        or query in _normalize_search_text(label)
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


def audit_per_page(value):
    try:
        per_page = int(value)
    except (TypeError, ValueError):
        return 25
    return per_page if per_page in {10, 25, 50} else 25


def build_admin_audit_context(*, q="", per_page_value=None, page_number=None):
    q = (q or "").strip()
    per_page = audit_per_page(per_page_value)

    logs = (
        AuditLog.objects.select_related("user")
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
            search_filter |= Q(old_values_text__icontains=field) | Q(
                new_values_text__icontains=field
            )

        for term in _audit_device_terms(q):
            search_filter |= Q(user_agent__icontains=term)

        logs = logs.filter(search_filter)

    paginator = Paginator(logs, per_page)
    page_obj = paginator.get_page(page_number)
    page_range = [
        page if isinstance(page, int) else None
        for page in paginator.get_elided_page_range(
            page_obj.number,
            on_each_side=2,
            on_ends=1,
        )
    ]

    return {
        "admin_tab": "auditoria",
        "logs": page_obj.object_list,
        "audit_rows": build_user_activity_rows(page_obj.object_list),
        "page_obj": page_obj,
        "page_range": page_range,
        "per_page": per_page,
        "per_page_options": [10, 25, 50],
        "q": q,
    }
