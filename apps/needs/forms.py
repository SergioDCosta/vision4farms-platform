from decimal import Decimal

from django import forms

from apps.inventory.models import ProductionForecast
from apps.marketplace.models import DeliveryMode
from apps.marketplace.services import (
    LISTING_SOURCE_FORECAST,
    LISTING_SOURCE_STOCK,
    get_forecast_available_quantity,
    get_marketplace_eligible_forecasts,
    get_max_publishable_quantity,
    get_stock_for_product,
)


class NeedResponsePublishForm(forms.Form):
    LISTING_SOURCE_CHOICES = (
        (LISTING_SOURCE_STOCK, "Stock atual (disponível agora)"),
        (LISTING_SOURCE_FORECAST, "Produção futura (pré-venda)"),
    )

    listing_source = forms.ChoiceField(
        label="Origem da oferta",
        choices=LISTING_SOURCE_CHOICES,
        initial=LISTING_SOURCE_STOCK,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    forecast = forms.ModelChoiceField(
        label="Previsão de produção",
        queryset=ProductionForecast.objects.none(),
        required=False,
        empty_label="Selecionar previsão...",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    quantity = forms.DecimalField(
        label="Quantidade a oferecer",
        min_value=Decimal("0.001"),
        max_digits=14,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "any",
            "placeholder": "Ex: 50",
        }),
    )
    unit_price = forms.DecimalField(
        label="Preço por kg",
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "any",
            "placeholder": "Ex: 2.50",
        }),
    )
    delivery_mode = forms.ChoiceField(
        label="Modo de entrega",
        choices=DeliveryMode.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    delivery_radius_km = forms.DecimalField(
        label="Raio de entrega (km)",
        required=False,
        min_value=Decimal("0.01"),
        max_digits=8,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "any",
            "placeholder": "Ex: 20",
        }),
    )
    delivery_fee = forms.DecimalField(
        label="Taxa de entrega (€)",
        required=False,
        min_value=Decimal("0.00"),
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "any",
            "placeholder": "Ex: 5.00",
        }),
    )
    show_location_on_map = forms.BooleanField(
        label="Mostrar localização aproximada no mapa",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    notes = forms.CharField(
        label="Observações da resposta",
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 4,
            "placeholder": "Condições, qualidade, colheita, disponibilidade ou detalhes da entrega...",
        }),
    )

    def __init__(self, *args, producer=None, need=None, **kwargs):
        self.producer = producer
        self.need = need
        super().__init__(*args, **kwargs)

        product = getattr(need, "product", None)
        if producer and product:
            forecasts = get_marketplace_eligible_forecasts(producer, product=product)
            self.fields["forecast"].queryset = (
                ProductionForecast.objects
                .filter(id__in=[forecast.id for forecast in forecasts])
                .select_related("product")
                .order_by("-period_start", "-created_at")
            )

    def clean(self):
        cleaned_data = super().clean()
        producer = self.producer
        need = self.need
        product = getattr(need, "product", None)
        listing_source = cleaned_data.get("listing_source") or LISTING_SOURCE_STOCK
        forecast = cleaned_data.get("forecast")
        quantity = cleaned_data.get("quantity")
        delivery_mode = cleaned_data.get("delivery_mode")
        delivery_radius_km = cleaned_data.get("delivery_radius_km")

        if not producer or not need or not product:
            self.add_error(None, "Não foi possível preparar a resposta a esta necessidade.")
            return cleaned_data

        if quantity:
            if listing_source == LISTING_SOURCE_STOCK:
                cleaned_data["forecast"] = None
                stock = get_stock_for_product(producer, product)
                max_publishable = get_max_publishable_quantity(stock)
                if max_publishable <= 0:
                    self.add_error("listing_source", "Este produto não tem excedente atual disponível.")
                elif quantity > max_publishable:
                    self.add_error(
                        "quantity",
                        f"A quantidade excede o máximo disponível ({max_publishable} {product.unit}).",
                    )
            elif listing_source == LISTING_SOURCE_FORECAST:
                if not forecast:
                    self.add_error("forecast", "Seleciona uma previsão de produção.")
                else:
                    if forecast.producer_id != producer.id:
                        self.add_error("forecast", "Previsão inválida para este produtor.")
                    if forecast.product_id != product.id:
                        self.add_error("forecast", "A previsão selecionada não corresponde ao produto.")
                    if not forecast.is_marketplace_enabled:
                        self.add_error("forecast", "Esta previsão não está ativa para pré-venda.")
                    max_publishable = get_forecast_available_quantity(forecast)
                    if max_publishable <= 0:
                        self.add_error("forecast", "Esta previsão não tem quantidade disponível.")
                    elif quantity > max_publishable:
                        self.add_error(
                            "quantity",
                            f"A quantidade excede o máximo disponível ({max_publishable} {product.unit}).",
                        )
            else:
                self.add_error("listing_source", "Origem da oferta inválida.")

        if delivery_mode in {DeliveryMode.DELIVERY, DeliveryMode.BOTH}:
            if not delivery_radius_km:
                self.add_error("delivery_radius_km", "Indica o raio de entrega.")
        else:
            cleaned_data["delivery_radius_km"] = None
            cleaned_data["delivery_fee"] = None

        return cleaned_data
