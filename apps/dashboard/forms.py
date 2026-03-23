from django import forms
from apps.accounts.models import User, UserRole


ROLE_CHOICES = [
    (UserRole.CLIENTE, "Cliente"),
    (UserRole.ADMIN, "Administrador"),
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
    email = forms.EmailField(label="Email")
    role = forms.ChoiceField(label="Tipo de utilizador", choices=ROLE_CHOICES)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()

        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Já existe um utilizador com este email.")

        return email