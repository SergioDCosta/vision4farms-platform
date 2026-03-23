import uuid
from django.db import models


class UserRole(models.TextChoices):
    CLIENTE = "CLIENTE", "Cliente"
    ADMIN = "ADMIN", "Administrador"


class RegistrationSource(models.TextChoices):
    SELF_REGISTERED = "SELF_REGISTERED", "Registo Público"
    ADMIN_CREATED = "ADMIN_CREATED", "Criado por Admin"


class AccountStatus(models.TextChoices):
    PENDING_EMAIL_CONFIRMATION = "PENDING_EMAIL_CONFIRMATION", "Pendente Confirmação"
    ACTIVE = "ACTIVE", "Ativa"
    SUSPENDED = "SUSPENDED", "Suspensa"


class VerificationPurpose(models.TextChoices):
    SIGNUP_CONFIRMATION = "SIGNUP_CONFIRMATION", "Confirmação de Registo"
    ADMIN_INVITE = "ADMIN_INVITE", "Convite de Administrador"
    PASSWORD_RESET = "PASSWORD_RESET", "Recuperação de Password"


class User(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, max_length=255)
    password = models.CharField(max_length=128)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    role = models.CharField(max_length=20, choices=UserRole.choices)
    registration_source = models.CharField(
        max_length=30,
        choices=RegistrationSource.choices,
        default=RegistrationSource.SELF_REGISTERED,
    )
    account_status = models.CharField(
        max_length=40,
        choices=AccountStatus.choices,
        default=AccountStatus.PENDING_EMAIL_CONFIRMATION,
    )
    email_verified_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    last_login = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "users"
        ordering = ["-created_at"]

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.full_name or self.email


class AccountVerificationToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="verification_tokens",
    )
    token = models.CharField(max_length=255, unique=True)
    purpose = models.CharField(max_length=30, choices=VerificationPurpose.choices)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "account_verification_tokens"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.email} - {self.purpose}"