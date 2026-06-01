import json


def is_htmx_request(request):
    return getattr(request, "htmx", False) or request.headers.get("HX-Request") == "true"


def _decode_hx_trigger(value):
    if not value:
        return {}

    raw_value = str(value).strip()
    if not raw_value:
        return {}

    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        return {raw_value: {}}

    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, str) and parsed:
        return {parsed: {}}
    return {}


def with_htmx_trigger(response, event_name, payload):
    triggers = _decode_hx_trigger(response.get("HX-Trigger"))
    triggers[event_name] = payload
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def with_htmx_toast(response, level, message):
    return with_htmx_trigger(
        response,
        "app:toast",
        {
            "level": level,
            "message": message,
        },
    )
