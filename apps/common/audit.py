import logging

logger = logging.getLogger(__name__)


def get_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR") if request else None
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") if request else None


def describe_user_agent(user_agent):
    raw = (user_agent or "").strip()
    normalized = raw.lower()

    if not raw:
        device_type = "Desconhecido"
    elif "bot" in normalized or "crawler" in normalized or "spider" in normalized:
        device_type = "Bot/Automação"
    elif "ipad" in normalized or "tablet" in normalized:
        device_type = "Tablet"
    elif "mobile" in normalized or "iphone" in normalized or "android" in normalized:
        device_type = "Telemóvel"
    else:
        device_type = "Computador"

    if "edg/" in normalized or "edge/" in normalized:
        browser = "Microsoft Edge"
    elif "opr/" in normalized or "opera" in normalized:
        browser = "Opera"
    elif "firefox/" in normalized:
        browser = "Firefox"
    elif "chrome/" in normalized and "chromium" not in normalized:
        browser = "Chrome"
    elif "safari/" in normalized and "chrome/" not in normalized:
        browser = "Safari"
    elif "msie" in normalized or "trident/" in normalized:
        browser = "Internet Explorer"
    else:
        browser = "Browser desconhecido"

    if "windows" in normalized:
        operating_system = "Windows"
    elif "iphone" in normalized or "ipad" in normalized or "ios" in normalized:
        operating_system = "iOS"
    elif "android" in normalized:
        operating_system = "Android"
    elif "mac os" in normalized or "macintosh" in normalized:
        operating_system = "macOS"
    elif "linux" in normalized:
        operating_system = "Linux"
    else:
        operating_system = "SO desconhecido"

    label_parts = [device_type]
    if browser != "Browser desconhecido":
        label_parts.append(browser)
    if operating_system != "SO desconhecido":
        label_parts.append(operating_system)

    return {
        "raw": raw,
        "device_type": device_type,
        "browser": browser,
        "operating_system": operating_system,
        "label": " · ".join(label_parts),
    }


def log_audit_event(
    *,
    request=None,
    user=None,
    action,
    entity_type=None,
    entity_id=None,
    notes=None,
    old_values=None,
    new_values=None,
    actor=None,
):
    from apps.dashboard.models import AuditLog

    audit_user = actor
    if audit_user is None and request is not None:
        audit_user = getattr(request, "current_user", None)
    if audit_user is None:
        audit_user = user

    try:
        return AuditLog.objects.create(
            user=audit_user,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            old_values=old_values,
            new_values=new_values,
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT") if request else None,
            notes=notes,
        )
    except Exception:
        logger.exception("Nao foi possivel registar evento de auditoria %s.", action)
        return None
