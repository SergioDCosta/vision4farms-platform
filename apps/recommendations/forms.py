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
        min_value=Decimal("0.001"),
        max_digits=14,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.001",
            "min": "0.001",
            "placeholder": "0.000",
        }),
    )

    def __init__(self, *args, **kwargs):
        products = kwargs.pop("products", [])
        super().__init__(*args, **kwargs)

        choices = [("", "Selecionar produto")]
        for product in products:
            choices.append((str(product.id), product.name))
        self.fields["product_id"].choices = choices

    def clean_product_id(self):
        value = (self.cleaned_data.get("product_id") or "").strip()
        if not value:
            raise forms.ValidationError("Seleciona um produto.")
        return value

    def clean_requested_quantity(self):
        value = self.cleaned_data["requested_quantity"]
        if value <= 0:
            raise forms.ValidationError("A quantidade a comprar deve ser superior a zero.")
        return value
