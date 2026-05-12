from django.core.management.base import BaseCommand

from apps.alerts.services import run_operational_alerts_job


class Command(BaseCommand):
    help = (
        "Sincroniza alertas operacionais de forma agendada. "
        "Por omissão corre em dry-run; usar --apply para alterar a base de dados."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica expirações e sincronização de alertas.",
        )
        parser.add_argument(
            "--producer-id",
            dest="producer_id",
            help="Opcional: sincroniza apenas um produtor específico.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Opcional: limita o número de produtores processados.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        summary = run_operational_alerts_job(
            producer_id=(options.get("producer_id") or "").strip() or None,
            limit=options.get("limit"),
            apply=apply_changes,
        )

        mode = "APPLY" if apply_changes else "DRY-RUN"
        style = self.style.SUCCESS if apply_changes else self.style.WARNING
        self.stdout.write(style(f"[{mode}] Job de alertas operacionais"))
        self.stdout.write(
            " | ".join(
                [
                    f"produtores={summary['producers_seen']}",
                    f"sincronizados={summary['producers_synced']}",
                    f"listings_expirados={summary['listings_expired']}",
                    f"adiamentos_expirados={summary['ignored_expired']}",
                    f"alertas_expirados={summary['alerts_expired']}",
                    f"criados={summary['created']}",
                    f"atualizados={summary['updated']}",
                    f"resolvidos={summary['resolved']}",
                    f"limpos={summary['cleared']}",
                    f"erros={summary['errors']}",
                ]
            )
        )
