from django import forms

from apps.catalog.models import ProductCategory
from apps.inventory.models import StockMovementType


class AddProducerProductForm(forms.Form):
    """Associar um produto já existente do catálogo ao produtor."""

    product_id = forms.UUIDField(widget=forms.HiddenInput())

    producer_description = forms.CharField(
        label="Descrição do produtor (opcional)",
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 3,
            "placeholder": "Descrição específica deste produto para o seu negócio",
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

    safety_stock = forms.DecimalField(
        label="Stock de segurança",
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

    surplus_threshold = forms.IntegerField(
        label="Limiar excedente (opcional)",
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "1",
            "inputmode": "numeric",
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

    producer_description = forms.CharField(
        label="Descrição do produtor (opcional)",
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 3,
            "placeholder": "Descrição específica deste produto para o seu negócio",
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

    safety_stock = forms.DecimalField(
        label="Stock de segurança",
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

    surplus_threshold = forms.IntegerField(
        label="Limiar excedente (opcional)",
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "1",
            "inputmode": "numeric",
            "placeholder": "0",
        }),
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


class UpdateStockForm(forms.Form):
    """Atualizar a quantidade em stock, stock de segurança e limiar excedente."""

    MOVEMENT_CHOICES = [
        (StockMovementType.MANUAL_ADJUSTMENT, "Ajuste manual"),
        (StockMovementType.CORRECTION, "Correção de inventário"),
        (StockMovementType.IMPORT, "Importação / entrada"),
    ]

    new_quantity = forms.IntegerField(
        label="Nova quantidade em stock",
        min_value=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "1",
            "inputmode": "numeric",
            "placeholder": "0",
        }),
    )

    safety_stock = forms.IntegerField(
        label="Stock de segurança",
        min_value=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "1",
            "inputmode": "numeric",
            "placeholder": "0",
        }),
    )

    surplus_threshold = forms.IntegerField(
        label="Limiar excedente (opcional)",
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "1",
            "inputmode": "numeric",
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


class ProductionForecastForm(forms.Form):
    forecast_id = forms.UUIDField(required=False, widget=forms.HiddenInput())

    forecast_quantity = forms.DecimalField(
        label="Quantidade prevista",
        min_value=0,
        max_digits=14,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.001",
            "placeholder": "0",
        }),
    )

    period_start = forms.DateTimeField(
        label="Início do período (opcional)",
        required=False,
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={
            "class": "form-control",
            "type": "datetime-local",
        }),
    )

    period_end = forms.DateTimeField(
        label="Fim do período (opcional)",
        required=False,
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={
            "class": "form-control",
            "type": "datetime-local",
        }),
    )

    is_marketplace_enabled = forms.BooleanField(
        label="Ativar esta previsão para pré-venda no marketplace",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        period_start = cleaned_data.get("period_start")
        period_end = cleaned_data.get("period_end")
        forecast_quantity = cleaned_data.get("forecast_quantity")

        if forecast_quantity is not None and forecast_quantity <= 0:
            self.add_error("forecast_quantity", "A quantidade prevista deve ser superior a zero.")

        if period_start and period_end and period_end < period_start:
            self.add_error("period_end", "O período final não pode ser anterior ao período inicial.")

        return cleaned_data


