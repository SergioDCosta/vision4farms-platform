from django import forms
from django.contrib.auth.password_validation import validate_password
import re

from apps.inventory.models import ProducerProfile, ProducerUserType
from apps.settings_app.models import UserPreference


class IdentityProfileForm(forms.Form):
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
            "readonly": "readonly",
        }),
    )
    company_name = forms.CharField(
        label="Empresa",
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Nome da empresa ou exploração",
        }),
    )
    phone = forms.CharField(
        label="Telemóvel",
        required=False,
        max_length=20,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "912345678",
        }),
    )
    nif = forms.CharField(
        label="NIF",
        required=False,
        max_length=20,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "123456789",
        }),
    )
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

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        self.producer_profile = kwargs.pop("producer_profile", None)
        initial = dict(kwargs.pop("initial", {}) or {})

        if self.user:
            initial.setdefault("first_name", self.user.first_name)
            initial.setdefault("last_name", self.user.last_name)
            initial.setdefault("email", self.user.email)

        if self.producer_profile:
            initial.setdefault("company_name", self.producer_profile.company_name)
            initial.setdefault("phone", self.producer_profile.phone)
            initial.setdefault("nif", self.producer_profile.nif)
            initial.setdefault("user_type", self.producer_profile.user_type)

        kwargs["initial"] = initial
        super().__init__(*args, **kwargs)

        if not self.producer_profile:
            for field in ("company_name", "phone", "nif", "user_type"):
                self.fields.pop(field, None)

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
        if self.user:
            return self.user.email
        value = (self.cleaned_data.get("email") or "").strip().lower()
        if not value:
            raise forms.ValidationError("Indica um email válido.")
        return value

    def clean_phone(self):
        value = (self.cleaned_data.get("phone") or "").strip().replace(" ", "")
        if not value:
            return None
        if not re.fullmatch(r"\d{9}", value):
            raise forms.ValidationError("O telemóvel deve ter exatamente 9 dígitos.")
        return value

    def clean_nif(self):
        value = (self.cleaned_data.get("nif") or "").strip()
        if not value:
            return None
        if not re.fullmatch(r"\d{9}", value):
            raise forms.ValidationError("O NIF deve ter exatamente 9 dígitos.")
        return value

    def clean_user_type(self):
        value = (self.cleaned_data.get("user_type") or "").strip()
        return value or None

    def clean_company_name(self):
        value = " ".join((self.cleaned_data.get("company_name") or "").split()).strip()
        return value or None


class ProducerLocationForm(forms.ModelForm):
    class Meta:
        model = ProducerProfile
        fields = [
            "address_line",
            "postal_code",
            "city",
            "district",
            "latitude",
            "longitude",
        ]
        widgets = {
            "address_line": forms.TextInput(attrs={"class": "form-control", "placeholder": "Morada"}),
            "postal_code": forms.TextInput(attrs={"class": "form-control", "placeholder": "3510-000"}),
            "city": forms.TextInput(attrs={"class": "form-control", "placeholder": "Cidade"}),
            "district": forms.TextInput(attrs={"class": "form-control", "placeholder": "Distrito"}),
            "latitude": forms.NumberInput(attrs={"class": "form-control", "step": "0.000001", "placeholder": "Ex: 41.157944"}),
            "longitude": forms.NumberInput(attrs={"class": "form-control", "step": "0.000001", "placeholder": "Ex: -8.629105"}),
        }
        labels = {
            "address_line": "Morada",
            "postal_code": "Código-postal",
            "city": "Cidade",
            "district": "Distrito",
            "latitude": "Latitude",
            "longitude": "Longitude",
        }

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
        for field in ("address_line", "postal_code", "city", "district"):
            value = cleaned_data.get(field)
            if isinstance(value, str):
                value = " ".join(value.split()).strip()
                cleaned_data[field] = value or None
        return cleaned_data


class UserPreferencesForm(forms.ModelForm):
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
            "profile_photo",
        ]

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)


class ProfilePhotoForm(forms.Form):
    profile_photo = forms.ImageField(
        label="Foto de perfil",
        required=True,
        widget=forms.ClearableFileInput(
            attrs={
                "class": "form-control",
                "accept": "image/*",
            }
        ),
    )


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

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned_data = super().clean()
        current_password = cleaned_data.get("current_password")
        new_password = cleaned_data.get("new_password")
        confirm_password = cleaned_data.get("confirm_password")

        if new_password and confirm_password and new_password != confirm_password:
            self.add_error("confirm_password", "A confirmação não coincide com a nova palavra-passe.")

        if current_password and new_password and current_password == new_password:
            self.add_error("new_password", "A nova palavra-passe deve ser diferente da atual.")

        if new_password and not self.errors.get("new_password"):
            try:
                validate_password(new_password, user=self.user)
            except forms.ValidationError as exc:
                self.add_error("new_password", exc)

        return cleaned_data
