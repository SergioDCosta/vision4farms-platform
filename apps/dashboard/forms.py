from django import forms

from apps.catalog.models import ProductCategory


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
        return (self.cleaned_data.get("email") or "").strip().lower()


class AdminCategoryForm(forms.Form):
    name = forms.CharField(
        label="Nome da categoria",
        max_length=255,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ex: Legumes",
        }),
    )

    def clean_name(self):
        value = " ".join((self.cleaned_data.get("name") or "").split()).strip()
        if not value:
            raise forms.ValidationError("Indica o nome da categoria.")
        return value


class AdminProductForm(forms.Form):
    category = forms.ModelChoiceField(
        label="Categoria",
        queryset=ProductCategory.objects.none(),
        empty_label="Selecionar categoria",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    name = forms.CharField(
        label="Nome do produto",
        max_length=255,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ex: Tomate",
        }),
    )

    unit = forms.CharField(
        label="Unidade",
        max_length=50,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ex: kg, un, caixa",
        }),
    )

    description = forms.CharField(
        label="Descrição genérica do catálogo (opcional)",
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 4,
            "placeholder": "Descrição genérica para o catálogo global",
        }),
    )

    is_active = forms.BooleanField(
        label="Produto ativo",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ProductCategory.objects.order_by("name")

    def clean_name(self):
        value = " ".join((self.cleaned_data.get("name") or "").split()).strip()
        if not value:
            raise forms.ValidationError("Indica o nome do produto.")
        return value

    def clean_unit(self):
        value = " ".join((self.cleaned_data.get("unit") or "").split()).strip()
        if not value:
            raise forms.ValidationError("Indica a unidade do produto.")
        return value
