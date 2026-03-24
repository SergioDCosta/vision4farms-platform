import json


def with_htmx_trigger(response, event_name, payload):
    response["HX-Trigger"] = json.dumps({
        event_name: payload
    })
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