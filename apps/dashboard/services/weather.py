import logging
from datetime import date
from decimal import Decimal, InvalidOperation
import unicodedata

import requests
from django.core.cache import caches
from django.utils import timezone


logger = logging.getLogger(__name__)

IPMA_LOCATIONS_URL = "https://api.ipma.pt/open-data/distrits-islands.json"
IPMA_FORECAST_URL_TEMPLATE = (
    "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily/{global_id}.json"
)
IPMA_WEATHER_TYPES_URL = "https://api.ipma.pt/open-data/weather-type-classe.json"

WEATHER_CACHE_ALIAS = "weather"
LOCATIONS_CACHE_KEY = "ipma:locations:lookup:v1"
WEATHER_TYPES_CACHE_KEY = "ipma:weather-types:lookup:v1"
LOCATIONS_CACHE_TTL = 24 * 60 * 60
FORECAST_CACHE_TTL = 30 * 60
FORECAST_HORIZON_DAYS = 5
TREND_THRESHOLD_DEGREES = Decimal("2.0")

HTTP_TIMEOUT_SECONDS = (1.5, 2.5)
REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Cooperativa/1.0 (+https://cooperativav4f.up.railway.app)",
}

DISTRICT_TO_REFERENCE_CITY = {
    "aveiro": "aveiro",
    "beja": "beja",
    "braga": "braga",
    "braganca": "braganca",
    "castelo branco": "castelo branco",
    "coimbra": "coimbra",
    "evora": "evora",
    "faro": "faro",
    "guarda": "guarda",
    "leiria": "leiria",
    "lisboa": "lisboa",
    "portalegre": "portalegre",
    "porto": "porto",
    "santarem": "santarem",
    "setubal": "setubal",
    "viana do castelo": "viana do castelo",
    "vila real": "vila real",
    "viseu": "viseu",
    "acores": "ponta delgada",
    "regiao autonoma dos acores": "ponta delgada",
    "regiao autonoma da madeira": "funchal",
    "madeira": "funchal",
}


WEEKDAY_PT_SHORT = {
    0: "Seg",
    1: "Ter",
    2: "Qua",
    3: "Qui",
    4: "Sex",
    5: "Sáb",
    6: "Dom",
}


def _normalize_text(value):
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _safe_cache():
    try:
        return caches[WEATHER_CACHE_ALIAS]
    except Exception:
        return caches["default"]


def _safe_cache_get(key):
    try:
        return _safe_cache().get(key)
    except Exception:
        logger.exception("Falha ao ler cache de meteorologia.")
        return None


def _safe_cache_set(key, value, timeout):
    try:
        _safe_cache().set(key, value, timeout=timeout)
    except Exception:
        logger.exception("Falha ao escrever cache de meteorologia.")


def _fetch_json(url):
    response = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS, headers=REQUEST_HEADERS)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _to_decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_locations_lookup(payload):
    lookup = {}
    for row in payload.get("data", []):
        local_name = " ".join(str(row.get("local") or "").split()).strip()
        global_id = row.get("globalIdLocal")
        if not local_name or global_id is None:
            continue

        try:
            parsed_global_id = int(global_id)
        except (TypeError, ValueError):
            continue

        normalized_name = _normalize_text(local_name)
        if not normalized_name:
            continue

        lookup[normalized_name] = {
            "global_id": parsed_global_id,
            "local_name": local_name,
        }
    return lookup


def _build_weather_types_lookup(payload):
    weather_types = {}
    for row in payload.get("data", []):
        weather_type_id = row.get("idWeatherType")
        description = " ".join(str(row.get("descWeatherTypePT") or "").split()).strip()
        if weather_type_id is None or not description:
            continue
        try:
            weather_types[int(weather_type_id)] = description
        except (TypeError, ValueError):
            continue
    return weather_types


def _get_locations_lookup():
    cached = _safe_cache_get(LOCATIONS_CACHE_KEY)
    if isinstance(cached, dict) and cached:
        return cached

    payload = _fetch_json(IPMA_LOCATIONS_URL)
    lookup = _build_locations_lookup(payload)
    if lookup:
        _safe_cache_set(LOCATIONS_CACHE_KEY, lookup, LOCATIONS_CACHE_TTL)
    return lookup


def _get_weather_types_lookup():
    cached = _safe_cache_get(WEATHER_TYPES_CACHE_KEY)
    if isinstance(cached, dict) and cached:
        return cached

    payload = _fetch_json(IPMA_WEATHER_TYPES_URL)
    lookup = _build_weather_types_lookup(payload)
    if lookup:
        _safe_cache_set(WEATHER_TYPES_CACHE_KEY, lookup, LOCATIONS_CACHE_TTL)
    return lookup


def _resolve_location(city, district, locations_lookup):
    normalized_city = _normalize_text(city)
    if normalized_city and normalized_city in locations_lookup:
        return locations_lookup[normalized_city]

    normalized_district = _normalize_text(district)
    if normalized_district and normalized_district in locations_lookup:
        return locations_lookup[normalized_district]

    if normalized_district:
        fallback_city = DISTRICT_TO_REFERENCE_CITY.get(normalized_district)
        if fallback_city and fallback_city in locations_lookup:
            return locations_lookup[fallback_city]

    return None


def _get_forecast_data(global_id):
    today = timezone.localdate()
    forecast_cache_key = f"ipma:forecast:{global_id}:{today.isoformat()}:h{FORECAST_HORIZON_DAYS}"
    cached = _safe_cache_get(forecast_cache_key)
    if isinstance(cached, dict) and cached:
        return cached

    payload = _fetch_json(IPMA_FORECAST_URL_TEMPLATE.format(global_id=global_id))
    rows = payload.get("data") or []
    if not isinstance(rows, list) or not rows:
        return None

    parsed_rows = []
    for row in rows:
        forecast_date_raw = str(row.get("forecastDate") or "").strip()
        try:
            forecast_date = date.fromisoformat(forecast_date_raw)
        except ValueError:
            continue
        if forecast_date < today:
            continue

        parsed_rows.append(
            {
                "forecast_date": forecast_date,
                "temperature_min": row.get("tMin"),
                "temperature_max": row.get("tMax"),
                "weather_type_id": row.get("idWeatherType"),
                "precipitation_probability": row.get("precipitaProb"),
            }
        )

    if not parsed_rows:
        return None

    parsed_rows.sort(key=lambda item: item["forecast_date"])
    parsed = {
        "days": parsed_rows[:FORECAST_HORIZON_DAYS],
    }
    _safe_cache_set(forecast_cache_key, parsed, FORECAST_CACHE_TTL)
    return parsed


def _weather_icon_for_label(weather_label):
    normalized_label = _normalize_text(weather_label)
    if not normalized_label:
        return "bi-cloud-sun"
    if "trovoada" in normalized_label:
        return "bi-cloud-lightning-rain"
    if "neve" in normalized_label or "granizo" in normalized_label:
        return "bi-cloud-snow"
    if "nevoeiro" in normalized_label:
        return "bi-cloud-fog2"
    if "chuva" in normalized_label or "aguaceiro" in normalized_label:
        return "bi-cloud-rain"
    if "encoberto" in normalized_label or "nublado" in normalized_label:
        return "bi-cloudy"
    if "limpo" in normalized_label or "pouco nublado" in normalized_label:
        return "bi-brightness-high"
    return "bi-cloud-sun"


def _build_daily_forecast_rows(*, forecast_days, weather_type_lookup, today):
    output = []
    for row in forecast_days:
        forecast_date = row.get("forecast_date")
        if not isinstance(forecast_date, date):
            continue

        weather_type_id = _to_int(row.get("weather_type_id"))
        weather_type_label = weather_type_lookup.get(weather_type_id, "") if weather_type_id is not None else ""
        precipitation_probability = _to_decimal(row.get("precipitation_probability"))
        offset_days = (forecast_date - today).days

        output.append(
            {
                "forecast_date": forecast_date.isoformat(),
                "day_label": "Hoje" if offset_days == 0 else WEEKDAY_PT_SHORT.get(forecast_date.weekday(), forecast_date.strftime("%a")),
                "date_label": forecast_date.strftime("%d/%m"),
                "offset_days": offset_days,
                "is_today": offset_days == 0,
                "temperature_min": row.get("temperature_min"),
                "temperature_max": row.get("temperature_max"),
                "temperature_min_numeric": _to_decimal(row.get("temperature_min")),
                "temperature_max_numeric": _to_decimal(row.get("temperature_max")),
                "weather_type_id": weather_type_id,
                "weather_type_label": weather_type_label,
                "weather_icon": _weather_icon_for_label(weather_type_label),
                "precipitation_probability": float(precipitation_probability) if precipitation_probability is not None else None,
                "is_wet_risk": (
                    (precipitation_probability is not None and precipitation_probability >= Decimal("40"))
                    or "chuva" in _normalize_text(weather_type_label)
                    or "aguaceiro" in _normalize_text(weather_type_label)
                    or "trovoada" in _normalize_text(weather_type_label)
                ),
            }
        )
    return output


def _build_temperature_trend(daily_forecast):
    if not daily_forecast:
        return {"key": "stable", "label": "Estável", "delta": 0.0}

    upcoming = [day for day in daily_forecast if day.get("offset_days", 0) > 0]
    if not upcoming:
        return {"key": "stable", "label": "Estável", "delta": 0.0}

    first_max = upcoming[0].get("temperature_max_numeric")
    last_max = upcoming[-1].get("temperature_max_numeric")
    if first_max is None or last_max is None:
        return {"key": "stable", "label": "Estável", "delta": 0.0}

    delta = last_max - first_max
    if delta >= TREND_THRESHOLD_DEGREES:
        return {"key": "rising", "label": "A subir", "delta": float(delta)}
    if delta <= -TREND_THRESHOLD_DEGREES:
        return {"key": "falling", "label": "A descer", "delta": float(delta)}
    return {"key": "stable", "label": "Estável", "delta": float(delta)}


def _build_temperature_badge(today_max):
    if today_max is None:
        return {"key": "unknown", "label": "Sem leitura"}
    if today_max < Decimal("12"):
        return {"key": "cold", "label": "Frio"}
    if today_max <= Decimal("24"):
        return {"key": "mild", "label": "Ameno"}
    return {"key": "hot", "label": "Quente"}


def _build_location_context(city, district, mode):
    clean_city = " ".join(str(city or "").split()).strip()
    clean_district = " ".join(str(district or "").split()).strip()
    if mode == "city":
        if clean_city and clean_district:
            return f"{clean_city}, {clean_district}"
        return clean_city or clean_district
    return clean_district or clean_city


def _degraded_weather(message, *, location_context=""):
    return {
        "state": "degraded",
        "message": message,
        "location_context": location_context,
    }


def get_dashboard_weather_snapshot(*, city=None, district=None):
    location_context = _build_location_context(city, district, mode="city")
    if not location_context.strip():
        return _degraded_weather(
            "Sem localização definida no perfil para consultar meteorologia.",
            location_context="",
        )

    try:
        locations_lookup = _get_locations_lookup()
    except requests.RequestException:
        return _degraded_weather(
            "Serviço de meteorologia temporariamente indisponível.",
            location_context=location_context,
        )
    except Exception:
        logger.exception("Falha inesperada ao obter localizações IPMA.")
        return _degraded_weather(
            "Não foi possível obter meteorologia neste momento.",
            location_context=location_context,
        )

    if not locations_lookup:
        return _degraded_weather(
            "IPMA sem dados de localização disponíveis de momento.",
            location_context=location_context,
        )

    location_entry = _resolve_location(city, district, locations_lookup)
    if not location_entry:
        return _degraded_weather(
            "Localização não suportada pelo IPMA para previsão automática.",
            location_context=location_context,
        )

    try:
        forecast = _get_forecast_data(location_entry["global_id"])
    except requests.RequestException:
        return _degraded_weather(
            "Serviço de previsão indisponível. Tenta novamente dentro de instantes.",
            location_context=location_context,
        )
    except Exception:
        logger.exception("Falha inesperada ao obter previsão IPMA.")
        return _degraded_weather(
            "Erro ao carregar previsão do dia.",
            location_context=location_context,
        )

    if not forecast or not forecast.get("days"):
        return _degraded_weather(
            "Sem previsão disponível para hoje na localização selecionada.",
            location_context=location_context,
        )

    weather_type_lookup = {}
    try:
        weather_type_lookup = _get_weather_types_lookup()
    except requests.RequestException:
        weather_type_lookup = {}
    except Exception:
        logger.exception("Falha inesperada ao obter classes de tempo do IPMA.")
        weather_type_lookup = {}

    today = timezone.localdate()
    daily_forecast = _build_daily_forecast_rows(
        forecast_days=forecast["days"],
        weather_type_lookup=weather_type_lookup,
        today=today,
    )

    if not daily_forecast:
        return _degraded_weather(
            "Sem previsão disponível para hoje na localização selecionada.",
            location_context=location_context,
        )

    today_forecast = next((day for day in daily_forecast if day["is_today"]), daily_forecast[0])
    temperature_trend = _build_temperature_trend(daily_forecast)
    temperature_badge = _build_temperature_badge(today_forecast.get("temperature_max_numeric"))

    return {
        "state": "success",
        "location_context": location_context,
        "location_label": location_entry["local_name"],
        "temperature_min": today_forecast.get("temperature_min"),
        "temperature_max": today_forecast.get("temperature_max"),
        "weather_type_id": today_forecast.get("weather_type_id"),
        "weather_type_label": today_forecast.get("weather_type_label"),
        "weather_icon": today_forecast.get("weather_icon"),
        "forecast_date": today_forecast.get("forecast_date"),
        "daily_forecast": daily_forecast,
        "temperature_trend": temperature_trend,
        "temperature_badge": temperature_badge,
        "fetched_at": timezone.now(),
    }
