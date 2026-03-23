import uuid
from django.db import models


class SyncType(models.TextChoices):
    DEFICITS = "DEFICITS", "Défices"
    FORECASTS = "FORECASTS", "Previsões"
    NEEDS = "NEEDS", "Necessidades"
    EVENTS = "EVENTS", "Eventos"


class SyncStatus(models.TextChoices):
    SUCCESS = "SUCCESS", "Sucesso"
    PARTIAL = "PARTIAL", "Parcial"
    FAILED = "FAILED", "Falhou"


class Vision4FarmsSyncLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_type = models.CharField(max_length=30, choices=SyncType.choices)
    status = models.CharField(max_length=20, choices=SyncStatus.choices)
    records_received = models.IntegerField(default=0)
    records_imported = models.IntegerField(default=0)
    records_skipped = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    payload_summary = models.JSONField(blank=True, null=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "vision4farms_sync_log"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.sync_type} - {self.status}"