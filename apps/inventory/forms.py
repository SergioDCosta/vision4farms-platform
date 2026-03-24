from django import forms

from apps.catalog.models import ProductCategory
from apps.inventory.models import StockMovementType


class AddProducerProductForm(forms.Form):
    """Associar um produto já existente do catálogo ao produtor."""

    product_id = forms.UUIDField(widget=forms.HiddenInput())

    initial_quantity = forms.DecimalField(
        label="Stock inicial",
        min_value=0,
        max_digits=14,
        decimal_places=3,
        initial=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.001",
            "placeholder": "0",
        }),
    )

    minimum_threshold = forms.DecimalField(
        label="Stock mínimo de alerta",
        min_value=0,
        max_digits=14,
        decimal_places=3,
        initial=0,
        help_text="Recebe alerta quando o stock descer abaixo deste valor.",
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.001",
            "placeholder": "0",
        }),
    )


class CreateCustomProductForm(forms.Form):
    """Criar um novo produto no catálogo e associá-lo ao produtor."""

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
            "placeholder": "Ex: Soja",
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
        label="Descrição (opcional)",
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 3,
            "placeholder": "Descrição breve do produto",
        }),
    )

    initial_quantity = forms.DecimalField(
        label="Stock inicial",
        min_value=0,
        max_digits=14,
        decimal_places=3,
        initial=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.001",
            "placeholder": "0",
        }),
    )

    minimum_threshold = forms.DecimalField(
        label="Stock mínimo de alerta",
        min_value=0,
        max_digits=14,
        decimal_places=3,
        initial=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.001",
            "placeholder": "0",
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ProductCategory.objects.filter(
            is_active=True
        ).order_by("name")

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


class UpdateStockForm(forms.Form):
    """Atualizar a quantidade em stock e o limiar mínimo."""

    MOVEMENT_CHOICES = [
        (StockMovementType.MANUAL_ADJUSTMENT, "Ajuste manual"),
        (StockMovementType.CORRECTION, "Correção de inventário"),
        (StockMovementType.IMPORT, "Importação / entrada"),
    ]

    new_quantity = forms.DecimalField(
        label="Nova quantidade em stock",
        min_value=0,
        max_digits=14,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.001",
            "placeholder": "0",
        }),
    )

    minimum_threshold = forms.DecimalField(
        label="Stock mínimo de alerta",
        min_value=0,
        max_digits=14,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.001",
            "placeholder": "0",
        }),
    )

    movement_type = forms.ChoiceField(
        label="Motivo da atualização",
        choices=MOVEMENT_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    notes = forms.CharField(
        label="Notas (opcional)",
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 2,
            "placeholder": "Ex: contagem após colheita",
        }),
    )

    def clean_new_quantity(self):
        value = self.cleaned_data.get("new_quantity")
        if value is not None and value < 0:
            raise forms.ValidationError("A quantidade não pode ser negativa.")
        return value