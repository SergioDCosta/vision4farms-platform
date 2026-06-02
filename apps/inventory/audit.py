from decimal import Decimal

from apps.common.audit import log_audit_event


def _audit_qty(value):
    return str(Decimal(str(value or 0)).quantize(Decimal("0.001")))


def _stock_audit_values(stock):
    return {
        "stock_id": str(stock.id),
        "producer_id": str(stock.producer_id),
        "product_id": str(stock.product_id),
        "product_name": getattr(getattr(stock, "product", None), "name", None),
        "current_quantity": _audit_qty(stock.current_quantity),
        "reserved_quantity": _audit_qty(stock.reserved_quantity),
        "safety_stock": _audit_qty(stock.safety_stock),
    }


def _forecast_audit_values(forecast):
    return {
        "forecast_id": str(forecast.id),
        "producer_id": str(forecast.producer_id),
        "product_id": str(forecast.product_id),
        "product_name": getattr(getattr(forecast, "product", None), "name", None),
        "forecast_quantity": _audit_qty(forecast.forecast_quantity),
        "reserved_quantity": _audit_qty(forecast.reserved_quantity),
        "period_start": str(forecast.period_start) if forecast.period_start else None,
        "period_end": str(forecast.period_end) if forecast.period_end else None,
        "is_marketplace_enabled": bool(forecast.is_marketplace_enabled),
    }


def _log_stock_movement(movement, *, actor=None):
    log_audit_event(
        actor=actor,
        action="STOCK_MOVEMENT_CREATED",
        entity_type="stock_movements",
        entity_id=movement.id,
        notes=movement.notes or "Movimento de stock registado.",
        new_values={
            "stock_id": str(movement.stock_id),
            "product_id": str(movement.stock.product_id),
            "product_name": getattr(getattr(movement.stock, "product", None), "name", None),
            "movement_type": movement.movement_type,
            "quantity_delta": _audit_qty(movement.quantity_delta),
            "reference_type": movement.reference_type,
            "reference_id": str(movement.reference_id) if movement.reference_id else None,
        },
    )
