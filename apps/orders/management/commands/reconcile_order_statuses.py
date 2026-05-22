from django.core.management.base import BaseCommand

from apps.orders.models import Order
from apps.orders.services import _create_status_history, _set_order_status, compute_order_status_from_db


class Command(BaseCommand):
    help = (
        "Reconcilia o estado global das encomendas com base no estado atual dos items. "
        "Por omissão corre em modo dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica as alterações na base de dados.",
        )
        parser.add_argument(
            "--order-id",
            dest="order_id",
            help="Opcional: reconcilia apenas uma encomenda específica (UUID).",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        order_id = (options.get("order_id") or "").strip()

        orders_qs = Order.objects.all().order_by("created_at")
        if order_id:
            orders_qs = orders_qs.filter(id=order_id)

        inspected = 0
        changed = 0

        for order in orders_qs.iterator():
            inspected += 1
            expected_status = compute_order_status_from_db(
                order.id,
                current_status=order.status,
            )

            if order.status == expected_status:
                continue

            changed += 1
            line = (
                f"Encomenda #{order.order_number} ({order.id}): "
                f"{order.status} -> {expected_status}"
            )

            if apply_changes:
                previous_status = order.status
                _set_order_status(order, expected_status)
                _create_status_history(
                    order=order,
                    status=expected_status,
                    changed_by=None,
                    notes=(
                        "Reconciliação técnica automática: "
                        f"{previous_status} -> {expected_status}."
                    ),
                )
                self.stdout.write(self.style.SUCCESS(f"[APLICADO] {line}"))
            else:
                self.stdout.write(self.style.WARNING(f"[DRY-RUN] {line}"))

        mode = "APPLY" if apply_changes else "DRY-RUN"
        self.stdout.write(f"[{mode}] Inspecionadas: {inspected} | Alterações: {changed}")
