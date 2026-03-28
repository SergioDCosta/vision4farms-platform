from django.db import models


class UserPreference(models.Model):
    id = models.UUIDField(primary_key=True)
    user = models.OneToOneField(
        "accounts.User",
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="preferences",
    )
    alerts_in_app = models.BooleanField(default=True)
    alerts_email = models.BooleanField(default=True)
    alerts_sms = models.BooleanField(default=False)
    preferred_unit = models.CharField(max_length=20, default="kg")
    profile_photo = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "user_preferences"

    def __str__(self):
        return f"Preferências de {self.user.email}"