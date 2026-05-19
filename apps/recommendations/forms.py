from decimal import Decimal

from django import forms


class RecommendationRequestForm(forms.Form):
    product_id = forms.ChoiceField(
        label="Produto",
        choices=[],
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    requested_quantity = forms.DecimalField(
        label="Quantidade a comprar",
        min_value=Decimal("0.000"),
        max_digits=14,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.001",
            "min": "0",
            "placeholder": "0.000",
        }),
    )

    def __init__(self, *args, **kwargs):
        products = kwargs.pop("products", [])
        super().__init__(*args, **kwargs)

        choices = [("", "Selecionar produto")]
        for product in products:
            label = product.name
            if getattr(product, "is_critical_stock", False):
                label = f"{label} - Stock crítico"
            elif getattr(product, "is_surplus_stock", False):
                label = f"{label} - Excedente"
            choices.append((str(product.id), label))
        self.fields["product_id"].choices = choices

    def clean_product_id(self):
        value = (self.cleaned_data.get("product_id") or "").strip()
        if not value:
            raise forms.ValidationError("Seleciona um produto.")
        return value

    def clean_requested_quantity(self):
        value = self.cleaned_data["requested_quantity"]
        return value
