from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.common.audit import log_audit_event
from apps.needs.audit import need_marketplace_audit_values
from apps.needs.constants import ACTIVE_NEED_STATUSES, EDITABLE_NEED_STATUSES
from apps.needs.coverage import calculate_need_coverage, recalculate_need_status
from apps.needs.exceptions import DuplicateActiveNeedError
from apps.needs.models import Need, NeedSourceSystem, NeedStatus
from apps.needs.utils import (
    get_need_minimum_edit_quantity,
    normalize_needed_by_date,
    quantize_need_quantity,
)


@transaction.atomic
def update_need(
    *,
    need,
    producer,
    required_quantity,
    needed_by_date=None,
    notes=None,
):
    if not need:
        raise ValidationError("Necessidade inválida.")

    locked_need = (
        Need.objects
        .select_for_update()
        .select_related("product", "producer")
        .get(id=need.id)
    )
    if locked_need.producer_id != producer.id:
        raise ValidationError("Não pode editar esta necessidade.")
    if getattr(locked_need, "source_system", None) == NeedSourceSystem.CUSTOMER_DEMAND:
        raise ValidationError(
            "Esta procura é gerada automaticamente a partir dos pedidos de clientes. "
            "Para alterar a quantidade ou a data, edite os pedidos de origem."
        )
    if locked_need.status not in EDITABLE_NEED_STATUSES:
        raise ValidationError("Esta necessidade já não pode ser editada.")

    quantity = quantize_need_quantity(required_quantity)
    if quantity <= Decimal("0.000"):
        raise ValidationError("A quantidade necessária deve ser superior a zero.")

    coverage = calculate_need_coverage(locked_need)
    minimum_quantity = get_need_minimum_edit_quantity(coverage)
    if quantity < minimum_quantity:
        raise ValidationError(
            f"A quantidade mínima permitida é {minimum_quantity} {locked_need.product.unit}, "
            "porque já existem encomendas associadas."
        )

    normalized_deadline = normalize_needed_by_date(needed_by_date)
    normalized_notes = (notes or "").strip() or None

    changed = (
        locked_need.required_quantity != quantity
        or locked_need.needed_by_date != normalized_deadline
        or (locked_need.notes or None) != normalized_notes
    )

    if changed:
        locked_need.required_quantity = quantity
        locked_need.needed_by_date = normalized_deadline
        locked_need.notes = normalized_notes
        if hasattr(locked_need, "updated_at"):
            locked_need.updated_at = timezone.now()
            locked_need.save(
                update_fields=["required_quantity", "needed_by_date", "notes", "updated_at"]
            )
        else:
            locked_need.save(update_fields=["required_quantity", "needed_by_date", "notes"])

    updated_need, updated_coverage, status_changed = recalculate_need_status(locked_need)
    return updated_need, updated_coverage, bool(changed or status_changed)


def get_need_for_producer(*, producer, need_id):
    return Need.objects.filter(id=need_id, producer=producer).select_related(
        "product",
        "product__category",
        "producer",
        "producer__user",
    ).first()


@transaction.atomic
def create_need(
    *,
    producer,
    product,
    required_quantity,
    needed_by_date=None,
    source_system=NeedSourceSystem.MANUAL,
    external_id=None,
    notes=None,
    acting_user=None,
):
    quantity = quantize_need_quantity(required_quantity)
    if quantity <= Decimal("0.000"):
        raise ValidationError("A quantidade necessária deve ser superior a zero.")

    existing_need = (
        Need.objects
        .select_for_update()
        .filter(
            producer=producer,
            product=product,
            status__in=ACTIVE_NEED_STATUSES,
        )
        .order_by("-updated_at", "-created_at")
        .first()
    )
    if existing_need:
        raise DuplicateActiveNeedError(existing_need)

    is_marketplace_published = source_system != NeedSourceSystem.CUSTOMER_DEMAND
    need = Need.objects.create(
        producer=producer,
        product=product,
        required_quantity=quantity,
        needed_by_date=needed_by_date,
        source_system=source_system,
        external_id=external_id,
        notes=(notes or "").strip() or None,
        status=NeedStatus.OPEN,
        is_marketplace_published=is_marketplace_published,
        published_at=timezone.now() if is_marketplace_published else None,
    )

    need, coverage, _ = recalculate_need_status(need, acting_user=acting_user)
    if is_marketplace_published:
        log_audit_event(
            actor=acting_user,
            action="NEED_MARKETPLACE_PUBLISHED",
            entity_type="needs",
            entity_id=need.id,
            notes="Procura publicada no marketplace ao ser criada explicitamente.",
            new_values=need_marketplace_audit_values(need),
        )
    return need, coverage


@transaction.atomic
def create_or_update_need(**kwargs):
    need, coverage = create_need(**kwargs)
    return need, coverage, True


@transaction.atomic
def ignore_need(*, need, producer):
    if not need or need.producer_id != producer.id:
        raise ValidationError("Necessidade inválida para este produtor.")

    if need.status == NeedStatus.IGNORED:
        return False

    need.status = NeedStatus.IGNORED
    if hasattr(need, "updated_at"):
        need.updated_at = timezone.now()
        need.save(update_fields=["status", "updated_at"])
    else:
        need.save(update_fields=["status"])
    return True


@transaction.atomic
def publish_need_to_marketplace(*, need, producer, acting_user=None):
    if not need:
        raise ValidationError("Procura inválida.")

    locked_need = (
        Need.objects
        .select_for_update()
        .select_related("product", "producer")
        .get(id=need.id)
    )
    if locked_need.producer_id != producer.id:
        raise ValidationError("Não pode publicar esta procura.")
    if locked_need.status not in ACTIVE_NEED_STATUSES:
        raise ValidationError("Apenas procuras abertas ou parcialmente cobertas podem ser publicadas.")

    coverage = calculate_need_coverage(locked_need)
    if coverage["remaining_to_plan"] <= Decimal("0.000"):
        raise ValidationError("Esta procura já não tem quantidade por cobrir.")

    if locked_need.source_system == NeedSourceSystem.CUSTOMER_DEMAND:
        from apps.needs.services import calculate_external_demand_plan

        plan = calculate_external_demand_plan(
            producer=locked_need.producer,
            product=locked_need.product,
        )
        if quantize_need_quantity(plan.get("max_deficit")) <= Decimal("0.000"):
            raise ValidationError("Os pedidos de clientes já estão cobertos; não existe défice para publicar.")

    if getattr(locked_need, "is_marketplace_published", False):
        return locked_need, False

    locked_need.is_marketplace_published = True
    locked_need.published_at = timezone.now()
    update_fields = ["is_marketplace_published", "published_at"]
    if hasattr(locked_need, "updated_at"):
        locked_need.updated_at = timezone.now()
        update_fields.append("updated_at")
    locked_need.save(update_fields=update_fields)
    log_audit_event(
        actor=acting_user,
        action="NEED_MARKETPLACE_PUBLISHED",
        entity_type="needs",
        entity_id=locked_need.id,
        notes="Procura publicada no marketplace pelo produtor.",
        new_values=need_marketplace_audit_values(locked_need),
    )
    return locked_need, True


@transaction.atomic
def withdraw_need_from_marketplace(*, need, producer, acting_user=None):
    if not need:
        raise ValidationError("Procura inválida.")

    locked_need = (
        Need.objects
        .select_for_update()
        .select_related("product", "producer")
        .get(id=need.id)
    )
    if locked_need.producer_id != producer.id:
        raise ValidationError("Não pode retirar esta procura.")
    if not getattr(locked_need, "is_marketplace_published", False):
        return locked_need, False

    previous_values = need_marketplace_audit_values(locked_need)
    locked_need.is_marketplace_published = False
    update_fields = ["is_marketplace_published"]
    if hasattr(locked_need, "updated_at"):
        locked_need.updated_at = timezone.now()
        update_fields.append("updated_at")
    locked_need.save(update_fields=update_fields)
    log_audit_event(
        actor=acting_user,
        action="NEED_MARKETPLACE_WITHDRAWN",
        entity_type="needs",
        entity_id=locked_need.id,
        notes="Procura retirada do marketplace pelo produtor.",
        old_values=previous_values,
        new_values=need_marketplace_audit_values(locked_need),
    )
    return locked_need, True
