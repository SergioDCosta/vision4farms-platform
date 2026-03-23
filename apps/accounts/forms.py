from django import forms
from apps.inventory.models import ProducerUserType


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

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "As palavras-passe não coincidem.")

        return cleaned_data