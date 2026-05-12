from django import forms
from django.db.models import Q

from apps.catalog.models import ProductCategory
from apps.catalog.services import normalize_text, normalize_unit


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
        value = normalize_text(self.cleaned_data.get("name"))
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

    def __init__(self, *args, product=None, **kwargs):
        super().__init__(*args, **kwargs)
        categories = ProductCategory.objects.filter(is_active=True)
        if product and product.category_id:
            categories = ProductCategory.objects.filter(
                Q(is_active=True) | Q(id=product.category_id)
            )
        self.fields["category"].queryset = categories.order_by("name")

    def clean_name(self):
        value = normalize_text(self.cleaned_data.get("name"))
        if not value:
            raise forms.ValidationError("Indica o nome do produto.")
        return value

    def clean_unit(self):
        value = normalize_unit(self.cleaned_data.get("unit"))
        if not value:
            raise forms.ValidationError("Indica a unidade do produto.")
        return value
