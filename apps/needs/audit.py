from apps.needs.utils import quantize_need_quantity


def audit_quantity(value):
    return str(quantize_need_quantity(value))


def external_demand_audit_values(demand):
    return {
        "producer_id": str(demand.producer_id),
        "product_id": str(demand.product_id),
        "product_name": getattr(getattr(demand, "product", None), "name", None),
        "requested_quantity": audit_quantity(demand.requested_quantity),
        "requested_delivery_date": str(demand.requested_delivery_date),
        "status": demand.status,
        "source_system": demand.source_system,
        "client_name": demand.client_name,
        "generated_need_id": str(demand.generated_need_id) if demand.generated_need_id else None,
    }


def need_audit_values(need, *, plan=None):
    values = {
        "producer_id": str(need.producer_id),
        "product_id": str(need.product_id),
        "product_name": getattr(getattr(need, "product", None), "name", None),
        "required_quantity": audit_quantity(need.required_quantity),
        "needed_by_date": str(need.needed_by_date) if need.needed_by_date else None,
        "status": need.status,
        "source_system": need.source_system,
        "is_marketplace_published": bool(getattr(need, "is_marketplace_published", False)),
        "published_at": (
            str(need.published_at) if getattr(need, "published_at", None) else None
        ),
    }
    if plan is not None:
        values["max_deficit"] = audit_quantity(plan.get("max_deficit"))
        values["first_deficit_date"] = (
            str(plan.get("first_deficit_date")) if plan.get("first_deficit_date") else None
        )
    return values


def need_marketplace_audit_values(need):
    return {
        "need_id": str(need.id),
        "product_id": str(need.product_id),
        "source_system": need.source_system,
        "required_quantity": audit_quantity(need.required_quantity),
        "needed_by_date": str(need.needed_by_date) if need.needed_by_date else None,
        "is_marketplace_published": bool(getattr(need, "is_marketplace_published", False)),
        "published_at": (
            str(need.published_at) if getattr(need, "published_at", None) else None
        ),
    }
