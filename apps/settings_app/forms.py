from django import forms
import re

from apps.accounts.models import User, UserRole
from apps.inventory.models import ProducerProfile, ProducerUserType
from apps.settings_app.models import UserPreference


class AccountProfileForm(forms.Form):
    first_name = forms.CharField(
        label="Primeiro nome",
        max_length=150,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ex: João",
        }),
    )
    last_name = forms.CharField(
        label="Último nome",
        max_length=150,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ex: Silva",
        }),
    )
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={
            "class": "form-control",
            "placeholder": "utilizador@exemplo.pt",
            "readonly": "readonly",
        }),
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

    def clean_first_name(self):
        value = " ".join((self.cleaned_data.get("first_name") or "").split()).strip()
        if not value:
            raise forms.ValidationError("Indica o primeiro nome.")
        return value

    def clean_last_name(self):
        value = " ".join((self.cleaned_data.get("last_name") or "").split()).strip()
        if not value:
            raise forms.ValidationError("Indica o último nome.")
        return value

    def clean_email(self):
        value = (self.cleaned_data.get("email") or "").strip().lower()
        if not value:
            raise forms.ValidationError("Indica um email válido.")

        qs = User.objects.filter(email=value)
        if self.user:
            qs = qs.exclude(id=self.user.id)
        if qs.exists():
            raise forms.ValidationError("Este email já está a ser utilizado por outra conta.")
        return value


class ProducerProfileSettingsForm(forms.ModelForm):
    user_type = forms.ChoiceField(
        label="Tipo de utilizador",
        required=False,
        choices=[
            ("", "Selecionar tipo"),
            (ProducerUserType.AGRICULTOR, "Agricultor / Produtor"),
            (ProducerUserType.DISTRIBUIDOR, "Distribuidor"),
            (ProducerUserType.VENDEDOR, "Vendedor / Retalhista"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = ProducerProfile
        fields = [
            "display_name",
            "company_name",
            "phone",
            "nif",
            "address_line",
            "postal_code",
            "city",
            "district",
            "latitude",
            "longitude",
            "user_type",
            "is_active_marketplace",
        ]
        widgets = {
            "display_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Nome público"}),
            "company_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Empresa"}),
            "phone": forms.TextInput(attrs={"class": "form-control", "placeholder": "912345678"}),
            "nif": forms.TextInput(attrs={"class": "form-control", "placeholder": "123456789"}),
            "address_line": forms.TextInput(attrs={"class": "form-control", "placeholder": "Morada"}),
            "postal_code": forms.TextInput(attrs={"class": "form-control", "placeholder": "3510-000"}),
            "city": forms.TextInput(attrs={"class": "form-control", "placeholder": "Cidade"}),
            "district": forms.TextInput(attrs={"class": "form-control", "placeholder": "Distrito"}),
            "latitude": forms.NumberInput(attrs={"class": "form-control", "step": "0.000001", "placeholder": "Ex: 41.157944"}),
            "longitude": forms.NumberInput(attrs={"class": "form-control", "step": "0.000001", "placeholder": "Ex: -8.629105"}),
            "is_active_marketplace": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "display_name": "Nome de exibição",
            "company_name": "Empresa",
            "phone": "Telemóvel",
            "nif": "NIF",
            "address_line": "Morada",
            "postal_code": "Código-postal",
            "city": "Cidade",
            "district": "Distrito",
            "latitude": "Latitude",
            "longitude": "Longitude",
            "is_active_marketplace": "Ativo no marketplace",
        }

    def clean_display_name(self):
        value = " ".join((self.cleaned_data.get("display_name") or "").split()).strip()
        if not value:
            raise forms.ValidationError("Indica o nome de exibição.")
        return value

    def clean_user_type(self):
        value = (self.cleaned_data.get("user_type") or "").strip()
        return value or None

    def clean_nif(self):
        value = (self.cleaned_data.get("nif") or "").strip()
        if not value:
            return None
        if not re.fullmatch(r"\d{9}", value):
            raise forms.ValidationError("O NIF deve ter exatamente 9 dígitos.")
        return value

    def clean_phone(self):
        value = (self.cleaned_data.get("phone") or "").strip().replace(" ", "")
        if not value:
            return None
        if not re.fullmatch(r"\d{9}", value):
            raise forms.ValidationError("O telemóvel deve ter exatamente 9 dígitos.")
        return value

    def clean_postal_code(self):
        value = (self.cleaned_data.get("postal_code") or "").strip().upper()
        if not value:
            return None
        if not re.fullmatch(r"\d{4}-\d{3}", value):
            raise forms.ValidationError("O código-postal deve estar no formato 1234-567.")
        return value

    def clean_latitude(self):
        value = self.cleaned_data.get("latitude")
        if value in (None, ""):
            return None
        if value < -90 or value > 90:
            raise forms.ValidationError("A latitude deve estar entre -90 e 90.")
        return value

    def clean_longitude(self):
        value = self.cleaned_data.get("longitude")
        if value in (None, ""):
            return None
        if value < -180 or value > 180:
            raise forms.ValidationError("A longitude deve estar entre -180 e 180.")
        return value

    def clean(self):
        cleaned_data = super().clean()
        optional_text_fields = [
            "company_name",
            "phone",
            "nif",
            "address_line",
            "postal_code",
            "city",
            "district",
        ]

        for field in optional_text_fields:
            value = cleaned_data.get(field)
            if isinstance(value, str):
                value = " ".join(value.split()).strip()
                cleaned_data[field] = value or None

        return cleaned_data


class UserPreferencesForm(forms.ModelForm):
    UNIT_CHOICES = [
        ("kg", "kg"),
        ("t", "t"),
    ]

    alerts_in_app = forms.BooleanField(
        label="Alertas na app",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    alerts_email = forms.BooleanField(
        label="Alertas por email",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    alerts_sms = forms.BooleanField(
        label="Alertas por SMS",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    preferred_unit = forms.ChoiceField(
        label="Unidade preferida",
        choices=UNIT_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    profile_photo = forms.ImageField(
        label="Foto de perfil",
        required=False,
        widget=forms.ClearableFileInput(attrs={
            "class": "form-control",
            "accept": "image/*",
        }),
    )

    class Meta:
        model = UserPreference
        fields = [
            "alerts_in_app",
            "alerts_email",
            "alerts_sms",
            "preferred_unit",
            "profile_photo",
        ]

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        is_admin = bool(self.user and self.user.role == UserRole.ADMIN)
        if is_admin:
            self.fields.pop("preferred_unit", None)

    def clean_preferred_unit(self):
        value = (self.cleaned_data.get("preferred_unit") or "").strip().lower()
        if value not in {"kg", "t"}:
            raise forms.ValidationError("Seleciona uma unidade válida.")
        return value


class ChangePasswordForm(forms.Form):
    current_password = forms.CharField(
        label="Palavra-passe atual",
        widget=forms.PasswordInput(attrs={
            "class": "form-control",
            "placeholder": "Palavra-passe atual",
            "id": "id_current_password",
        }),
    )
    new_password = forms.CharField(
        label="Nova palavra-passe",
        min_length=8,
        widget=forms.PasswordInput(attrs={
            "class": "form-control",
            "placeholder": "Mínimo 8 caracteres",
            "id": "id_new_password",
            "minlength": "8",
        }),
    )
    confirm_password = forms.CharField(
        label="Confirmar nova palavra-passe",
        widget=forms.PasswordInput(attrs={
            "class": "form-control",
            "placeholder": "Repete a nova palavra-passe",
            "id": "id_confirm_password_settings",
            "minlength": "8",
        }),
    )

    def clean(self):
        cleaned_data = super().clean()
        current_password = cleaned_data.get("current_password")
        new_password = cleaned_data.get("new_password")
        confirm_password = cleaned_data.get("confirm_password")

        if new_password and confirm_password and new_password != confirm_password:
            self.add_error("confirm_password", "A confirmação não coincide com a nova palavra-passe.")

        if current_password and new_password and current_password == new_password:
            self.add_error("new_password", "A nova palavra-passe deve ser diferente da atual.")

        return cleaned_data
