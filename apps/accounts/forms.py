from types import SimpleNamespace

from django import forms
from django.contrib.auth.password_validation import validate_password

from apps.accounts.models import UserRole
from apps.inventory.models import ProducerUserType


def _password_validation_user(*, first_name="", last_name="", email="", user=None):
    return SimpleNamespace(
        first_name=first_name if first_name is not None else getattr(user, "first_name", ""),
        last_name=last_name if last_name is not None else getattr(user, "last_name", ""),
        email=email if email is not None else getattr(user, "email", ""),
        username=email if email is not None else getattr(user, "email", ""),
    )


def _add_password_validation_error(form, password, user):
    if not password:
        return

    try:
        validate_password(password, user=user)
    except forms.ValidationError as exc:
        form.add_error("password", exc)


class LoginForm(forms.Form):
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={
            "placeholder": "seu.email@exemplo.pt",
            "class": "form-input with-left-icon",
            "id": "id_email",
        })
    )
    password = forms.CharField(
        label="Palavra-passe",
        widget=forms.PasswordInput(attrs={
            "placeholder": "••••••••",
            "class": "form-input with-left-icon with-right-button",
            "id": "id_password",
        })
    )
    remember_me = forms.BooleanField(
        label="Lembrar-me",
        required=False
    )


class RegisterForm(forms.Form):
    first_name = forms.CharField(
        label="Primeiro Nome",
        max_length=150,
        widget=forms.TextInput(attrs={
            "placeholder": "Ex: João",
            "class": "form-input with-left-icon",
            "id": "id_first_name",
        })
    )
    last_name = forms.CharField(
        label="Último Nome",
        max_length=150,
        widget=forms.TextInput(attrs={
            "placeholder": "Ex: Silva",
            "class": "form-input with-left-icon",
            "id": "id_last_name",
        })
    )
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={
            "placeholder": "joao.silva@exemplo.com",
            "class": "form-input with-left-icon",
            "id": "id_email",
        })
    )
    company = forms.CharField(
        label="Empresa",
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={
            "placeholder": "Nome da sua exploração ou empresa",
            "class": "form-input with-left-icon",
            "id": "id_company",
        })
    )
    user_type = forms.ChoiceField(
        label="Tipo de Utilizador",
        choices=[
            ("", "Selecione o seu perfil"),
            (ProducerUserType.AGRICULTOR, "Agricultor / Produtor"),
            (ProducerUserType.DISTRIBUIDOR, "Distribuidor"),
            (ProducerUserType.VENDEDOR, "Vendedor / Retalhista"),
        ],
        widget=forms.Select(attrs={
            "class": "form-input with-left-icon",
            "id": "id_user_type",
        })
    )
    password = forms.CharField(
        label="Palavra-passe",
        min_length=8,
        widget=forms.PasswordInput(attrs={
            "placeholder": "Mínimo 8 caracteres",
            "class": "form-input with-left-icon with-right-button",
            "id": "id_password",
            "minlength": "8",
        })
    )
    confirm_password = forms.CharField(
        label="Repetir Palavra-passe",
        widget=forms.PasswordInput(attrs={
            "placeholder": "Repita a palavra-passe",
            "class": "form-input with-left-icon with-right-button",
            "id": "id_confirm_password",
            "minlength": "8",
        })
    )

    def clean_email(self):
        from apps.accounts.models import User

        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("Este email já está registado.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "As palavras-passe não coincidem.")
        else:
            validation_user = _password_validation_user(
                first_name=cleaned_data.get("first_name"),
                last_name=cleaned_data.get("last_name"),
                email=cleaned_data.get("email"),
            )
            _add_password_validation_error(self, password, validation_user)

        return cleaned_data


class PasswordResetRequestForm(forms.Form):
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={
            "placeholder": "seu.email@exemplo.pt",
            "class": "form-input with-left-icon",
            "id": "id_email",
        })
    )


class PasswordResetConfirmForm(forms.Form):
    password = forms.CharField(
        label="Nova Palavra-passe",
        min_length=8,
        widget=forms.PasswordInput(attrs={
            "placeholder": "Mínimo 8 caracteres",
            "class": "form-input with-left-icon with-right-button",
            "id": "id_password",
            "minlength": "8",
        })
    )
    confirm_password = forms.CharField(
        label="Repetir Palavra-passe",
        widget=forms.PasswordInput(attrs={
            "placeholder": "Repita a nova palavra-passe",
            "class": "form-input with-left-icon with-right-button",
            "id": "id_confirm_password",
            "minlength": "8",
        })
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "As palavras-passe não coincidem.")
        else:
            _add_password_validation_error(self, password, self.user)

        return cleaned_data


class AdminInviteCompleteForm(forms.Form):
    first_name = forms.CharField(
        label="Primeiro Nome",
        max_length=150,
        widget=forms.TextInput(attrs={
            "placeholder": "Ex: João",
            "class": "form-input with-left-icon",
            "id": "id_first_name",
        })
    )
    last_name = forms.CharField(
        label="Último Nome",
        max_length=150,
        widget=forms.TextInput(attrs={
            "placeholder": "Ex: Silva",
            "class": "form-input with-left-icon",
            "id": "id_last_name",
        })
    )
    company = forms.CharField(
        label="Empresa",
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={
            "placeholder": "Nome da sua exploração ou empresa",
            "class": "form-input with-left-icon",
            "id": "id_company",
        })
    )
    user_type = forms.ChoiceField(
        label="Tipo de Utilizador",
        required=False,
        choices=[
            ("", "Selecione o seu perfil"),
            (ProducerUserType.AGRICULTOR, "Agricultor / Produtor"),
            (ProducerUserType.DISTRIBUIDOR, "Distribuidor"),
            (ProducerUserType.VENDEDOR, "Vendedor / Retalhista"),
        ],
        widget=forms.Select(attrs={
            "class": "form-input with-left-icon",
            "id": "id_user_type",
        })
    )
    password = forms.CharField(
        label="Palavra-passe",
        min_length=8,
        widget=forms.PasswordInput(attrs={
            "placeholder": "Mínimo 8 caracteres",
            "class": "form-input with-left-icon with-right-button",
            "id": "id_password",
            "minlength": "8",
        })
    )
    confirm_password = forms.CharField(
        label="Repetir Palavra-passe",
        widget=forms.PasswordInput(attrs={
            "placeholder": "Repita a palavra-passe",
            "class": "form-input with-left-icon with-right-button",
            "id": "id_confirm_password",
            "minlength": "8",
        })
    )

    def __init__(self, *args, user_role=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_role = user_role
        self.user = user
        if user_role == UserRole.ADMIN:
            self.fields.pop("user_type", None)

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "As palavras-passe não coincidem.")
        else:
            validation_user = _password_validation_user(
                first_name=cleaned_data.get("first_name"),
                last_name=cleaned_data.get("last_name"),
                email=getattr(self.user, "email", ""),
                user=self.user,
            )
            _add_password_validation_error(self, password, validation_user)

        return cleaned_data
