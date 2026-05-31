from django import forms

from apps.accounts.models import User
from apps.inventory.models import ProducerUserType


class AdminUserCreateForm(forms.Form):
    first_name = forms.CharField(
        label="Primeiro nome",
        max_length=150,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ex: Ana",
        }),
    )

    last_name = forms.CharField(
        label="Apelido",
        max_length=150,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ex: Silva",
        }),
    )

    email = forms.EmailField(
        label="Email profissional",
        widget=forms.EmailInput(attrs={
            "class": "form-control",
            "placeholder": "utilizador@exemplo.pt",
        }),
    )

    role = forms.ChoiceField(
        label="Tipo de acesso",
        choices=[
            ("CLIENTE", "Produtor"),
            ("ADMIN", "Administrador"),
        ],
        widget=forms.Select(attrs={
            "class": "form-select",
            "data-admin-access-select": "",
        }),
    )

    company = forms.CharField(
        label="Empresa ou exploração agrícola",
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ex: Quinta Vale Verde",
        }),
    )

    user_type = forms.ChoiceField(
        label="Tipo de entidade",
        required=False,
        choices=[
            ("", "Selecionar tipo de entidade"),
            (ProducerUserType.AGRICULTOR, "Agricultor / Produtor"),
            (ProducerUserType.DISTRIBUIDOR, "Distribuidor"),
            (ProducerUserType.VENDEDOR, "Vendedor / Retalhista"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    personal_message = forms.CharField(
        label="Mensagem personalizada (opcional)",
        required=False,
        max_length=600,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 4,
            "placeholder": "Contexto adicional para o convite",
        }),
    )

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("Este email já está registado.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get("role")

        if role == "CLIENTE":
            if not (cleaned_data.get("company") or "").strip():
                self.add_error("company", "Indica a empresa ou exploração agrícola.")
            if not cleaned_data.get("user_type"):
                self.add_error("user_type", "Seleciona o tipo de entidade.")
        elif role == "ADMIN":
            cleaned_data["company"] = ""
            cleaned_data["user_type"] = ""

        for field_name in ("first_name", "last_name", "company", "personal_message"):
            if field_name in cleaned_data:
                cleaned_data[field_name] = " ".join(
                    (cleaned_data.get(field_name) or "").split()
                ).strip()

        return cleaned_data


class AdminManualConfirmationForm(forms.Form):
    justification = forms.CharField(
        label="Justificação administrativa",
        min_length=10,
        max_length=500,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 4,
            "placeholder": "Indica o motivo para validar o email sem confirmação pelo utilizador.",
        }),
    )

    def clean_justification(self):
        return " ".join((self.cleaned_data.get("justification") or "").split()).strip()
