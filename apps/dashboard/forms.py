from django import forms
from apps.accounts.models import User, UserRole, AccountStatus
from apps.inventory.models import ProducerProfile


def producer_user_type_choices():
    try:
        return ProducerProfile._meta.get_field("user_type").choices
    except Exception:
        return []


ROLE_CHOICES = [
    (UserRole.CLIENTE, "Cliente"),
    (UserRole.ADMIN, "Administrador"),
]

ACCOUNT_STATUS_CHOICES = [
    (AccountStatus.ACTIVE, "Ativa"),
    (AccountStatus.PENDING_EMAIL_CONFIRMATION, "Pendente de confirmação"),
    (AccountStatus.SUSPENDED, "Suspensa"),
]


class BootstrapFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs["class"] = "form-control"
                field.widget.attrs.setdefault("rows", 3)
            else:
                field.widget.attrs["class"] = "form-control"


class AdminUserCreateForm(BootstrapFormMixin, forms.Form):
    first_name = forms.CharField(label="Primeiro nome", max_length=150)
    last_name = forms.CharField(label="Último nome", max_length=150)
    email = forms.EmailField(label="Email")
    role = forms.ChoiceField(label="Tipo de utilizador", choices=ROLE_CHOICES)
    password = forms.CharField(label="Palavra-passe", widget=forms.PasswordInput, min_length=8)
    password_confirm = forms.CharField(label="Confirmar palavra-passe", widget=forms.PasswordInput, min_length=8)

    company_name = forms.CharField(label="Empresa", required=False, max_length=255)
    user_type = forms.ChoiceField(label="Tipo de produtor", required=False, choices=producer_user_type_choices())

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()

        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Já existe um utilizador com este email.")

        return email

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        password = cleaned.get("password")
        password_confirm = cleaned.get("password_confirm")
        user_type = cleaned.get("user_type")

        if password and password_confirm and password != password_confirm:
            self.add_error("password_confirm", "As palavras-passe não coincidem.")

        if role == UserRole.CLIENTE and not user_type:
            self.add_error("user_type", "Selecione o tipo de produtor para um utilizador cliente.")

        return cleaned


class AdminUserUpdateForm(BootstrapFormMixin, forms.Form):
    first_name = forms.CharField(label="Primeiro nome", max_length=150)
    last_name = forms.CharField(label="Último nome", max_length=150)
    email = forms.EmailField(label="Email")
    role = forms.ChoiceField(label="Tipo de utilizador", choices=ROLE_CHOICES)

    account_status = forms.ChoiceField(label="Estado da conta", choices=ACCOUNT_STATUS_CHOICES)
    is_active = forms.BooleanField(label="Utilizador ativo", required=False)

    company_name = forms.CharField(label="Empresa", required=False, max_length=255)
    user_type = forms.ChoiceField(label="Tipo de produtor", required=False, choices=producer_user_type_choices())

    new_password = forms.CharField(
        label="Nova palavra-passe",
        widget=forms.PasswordInput,
        required=False,
        min_length=8
    )
    new_password_confirm = forms.CharField(
        label="Confirmar nova palavra-passe",
        widget=forms.PasswordInput,
        required=False,
        min_length=8
    )

    def __init__(self, *args, user_instance=None, producer_profile=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_instance = user_instance
        self.producer_profile = producer_profile

        if user_instance:
            self.fields["first_name"].initial = user_instance.first_name
            self.fields["last_name"].initial = user_instance.last_name
            self.fields["email"].initial = user_instance.email
            self.fields["role"].initial = user_instance.role
            self.fields["account_status"].initial = user_instance.account_status
            self.fields["is_active"].initial = user_instance.is_active

        if producer_profile:
            self.fields["company_name"].initial = producer_profile.company_name
            self.fields["user_type"].initial = producer_profile.user_type

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()

        qs = User.objects.filter(email__iexact=email)
        if self.user_instance:
            qs = qs.exclude(id=self.user_instance.id)

        if qs.exists():
            raise forms.ValidationError("Já existe um utilizador com este email.")

        return email

    def clean(self):
        cleaned = super().clean()

        role = cleaned.get("role")
        account_status = cleaned.get("account_status")
        is_active = cleaned.get("is_active")
        user_type = cleaned.get("user_type")
        new_password = cleaned.get("new_password")
        new_password_confirm = cleaned.get("new_password_confirm")

        if role == UserRole.CLIENTE and not user_type:
            self.add_error("user_type", "Selecione o tipo de produtor para um utilizador cliente.")

        if new_password or new_password_confirm:
            if new_password != new_password_confirm:
                self.add_error("new_password_confirm", "As palavras-passe não coincidem.")

        if account_status == AccountStatus.ACTIVE and not is_active:
            self.add_error("is_active", "Uma conta ativa deve ter o utilizador ativo.")

        if account_status in [AccountStatus.SUSPENDED, AccountStatus.PENDING_EMAIL_CONFIRMATION] and is_active:
            self.add_error("is_active", "Uma conta suspensa ou pendente não deve estar ativa.")

        return cleaned