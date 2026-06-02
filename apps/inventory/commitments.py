from decimal import Decimal

from apps.inventory.constants import STOCK_WARNING_MARGIN_RATIO, ZERO
from apps.inventory.models import Stock
from apps.inventory.utils import (
    progress_percent as _progress_percent,
    quantize_stock_quantity as _quantize_stock_quantity,
)


def _commitment_warning_margin(total_external_demand):
    total_external_demand = _quantize_stock_quantity(total_external_demand)
    if total_external_demand <= ZERO:
        return ZERO
    return (total_external_demand * STOCK_WARNING_MARGIN_RATIO).quantize(Decimal("0.001"))


def calculate_inventory_commitment_state(producer, product, stock=None, *, exclude_listing_id=None):
    """
    Fonte central para avaliar stock face a pedidos externos.

    Quando existem pedidos externos, reutiliza o plano temporal de necessidades:
    stock disponível atual + produção prevista disponível até cada data.
    Sem pedidos externos, mantém um fallback operacional simples baseado no
    stock disponível atual.
    """
    if stock is None and producer and product:
        stock = Stock.objects.filter(producer=producer, product=product).first()

    current_quantity = _quantize_stock_quantity(getattr(stock, "current_quantity", 0))
    reserved_quantity = _quantize_stock_quantity(getattr(stock, "reserved_quantity", 0))
    available_stock_now = _quantize_stock_quantity(max(current_quantity - reserved_quantity, ZERO))
    stored_commitments = _quantize_stock_quantity(getattr(stock, "safety_stock", 0))

    from apps.needs.services import calculate_external_demand_plan

    plan = calculate_external_demand_plan(
        producer=producer,
        product=product,
        exclude_listing_id=exclude_listing_id,
    )
    rows = list(plan.get("rows") or [])
    has_external_demands = bool(rows) or _quantize_stock_quantity(plan.get("total_external_demand")) > ZERO

    if has_external_demands:
        total_external_demand = _quantize_stock_quantity(plan.get("total_external_demand"))
        available_stock_now = _quantize_stock_quantity(plan.get("available_stock_now"))
        useful_forecast_total = _quantize_stock_quantity(plan.get("total_forecast_relevant"))
        max_deficit = _quantize_stock_quantity(plan.get("max_deficit"))
        first_deficit_date = plan.get("first_deficit_date")

        margins = [
            _quantize_stock_quantity(
                _quantize_stock_quantity(row.get("capacity_until_date"))
                - _quantize_stock_quantity(row.get("demand_until_date"))
            )
            for row in rows
        ]
        minimum_remaining_capacity = min(margins) if margins else available_stock_now
        temporal_sellable_quantity = _quantize_stock_quantity(max(minimum_remaining_capacity, ZERO))
        warning_margin = _commitment_warning_margin(total_external_demand)

        if max_deficit > ZERO:
            state_key = "critical"
            state_label = "Défice temporal"
            state_message = (
                "O stock atual e a produção prevista útil não chegam a tempo "
                "dos pedidos externos."
            )
            explanation = "A produção prevista registada não cobre o défice até à primeira data crítica."
        elif temporal_sellable_quantity <= warning_margin:
            state_key = "warning"
            state_label = "Coberto no limite"
            state_message = "Pedidos cobertos, mas com pouca margem disponível."
            explanation = "O stock atual e a produção prevista chegam a tempo, mas a margem para venda é curta."
        elif temporal_sellable_quantity > ZERO:
            state_key = "excess"
            state_label = "Coberto"
            state_message = "Pedidos cobertos com stock atual e produção prevista disponível a tempo."
            explanation = "Existe margem temporal que pode ser vendida sem comprometer os pedidos externos registados."
        else:
            state_key = "normal"
            state_label = "Coberto"
            state_message = "Pedidos cobertos com stock atual e produção prevista disponível a tempo."
            explanation = "Não existe défice temporal calculado para os pedidos externos."

        return {
            "has_external_demands": True,
            "total_external_demand": total_external_demand,
            "available_stock_now": available_stock_now,
            "useful_forecast_total": useful_forecast_total,
            "max_deficit": max_deficit,
            "first_deficit_date": first_deficit_date,
            "minimum_remaining_capacity": _quantize_stock_quantity(minimum_remaining_capacity),
            "temporal_sellable_quantity": temporal_sellable_quantity,
            "state_key": state_key,
            "state_label": state_label,
            "state_message": state_message,
            "explanation": explanation,
            "plan": plan,
            "safety_stock": total_external_demand,
            "current_quantity": current_quantity,
            "reserved_quantity": reserved_quantity,
        }

    if available_stock_now > ZERO:
        state_key = "excess"
        state_label = "Disponível"
        state_message = "Sem compromissos externos ativos para este produto."
        explanation = "Todo o stock disponível pode ser avaliado para venda ou uso interno."
    else:
        state_key = "normal"
        state_label = "Normal"
        state_message = "Sem compromissos externos ativos."
        explanation = "Não existem pedidos externos em aberto para este produto."

    return {
        "has_external_demands": False,
        "total_external_demand": Decimal("0.000"),
        "available_stock_now": available_stock_now,
        "useful_forecast_total": Decimal("0.000"),
        "max_deficit": Decimal("0.000"),
        "first_deficit_date": None,
        "minimum_remaining_capacity": available_stock_now,
        "temporal_sellable_quantity": available_stock_now,
        "state_key": state_key,
        "state_label": state_label,
        "state_message": state_message,
        "explanation": explanation,
        "plan": plan,
        "safety_stock": stored_commitments,
        "current_quantity": current_quantity,
        "reserved_quantity": reserved_quantity,
    }


def stock_state(stock, commitment_state=None):
    """
    Estado visual do stock:
    - critical: available_quantity < safety_stock
    - warning: safety_stock <= available_quantity <= safety_stock + 10%
    - excess: available_quantity > safety_stock + 10%
    - normal: sem compromissos externos definidos e sem excedente operacional
    """
    current_quantity = _quantize_stock_quantity(
        commitment_state.get("current_quantity") if commitment_state else getattr(stock, "current_quantity", 0)
    )
    safety_stock = _quantize_stock_quantity(
        commitment_state.get("total_external_demand") if commitment_state else getattr(stock, "safety_stock", 0)
    )
    reserved_quantity = _quantize_stock_quantity(
        commitment_state.get("reserved_quantity") if commitment_state else getattr(stock, "reserved_quantity", 0)
    )
    available_quantity = _quantize_stock_quantity(
        commitment_state.get("available_stock_now") if commitment_state else current_quantity - reserved_quantity
    )

    if commitment_state:
        real_surplus = _quantize_stock_quantity(commitment_state.get("temporal_sellable_quantity"))
        deficit_quantity = _quantize_stock_quantity(commitment_state.get("max_deficit"))
        publishable_quantity = real_surplus
        temporal_capacity_quantity = _quantize_stock_quantity(
            available_quantity + _quantize_stock_quantity(commitment_state.get("useful_forecast_total"))
        )
    else:
        real_surplus = max(available_quantity - safety_stock, ZERO)
        deficit_quantity = max((safety_stock + reserved_quantity) - current_quantity, ZERO)
        publishable_quantity = real_surplus
        temporal_capacity_quantity = available_quantity
    warning_margin_quantity = (
        (safety_stock * STOCK_WARNING_MARGIN_RATIO).quantize(Decimal("0.001"))
        if safety_stock > ZERO
        else ZERO
    )
    warning_upper_quantity = safety_stock + warning_margin_quantity

    state_base = {
        "available_quantity": available_quantity,
        "safety_stock": safety_stock,
        "has_safety_stock": safety_stock > ZERO,
        "reserved_quantity": reserved_quantity,
        "current_quantity": current_quantity,
        "warning_margin_quantity": warning_margin_quantity,
        "warning_upper_quantity": warning_upper_quantity,
        "publishable_quantity": publishable_quantity,
        "real_surplus": real_surplus,
        "deficit_quantity": deficit_quantity,
        "temporal_capacity_quantity": temporal_capacity_quantity,
        "useful_forecast_total": _quantize_stock_quantity(
            commitment_state.get("useful_forecast_total") if commitment_state else 0
        ),
        "minimum_remaining_capacity": _quantize_stock_quantity(
            commitment_state.get("minimum_remaining_capacity") if commitment_state else real_surplus
        ),
        "first_deficit_date": commitment_state.get("first_deficit_date") if commitment_state else None,
        "has_external_demands": bool(commitment_state and commitment_state.get("has_external_demands")),
        "state_message": commitment_state.get("state_message") if commitment_state else "",
        "explanation": commitment_state.get("explanation") if commitment_state else "",
        "safety_progress_percent": _progress_percent(available_quantity, safety_stock),
        "temporal_coverage_progress_percent": _progress_percent(temporal_capacity_quantity, safety_stock),
        "warning_progress_percent": _progress_percent(available_quantity, warning_upper_quantity),
        "reserved_progress_percent": _progress_percent(reserved_quantity, current_quantity),
    }

    if commitment_state:
        key = commitment_state.get("state_key") or "normal"
        if key == "critical":
            return state_base | {
                "key": "critical",
                "label": commitment_state.get("state_label") or "Crítico",
                "row_class": "inv-row--critical",
                "pill_class": "inv-status inv-status--critical",
                "text_class": "inv-value inv-value--critical",
                "publishable_quantity": ZERO,
                "action_type": "recommend",
                "action_label": "Comprar",
                "action_icon": "cart",
                "action_url": "/recomendacoes/",
            }
        if key == "warning":
            return state_base | {
                "key": "warning",
                "label": commitment_state.get("state_label") or "Coberto no limite",
                "row_class": "inv-row--warning",
                "pill_class": "inv-status inv-status--warning",
                "text_class": "inv-value inv-value--warning",
                "publishable_quantity": ZERO,
                "action_type": "monitor",
                "action_label": "Acompanhar",
                "action_icon": "exclamation-triangle",
                "action_url": "/recomendacoes/",
            }
        if key == "excess":
            return state_base | {
                "key": "excess",
                "label": commitment_state.get("state_label") or "Disponível",
                "row_class": "inv-row--excess",
                "pill_class": "inv-status inv-status--excess",
                "text_class": "inv-value inv-value--excess",
                "action_type": "publish",
                "action_label": "Publicar",
                "action_icon": "storefront",
                "action_url": "/marketplace/",
            }
        return state_base | {
            "key": "normal",
            "label": commitment_state.get("state_label") or "Normal",
            "row_class": "",
            "pill_class": "inv-status inv-status--normal",
            "text_class": "inv-value",
            "action_type": "marketplace",
            "action_label": "Marketplace",
            "action_icon": "shop",
            "action_url": "/marketplace/",
        }

    if available_quantity < safety_stock:
        return state_base | {
            "key": "critical",
            "label": "Crítico",
            "row_class": "inv-row--critical",
            "pill_class": "inv-status inv-status--critical",
            "text_class": "inv-value inv-value--critical",
            "publishable_quantity": ZERO,
            "action_type": "recommend",
            "action_label": "Comprar",
            "action_icon": "cart",
            "action_url": "/recomendacoes/",
        }

    if safety_stock > ZERO and available_quantity <= warning_upper_quantity:
        return state_base | {
            "key": "warning",
            "label": "Perto dos compromissos",
            "row_class": "inv-row--warning",
            "pill_class": "inv-status inv-status--warning",
            "text_class": "inv-value inv-value--warning",
            "publishable_quantity": ZERO,
            "action_type": "monitor",
            "action_label": "Acompanhar",
            "action_icon": "exclamation-triangle",
            "action_url": "/recomendacoes/",
        }

    if real_surplus > ZERO:
        return state_base | {
            "key": "excess",
            "label": "Excedente",
            "row_class": "inv-row--excess",
            "pill_class": "inv-status inv-status--excess",
            "text_class": "inv-value inv-value--excess",
            "action_type": "publish",
            "action_label": "Publicar",
            "action_icon": "storefront",
            "action_url": "/marketplace/",
        }

    return state_base | {
        "key": "normal",
        "label": "Normal",
        "row_class": "",
        "pill_class": "inv-status inv-status--normal",
        "text_class": "inv-value",
        "action_type": "marketplace",
        "action_label": "Marketplace",
        "action_icon": "shop",
        "action_url": "/marketplace/",
    }


# ---------------------------------------------------------------------------
# Produtos do produtor / inventário operacional
# ---------------------------------------------------------------------------
