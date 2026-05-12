from django import forms

from apps.accounts.models import User


class AdminUserCreateForm(forms.Form):
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={
            "class": "form-control",
            "placeholder": "utilizador@exemplo.pt",
        }),
    )

    role = forms.ChoiceField(
        label="Role",
        choices=[
            ("CLIENTE", "Cliente"),
            ("ADMIN", "Administrador"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("Este email já está registado.")
        return email

