import re
import unicodedata
from urllib.parse import urlencode
from uuid import UUID

from django.core.paginator import Paginator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Cast
from django.urls import reverse
from django.utils.dateparse import parse_date

from apps.accounts.models import User
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
    "USER_INVITE_RESENT": "Convite reenviado",
    "USER_INVITE_REVOKED": "Convite revogado",
    "USER_EMAIL_CONFIRMED_BY_ADMIN": "Email confirmado por admin",
    "USER_STATUS_UPDATED": "Estado alterado",
    "USER_SUSPENDED": "Utilizador suspenso",
    "USER_REACTIVATED": "Utilizador reativado",
    "SUPPORT_TICKET_CREATED": "Pedido de suporte criado",
    "SUPPORT_TICKET_UPDATED": "Pedido de suporte atualizado",
    "SUPPORT_TICKET_CLAIMED": "Pedido de suporte assumido",
    "SUPPORT_TICKET_REPLIED": "Resposta ao suporte",
    "SUPPORT_TICKET_REQUESTER_REPLIED": "Resposta do utilizador ao suporte",
    "SUPPORT_TICKET_CLOSED": "Pedido de suporte fechado",
    "PRODUCT_CREATED": "Produto criado",
    "PRODUCT_UPDATED": "Produto atualizado",
    "PRODUCT_DELETED": "Produto removido",
    "CATEGORY_CREATED": "Categoria criada",
    "CATEGORY_UPDATED": "Categoria atualizada",
    "CATEGORY_DELETED": "Categoria removida",
    "EXTERNAL_DEMAND_CREATED": "Pedido de cliente criado",
    "EXTERNAL_DEMAND_UPDATED": "Pedido de cliente atualizado",
    "EXTERNAL_DEMAND_CANCELLED": "Pedido de cliente cancelado",
    "EXTERNAL_DEMAND_FULFILLED": "Pedido de cliente cumprido",
    "CUSTOMER_DEMAND_NEED_CREATED": "Procura automática criada",
    "CUSTOMER_DEMAND_NEED_UPDATED": "Procura automática atualizada",
    "CUSTOMER_DEMAND_NEED_COVERED": "Procura automática coberta",
    "NEED_MARKETPLACE_PUBLISHED": "Procura publicada no marketplace",
    "NEED_MARKETPLACE_WITHDRAWN": "Procura retirada do marketplace",
    "NEED_MARKETPLACE_UNPUBLISHED_AFTER_RECALCULATION": "Procura retirada após recálculo",
    "NEED_RESPONSE_CREATED": "Proposta à procura criada",
    "NEED_RESPONSE_UPDATED": "Proposta à procura atualizada",
    "NEED_RESPONSE_REJECTED": "Proposta à procura rejeitada",
    "STOCK_CREATED": "Stock criado",
    "STOCK_UPDATED": "Stock atualizado",
    "STOCK_MOVEMENT_CREATED": "Movimento de stock registado",
    "FORECAST_CREATED": "Previsão criada",
    "FORECAST_UPDATED": "Previsão atualizada",
    "FORECAST_DELETED": "Previsão removida",
    "FORECAST_ASSIMILATED": "Produção futura assumida",
    "LISTING_CREATED": "Anúncio criado",
    "LISTING_UPDATED": "Anúncio atualizado",
    "LISTING_STATUS_CHANGED": "Estado do anúncio alterado",
    "LISTING_RETIRED": "Anúncio retirado",
    "LISTING_EXPIRED": "Anúncio expirado",
    "LISTING_INVALID_ATTEMPT": "Tentativa inválida de anúncio",
    "ORDER_CREATED": "Encomenda criada",
    "ORDER_STATUS_CHANGED": "Estado da encomenda alterado",
    "ORDER_RECEIPT_CONFIRMED": "Receção confirmada",
    "ORDER_CANCELLED": "Encomenda cancelada",
    "STOCK_RESERVATION_CHANGED": "Reserva de stock alterada",
    "FORECAST_RESERVATION_CHANGED": "Reserva de previsão alterada",
    "NEED_COVERAGE_CHANGED": "Cobertura da procura atualizada",
}

AUDIT_ACTION_META = {
    "USER_LOGIN": {"category": "Sessão", "icon": "bi-box-arrow-in-right", "tone": "info"},
    "USER_PROFILE_UPDATED": {"category": "Conta", "icon": "bi-person-check", "tone": "success"},
    "USER_PRODUCER_PROFILE_UPDATED": {"category": "Perfil", "icon": "bi-person-badge", "tone": "success"},
    "USER_PREFERENCES_UPDATED": {"category": "Preferências", "icon": "bi-sliders", "tone": "info"},
    "USER_PROFILE_PHOTO_REMOVED": {"category": "Perfil", "icon": "bi-image", "tone": "warning"},
    "USER_PASSWORD_CHANGED": {"category": "Segurança", "icon": "bi-shield-lock", "tone": "warning"},
    "USER_PASSWORD_RESET_COMPLETED": {"category": "Segurança", "icon": "bi-key", "tone": "warning"},
    "USER_INVITED": {"category": "Admin", "icon": "bi-envelope-plus", "tone": "success"},
    "USER_INVITE_RESENT": {"category": "Admin", "icon": "bi-envelope-arrow-up", "tone": "info"},
    "USER_INVITE_REVOKED": {"category": "Admin", "icon": "bi-envelope-x", "tone": "danger"},
    "USER_EMAIL_CONFIRMED_BY_ADMIN": {"category": "Admin", "icon": "bi-patch-check", "tone": "success"},
    "USER_STATUS_UPDATED": {"category": "Admin", "icon": "bi-person-gear", "tone": "warning"},
    "USER_SUSPENDED": {"category": "Admin", "icon": "bi-person-dash", "tone": "danger"},
    "USER_REACTIVATED": {"category": "Admin", "icon": "bi-person-check", "tone": "success"},
    "SUPPORT_TICKET_CREATED": {"category": "Suporte", "icon": "bi-life-preserver", "tone": "info"},
    "SUPPORT_TICKET_UPDATED": {"category": "Suporte", "icon": "bi-pencil-square", "tone": "info"},
    "SUPPORT_TICKET_CLAIMED": {"category": "Suporte", "icon": "bi-person-check", "tone": "success"},
    "SUPPORT_TICKET_REPLIED": {"category": "Suporte", "icon": "bi-reply", "tone": "success"},
    "SUPPORT_TICKET_REQUESTER_REPLIED": {"category": "Suporte", "icon": "bi-reply", "tone": "info"},
    "SUPPORT_TICKET_CLOSED": {"category": "Suporte", "icon": "bi-check-circle", "tone": "success"},
    "PRODUCT_CREATED": {"category": "Catálogo", "icon": "bi-box-seam", "tone": "success"},
    "PRODUCT_UPDATED": {"category": "Catálogo", "icon": "bi-pencil-square", "tone": "info"},
    "PRODUCT_DELETED": {"category": "Catálogo", "icon": "bi-trash", "tone": "danger"},
    "CATEGORY_CREATED": {"category": "Catálogo", "icon": "bi-tags", "tone": "success"},
    "CATEGORY_UPDATED": {"category": "Catálogo", "icon": "bi-pencil-square", "tone": "info"},
    "CATEGORY_DELETED": {"category": "Catálogo", "icon": "bi-trash", "tone": "danger"},
    "EXTERNAL_DEMAND_CREATED": {"category": "Pedidos", "icon": "bi-journal-plus", "tone": "success"},
    "EXTERNAL_DEMAND_UPDATED": {"category": "Pedidos", "icon": "bi-pencil-square", "tone": "info"},
    "EXTERNAL_DEMAND_CANCELLED": {"category": "Pedidos", "icon": "bi-x-circle", "tone": "danger"},
    "EXTERNAL_DEMAND_FULFILLED": {"category": "Pedidos", "icon": "bi-check2-circle", "tone": "success"},
    "CUSTOMER_DEMAND_NEED_CREATED": {"category": "Necessidades", "icon": "bi-megaphone", "tone": "warning"},
    "CUSTOMER_DEMAND_NEED_UPDATED": {"category": "Necessidades", "icon": "bi-arrow-repeat", "tone": "warning"},
    "CUSTOMER_DEMAND_NEED_COVERED": {"category": "Necessidades", "icon": "bi-check-circle", "tone": "success"},
    "NEED_MARKETPLACE_PUBLISHED": {"category": "Necessidades", "icon": "bi-shop", "tone": "success"},
    "NEED_MARKETPLACE_WITHDRAWN": {"category": "Necessidades", "icon": "bi-shop-window", "tone": "warning"},
    "NEED_MARKETPLACE_UNPUBLISHED_AFTER_RECALCULATION": {"category": "Necessidades", "icon": "bi-arrow-repeat", "tone": "warning"},
    "NEED_RESPONSE_CREATED": {"category": "Necessidades", "icon": "bi-send", "tone": "info"},
    "NEED_RESPONSE_UPDATED": {"category": "Necessidades", "icon": "bi-pencil-square", "tone": "info"},
    "NEED_RESPONSE_REJECTED": {"category": "Necessidades", "icon": "bi-x-circle", "tone": "danger"},
    "STOCK_CREATED": {"category": "Stock", "icon": "bi-box-seam", "tone": "success"},
    "STOCK_UPDATED": {"category": "Stock", "icon": "bi-box-seam", "tone": "info"},
    "STOCK_MOVEMENT_CREATED": {"category": "Stock", "icon": "bi-arrow-left-right", "tone": "info"},
    "FORECAST_CREATED": {"category": "Produção", "icon": "bi-calendar-plus", "tone": "success"},
    "FORECAST_UPDATED": {"category": "Produção", "icon": "bi-calendar-event", "tone": "info"},
    "FORECAST_DELETED": {"category": "Produção", "icon": "bi-trash", "tone": "danger"},
    "FORECAST_ASSIMILATED": {"category": "Produção", "icon": "bi-box-arrow-in-down", "tone": "success"},
    "LISTING_CREATED": {"category": "Marketplace", "icon": "bi-shop", "tone": "success"},
    "LISTING_UPDATED": {"category": "Marketplace", "icon": "bi-pencil-square", "tone": "info"},
    "LISTING_STATUS_CHANGED": {"category": "Marketplace", "icon": "bi-toggle-on", "tone": "warning"},
    "LISTING_RETIRED": {"category": "Marketplace", "icon": "bi-archive", "tone": "danger"},
    "LISTING_EXPIRED": {"category": "Marketplace", "icon": "bi-clock-history", "tone": "warning"},
    "LISTING_INVALID_ATTEMPT": {"category": "Marketplace", "icon": "bi-exclamation-triangle", "tone": "danger"},
    "ORDER_CREATED": {"category": "Encomendas", "icon": "bi-truck", "tone": "success"},
    "ORDER_STATUS_CHANGED": {"category": "Encomendas", "icon": "bi-arrow-repeat", "tone": "info"},
    "ORDER_RECEIPT_CONFIRMED": {"category": "Encomendas", "icon": "bi-check-circle", "tone": "success"},
    "ORDER_CANCELLED": {"category": "Encomendas", "icon": "bi-x-circle", "tone": "danger"},
    "STOCK_RESERVATION_CHANGED": {"category": "Reservas", "icon": "bi-lock", "tone": "warning"},
    "FORECAST_RESERVATION_CHANGED": {"category": "Reservas", "icon": "bi-calendar-check", "tone": "warning"},
    "NEED_COVERAGE_CHANGED": {"category": "Necessidades", "icon": "bi-pie-chart", "tone": "info"},
}

AUDIT_ENTITY_LABELS = {
    "users": "Utilizador",
    "producer_profiles": "Perfil de produtor",
    "user_preferences": "Preferências",
    "support_tickets": "Ticket de suporte",
    "products": "Produto",
    "categories": "Categoria",
    "marketplace_listings": "Anúncio",
    "needs": "Necessidade/procura",
    "orders": "Encomenda",
    "order_items": "Item de encomenda",
    "stocks": "Stock",
    "stock_movements": "Movimento de stock",
    "production_forecasts": "Previsão de produção",
    "external_customer_demands": "Pedido externo de cliente",
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
    "product_id": "Produto",
    "producer_id": "Produtor",
    "need_id": "Procura",
    "listing_id": "Anúncio",
    "order_id": "Encomenda",
    "requested_quantity": "Quantidade pedida",
    "requested_delivery_date": "Data pretendida",
    "required_quantity": "Quantidade necessária",
    "needed_by_date": "Data limite",
    "planned_quantity": "Cobertura planeada",
    "completed_quantity": "Cobertura concluída",
    "status": "Estado",
    "source_system": "Origem",
    "is_marketplace_published": "Publicada no marketplace",
    "published_at": "Publicada em",
    "current_quantity": "Quantidade atual",
    "reserved_quantity": "Quantidade reservada",
    "safety_stock": "Compromissos externos",
    "quantity_delta": "Movimento",
    "movement_type": "Tipo de movimento",
    "forecast_quantity": "Produção prevista",
    "period_start": "Início do período",
    "period_end": "Fim do período",
    "quantity_total": "Quantidade anunciada",
    "quantity_available": "Quantidade disponível",
    "quantity_reserved": "Quantidade reservada",
    "unit_price": "Preço unitário",
    "delivery_mode": "Entrega",
    "total_amount": "Valor total",
}

AUDIT_MODULES = {
    "stock": {
        "label": "Stock e produção",
        "actions": {
            "STOCK_CREATED",
            "STOCK_UPDATED",
            "STOCK_MOVEMENT_CREATED",
            "FORECAST_CREATED",
            "FORECAST_UPDATED",
            "FORECAST_DELETED",
            "FORECAST_ASSIMILATED",
            "STOCK_RESERVATION_CHANGED",
            "FORECAST_RESERVATION_CHANGED",
        },
    },
    "marketplace": {
        "label": "Marketplace",
        "actions": {
            "LISTING_CREATED",
            "LISTING_UPDATED",
            "LISTING_STATUS_CHANGED",
            "LISTING_RETIRED",
            "LISTING_EXPIRED",
            "LISTING_INVALID_ATTEMPT",
        },
    },
    "pedidos": {
        "label": "Pedidos de clientes",
        "actions": {
            "EXTERNAL_DEMAND_CREATED",
            "EXTERNAL_DEMAND_UPDATED",
            "EXTERNAL_DEMAND_CANCELLED",
            "EXTERNAL_DEMAND_FULFILLED",
        },
    },
    "needs": {
        "label": "Necessidades",
        "actions": {
            "CUSTOMER_DEMAND_NEED_CREATED",
            "CUSTOMER_DEMAND_NEED_UPDATED",
            "CUSTOMER_DEMAND_NEED_COVERED",
            "NEED_MARKETPLACE_PUBLISHED",
            "NEED_MARKETPLACE_WITHDRAWN",
            "NEED_MARKETPLACE_UNPUBLISHED_AFTER_RECALCULATION",
            "NEED_RESPONSE_CREATED",
            "NEED_RESPONSE_UPDATED",
            "NEED_RESPONSE_REJECTED",
            "NEED_COVERAGE_CHANGED",
        },
    },
    "orders": {
        "label": "Encomendas",
        "actions": {
            "ORDER_CREATED",
            "ORDER_STATUS_CHANGED",
            "ORDER_RECEIPT_CONFIRMED",
            "ORDER_CANCELLED",
        },
    },
    "support": {
        "label": "Suporte",
        "actions": {action for action in AUDIT_ACTION_LABELS if action.startswith("SUPPORT_")},
    },
    "users": {
        "label": "Utilizadores",
        "actions": {action for action in AUDIT_ACTION_LABELS if action.startswith("USER_")},
    },
    "catalog": {
        "label": "Catálogo",
        "actions": {
            action for action in AUDIT_ACTION_LABELS
            if action.startswith("PRODUCT_") or action.startswith("CATEGORY_")
        },
    },
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


def _short_identifier(value):
    if not value:
        return "—"
    raw = str(value)
    return raw if len(raw) <= 12 else f"{raw[:8]}…{raw[-4:]}"


def _action_meta(action):
    if action in AUDIT_ACTION_META:
        return AUDIT_ACTION_META[action]
    if action.startswith("USER_"):
        return {"category": "Utilizador", "icon": "bi-person", "tone": "info"}
    if action.startswith("SUPPORT_"):
        return {"category": "Suporte", "icon": "bi-life-preserver", "tone": "info"}
    if action.startswith(("PRODUCT_", "CATEGORY_")):
        return {"category": "Catálogo", "icon": "bi-box-seam", "tone": "info"}
    return {"category": "Sistema", "icon": "bi-activity", "tone": "neutral"}


def _entity_label(log):
    entity_type = getattr(log, "entity_type", None) or ""
    label = AUDIT_ENTITY_LABELS.get(entity_type, entity_type.replace("_", " ").title() or "Sem entidade")
    entity_id = getattr(log, "entity_id", None)
    return {
        "label": label,
        "technical_type": entity_type or "—",
        "id": str(entity_id or ""),
        "short_id": _short_identifier(entity_id),
    }


def _entity_detail_url(log):
    entity_type = getattr(log, "entity_type", None) or ""
    entity_id = getattr(log, "entity_id", None)
    if not entity_id:
        return ""
    if entity_type == "users":
        return reverse("dashboard:gestor_utilizador_detalhe", kwargs={"user_id": entity_id})
    if entity_type == "products":
        return reverse("dashboard:gestor_produto_detalhe", kwargs={"product_id": entity_id})
    if entity_type == "support_tickets":
        return reverse("support:admin_ticket_detail", kwargs={"ticket_id": entity_id})
    if entity_type in {
        "marketplace_listings",
        "orders",
        "stocks",
        "needs",
        "external_customer_demands",
        "production_forecasts",
        "stock_movements",
    }:
        return reverse(
            "dashboard:gestor_auditoria_entidade",
            kwargs={"entity_type": entity_type, "entity_id": entity_id},
        )
    return ""


def _event_description(log, action_label):
    notes = (getattr(log, "notes", None) or "").strip()
    if notes:
        return notes

    fallback_descriptions = {
        "USER_LOGIN": "O utilizador iniciou sessão na plataforma.",
        "USER_PASSWORD_CHANGED": "A palavra-passe foi alterada e a segurança da sessão foi atualizada.",
        "USER_PASSWORD_RESET_COMPLETED": "O utilizador concluiu a recuperação de palavra-passe.",
        "USER_PROFILE_UPDATED": "Foram atualizados dados de identidade da conta.",
        "USER_PRODUCER_PROFILE_UPDATED": "Foram atualizados dados operacionais do perfil de produtor.",
        "USER_PREFERENCES_UPDATED": "Foram alteradas preferências de utilização ou notificações.",
        "SUPPORT_TICKET_CREATED": "Foi criado um novo pedido de suporte.",
        "SUPPORT_TICKET_REPLIED": "Foi enviada uma resposta num pedido de suporte.",
        "SUPPORT_TICKET_CLOSED": "Um pedido de suporte foi marcado como resolvido.",
    }
    return fallback_descriptions.get(log.action, f"Evento registado: {action_label}.")


def _change_summary(changes):
    if not changes:
        return "Sem alterações de campos registadas."
    if len(changes) == 1:
        return f"Foi alterado 1 campo: {changes[0]['label']}."
    visible_labels = ", ".join(change["label"] for change in changes[:3])
    suffix = "" if len(changes) <= 3 else f" e mais {len(changes) - 3}"
    return f"Foram alterados {len(changes)} campos: {visible_labels}{suffix}."


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
        action_label = AUDIT_ACTION_LABELS.get(log.action, log.action)
        changes = audit_change_rows(log)
        meta = _action_meta(log.action)
        rows.append(
            {
                "log": log,
                "action_label": action_label,
                "action_meta": meta,
                "changes": changes,
                "change_summary": _change_summary(changes),
                "device_label": device["label"] if log.user_agent else "—",
                "actor_label": actor_label(log.user),
                "entity": _entity_label(log),
                "entity_url": _entity_detail_url(log),
                "event_description": _event_description(log, action_label),
                "ip_label": getattr(log, "ip_address", None) or "Não registado",
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


def _safe_uuid(value):
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _audit_filter_values(*, q="", module="", action="", user_id="", date_from="", date_to="", entity_type=""):
    return {
        "q": (q or "").strip()[:180],
        "module": module if module in AUDIT_MODULES else "",
        "action": action if action in AUDIT_ACTION_LABELS else "",
        "user_id": str(_safe_uuid(user_id) or ""),
        "date_from": str(parse_date(date_from) or ""),
        "date_to": str(parse_date(date_to) or ""),
        "entity_type": entity_type if entity_type in AUDIT_ENTITY_LABELS else "",
    }


def get_filtered_admin_audit_queryset(*, q="", module="", action="", user_id="", date_from="", date_to="", entity_type=""):
    filters = _audit_filter_values(
        q=q,
        module=module,
        action=action,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
        entity_type=entity_type,
    )
    q = (q or "").strip()
    logs = (
        AuditLog.objects.select_related("user")
        .annotate(
            old_values_text=Cast("old_values", models.TextField()),
            new_values_text=Cast("new_values", models.TextField()),
        )
        .order_by("-created_at")
    )

    if filters["q"]:
        q = filters["q"]
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

    if filters["module"]:
        logs = logs.filter(action__in=AUDIT_MODULES[filters["module"]]["actions"])
    if filters["action"]:
        logs = logs.filter(action=filters["action"])
    if filters["user_id"]:
        logs = logs.filter(user_id=filters["user_id"])
    if filters["entity_type"]:
        logs = logs.filter(entity_type=filters["entity_type"])
    if filters["date_from"]:
        logs = logs.filter(created_at__date__gte=filters["date_from"])
    if filters["date_to"]:
        logs = logs.filter(created_at__date__lte=filters["date_to"])

    return logs, filters


def build_admin_audit_context(
    *,
    q="",
    module="",
    action="",
    user_id="",
    date_from="",
    date_to="",
    entity_type="",
    per_page_value=None,
    page_number=None,
):
    per_page = audit_per_page(per_page_value)
    logs, filters = get_filtered_admin_audit_queryset(
        q=q,
        module=module,
        action=action,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
        entity_type=entity_type,
    )
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

    filter_query = urlencode(
        {
            **{key: value for key, value in filters.items() if value},
            "per_page": per_page,
        }
    )
    actor_ids = (
        AuditLog.objects
        .exclude(user_id__isnull=True)
        .values_list("user_id", flat=True)
        .distinct()
    )
    return {
        "admin_tab": "auditoria",
        "logs": page_obj.object_list,
        "audit_rows": build_user_activity_rows(page_obj.object_list),
        "page_obj": page_obj,
        "page_range": page_range,
        "per_page": per_page,
        "per_page_options": [10, 25, 50],
        **filters,
        "filter_query": filter_query,
        "module_options": [
            {"value": key, "label": data["label"]}
            for key, data in AUDIT_MODULES.items()
        ],
        "action_options": sorted(
            [{"value": key, "label": label} for key, label in AUDIT_ACTION_LABELS.items()],
            key=lambda option: option["label"].lower(),
        ),
        "entity_options": sorted(
            [{"value": key, "label": label} for key, label in AUDIT_ENTITY_LABELS.items()],
            key=lambda option: option["label"].lower(),
        ),
        "actor_options": User.objects.filter(id__in=actor_ids).order_by("first_name", "last_name", "email"),
    }


def get_admin_audit_export_rows(**filters):
    logs, _ = get_filtered_admin_audit_queryset(**filters)
    return build_user_activity_rows(logs)


def build_admin_audit_entity_context(*, entity_type, entity_id):
    loaders = {
        "marketplace_listings": _audit_listing_entity,
        "orders": _audit_order_entity,
        "stocks": _audit_stock_entity,
        "needs": _audit_need_entity,
        "external_customer_demands": _audit_external_demand_entity,
        "production_forecasts": _audit_forecast_entity,
        "stock_movements": _audit_stock_movement_entity,
    }
    loader = loaders.get(entity_type)
    if not loader:
        return None
    entity = loader(entity_id)
    logs = AuditLog.objects.filter(entity_type=entity_type, entity_id=entity_id).select_related("user").order_by("-created_at")[:25]
    return {
        "admin_tab": "auditoria",
        "entity_type": entity_type,
        "entity_label": AUDIT_ENTITY_LABELS.get(entity_type, entity_type),
        "entity_id": entity_id,
        "entity": entity,
        "audit_rows": build_user_activity_rows(logs),
    }


def _audit_listing_entity(entity_id):
    from apps.marketplace.models import MarketplaceListing
    listing = MarketplaceListing.objects.filter(id=entity_id).select_related("product", "producer", "producer__user").first()
    if not listing:
        return None
    return {
        "title": listing.product.name,
        "subtitle": "Anúncio / proposta privada",
        "status": listing.get_status_display(),
        "facts": [
            ("Produtor", actor_label(listing.producer.user)),
            ("Quantidade total", listing.quantity_total),
            ("Disponível", listing.quantity_available),
            ("Reservado", listing.quantity_reserved),
            ("Preço unitário", f"{listing.unit_price} EUR"),
            ("Necessidade associada", listing.need_id or "Não"),
        ],
    }


def _audit_order_entity(entity_id):
    from apps.orders.models import Order
    order = Order.objects.filter(id=entity_id).select_related("buyer_producer", "buyer_producer__user").first()
    if not order:
        return None
    return {
        "title": f"Encomenda #{order.order_number}",
        "subtitle": "Transação comercial",
        "status": order.get_status_display(),
        "facts": [
            ("Comprador", actor_label(order.buyer_producer.user)),
            ("Origem", order.get_source_type_display()),
            ("Valor total", f"{order.total_amount} EUR"),
            ("Método de entrega", order.get_delivery_method_display() if order.delivery_method else "Não indicado"),
            ("Criada em", order.created_at),
        ],
    }


def _audit_stock_entity(entity_id):
    from apps.inventory.models import Stock
    stock = Stock.objects.filter(id=entity_id).select_related("product", "producer", "producer__user").first()
    if not stock:
        return None
    return {
        "title": stock.product.name,
        "subtitle": "Stock do produtor",
        "status": "Ativo",
        "facts": [
            ("Produtor", actor_label(stock.producer.user)),
            ("Quantidade atual", stock.current_quantity),
            ("Reservado", stock.reserved_quantity),
            ("Disponível", stock.available_quantity),
            ("Compromissos externos", stock.safety_stock),
        ],
    }


def _audit_need_entity(entity_id):
    from apps.needs.models import Need
    need = Need.objects.filter(id=entity_id).select_related("product", "producer", "producer__user").first()
    if not need:
        return None
    return {
        "title": need.product.name,
        "subtitle": "Procura agregada",
        "status": need.get_status_display(),
        "facts": [
            ("Produtor", actor_label(need.producer.user)),
            ("Quantidade necessária", need.required_quantity),
            ("Data limite", need.needed_by_date),
            ("Origem", need.get_source_system_display()),
        ],
    }


def _audit_external_demand_entity(entity_id):
    from apps.needs.models import ExternalCustomerDemand
    demand = ExternalCustomerDemand.objects.filter(id=entity_id).select_related("product", "producer", "producer__user").first()
    if not demand:
        return None
    return {
        "title": demand.client_name,
        "subtitle": f"Pedido externo - {demand.product.name}",
        "status": demand.get_status_display(),
        "facts": [
            ("Produtor", actor_label(demand.producer.user)),
            ("Produto", demand.product.name),
            ("Quantidade pedida", demand.requested_quantity),
            ("Entrega pretendida", demand.requested_delivery_date),
            ("Procura gerada", demand.generated_need_id or "Sem procura"),
        ],
    }


def _audit_forecast_entity(entity_id):
    from apps.inventory.models import ProductionForecast
    forecast = ProductionForecast.objects.filter(id=entity_id).select_related("product", "producer", "producer__user").first()
    if not forecast:
        return None
    return {
        "title": forecast.product.name,
        "subtitle": "Produção futura",
        "status": "Publicável" if forecast.is_marketplace_enabled else "Interna",
        "facts": [
            ("Produtor", actor_label(forecast.producer.user)),
            ("Quantidade prevista", forecast.forecast_quantity),
            ("Reservada", forecast.reserved_quantity),
            ("Início", forecast.period_start),
            ("Fim", forecast.period_end),
        ],
    }


def _audit_stock_movement_entity(entity_id):
    from apps.inventory.models import StockMovement
    movement = StockMovement.objects.filter(id=entity_id).select_related("stock", "stock__product", "stock__producer", "stock__producer__user").first()
    if not movement:
        return None
    return {
        "title": movement.stock.product.name,
        "subtitle": "Movimento de stock",
        "status": movement.get_movement_type_display(),
        "facts": [
            ("Produtor", actor_label(movement.stock.producer.user)),
            ("Impacto", movement.quantity_delta),
            ("Referência", movement.reference_type or "Sem referência"),
            ("Notas", movement.notes or "Sem notas"),
        ],
    }
