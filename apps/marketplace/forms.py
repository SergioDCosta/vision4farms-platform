from decimal import Decimal
from datetime import timedelta

from django import forms
from django.utils import timezone

from apps.inventory.models import ProductionForecast
from apps.marketplace.models import DeliveryMode, ListingStatus
from apps.marketplace.services import (
    LISTING_SOURCE_FORECAST,
    LISTING_SOURCE_STOCK,
    get_forecast_available_quantity,
    get_marketplace_eligible_forecasts,
    get_publishable_products,
    get_max_publishable_quantity,
    get_stock_for_product,
)


class MarketplacePublishForm(forms.Form):
    LISTING_SOURCE_STOCK = LISTING_SOURCE_STOCK
    LISTING_SOURCE_FORECAST = LISTING_SOURCE_FORECAST
    LISTING_SOURCE_CHOICES = (
        (LISTING_SOURCE_STOCK, "Stock atual (disponível agora)"),
        (LISTING_SOURCE_FORECAST, "Produção futura (pré-venda)"),
    )

    EXPIRY_MODE_NONE = "none"
    EXPIRY_MODE_DATE = "date"

    EXPIRY_MODE_CHOICES = (
        (EXPIRY_MODE_NONE, "Sem prazo"),
        (EXPIRY_MODE_DATE, "Definir data e hora"),
    )

    listing_source = forms.ChoiceField(
        label="Origem da oferta",
        choices=LISTING_SOURCE_CHOICES,
        initial=LISTING_SOURCE_STOCK,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    product = forms.ModelChoiceField(
        label="Produto",
        queryset=None,
        empty_label="Selecione um produto...",
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
        label="Quantidade",
        min_value=Decimal("0.001"),
        max_digits=14,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "any",
            "placeholder": "Ex: 500",
        }),
    )

    unit_price = forms.DecimalField(
        label="Preço por unidade",
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "any",
            "placeholder": "Ex: 0.45",
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
        label="Mostrar localização no mapa",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    photo = forms.ImageField(
        label="Foto do anúncio",
        required=False,
        widget=forms.ClearableFileInput(attrs={
            "class": "form-control",
            "accept": "image/*",
        }),
    )
    photo_crop = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )

    notes = forms.CharField(
        label="Observações",
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 4,
            "placeholder": "Informações adicionais sobre o anúncio...",
        }),
    )

    status = forms.ChoiceField(
        label="Estado da listing",
        choices=(
            (ListingStatus.ACTIVE, "Ativo"),
            (ListingStatus.CANCELLED, "Desativado"),
            (ListingStatus.EXPIRED, "Expirado"),
        ),
        initial=ListingStatus.ACTIVE,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    expiration_mode = forms.ChoiceField(
        label="Modo de expiração",
        required=False,
        choices=EXPIRY_MODE_CHOICES,
        initial=EXPIRY_MODE_NONE,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    expires_at = forms.DateTimeField(
        label="Data e hora de expiração",
        required=False,
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(
            attrs={
                "class": "form-control",
                "type": "datetime-local",
            },
            format="%Y-%m-%dT%H:%M",
        ),
    )

    def __init__(self, *args, **kwargs):
        self.producer = kwargs.pop("producer", None)
        self.lock_listing_source = kwargs.pop("lock_listing_source", False)
        self.lock_product = kwargs.pop("lock_product", False)
        super().__init__(*args, **kwargs)

        if self.producer:
            self.fields["product"].queryset = get_publishable_products(self.producer)
            eligible_forecasts = get_marketplace_eligible_forecasts(self.producer)
            eligible_forecast_ids = [forecast.id for forecast in eligible_forecasts]
            self.fields["forecast"].queryset = (
                ProductionForecast.objects
                .filter(id__in=eligible_forecast_ids)
                .select_related("product")
                .order_by("-period_start", "-created_at")
            )
        else:
            self.fields["product"].queryset = self.fields["product"].queryset.none()
            self.fields["forecast"].queryset = ProductionForecast.objects.none()

        if self.lock_listing_source:
            self.fields["listing_source"].disabled = True

        if self.lock_product:
            self.fields["product"].disabled = True

    def clean(self):
        cleaned_data = super().clean()

        listing_source = cleaned_data.get("listing_source") or self.LISTING_SOURCE_STOCK
        product = cleaned_data.get("product")
        forecast = cleaned_data.get("forecast")
        quantity = cleaned_data.get("quantity")
        delivery_mode = cleaned_data.get("delivery_mode")
        delivery_radius_km = cleaned_data.get("delivery_radius_km")
        status = cleaned_data.get("status")
        expiration_mode = cleaned_data.get("expiration_mode") or self.EXPIRY_MODE_NONE
        expires_at = cleaned_data.get("expires_at")
        now = timezone.now()

        if self.producer and product and quantity:
            if listing_source == self.LISTING_SOURCE_STOCK:
                cleaned_data["forecast"] = None
                stock = get_stock_for_product(self.producer, product)
                max_publishable = get_max_publishable_quantity(stock)
                if max_publishable <= 0:
                    self.add_error("product", "Este produto não tem excedente atual para publicar.")
                elif quantity > max_publishable:
                    self.add_error(
                        "quantity",
                        f"A quantidade excede o máximo publicável ({max_publishable} {product.unit})."
                    )
            elif listing_source == self.LISTING_SOURCE_FORECAST:
                cleaned_data["stock"] = None
                if not forecast:
                    self.add_error("forecast", "Seleciona uma previsão de produção.")
                else:
                    if forecast.producer_id != self.producer.id:
                        self.add_error("forecast", "Previsão inválida para este produtor.")
                    if not forecast.is_marketplace_enabled:
                        self.add_error("forecast", "Esta previsão não está ativa para marketplace.")
                    if product and forecast.product_id != product.id:
                        self.add_error("forecast", "A previsão selecionada não corresponde ao produto.")
                    max_publishable = get_forecast_available_quantity(forecast)
                    if max_publishable <= 0:
                        self.add_error("forecast", "Esta previsão não tem quantidade disponível para pré-venda.")
                    elif quantity > max_publishable:
                        self.add_error(
                            "quantity",
                            (
                                "A quantidade excede o máximo disponível desta previsão "
                                f"({max_publishable} {forecast.product.unit})."
                            ),
                        )
            else:
                self.add_error("listing_source", "Origem da oferta inválida.")

        if delivery_mode in {DeliveryMode.DELIVERY, DeliveryMode.BOTH}:
            if not delivery_radius_km:
                self.add_error("delivery_radius_km", "Indica o raio de entrega.")
        else:
            cleaned_data["delivery_radius_km"] = None
            cleaned_data["delivery_fee"] = None

        expires_at_final = None
        if expiration_mode == self.EXPIRY_MODE_DATE:
            if not expires_at:
                self.add_error("expires_at", "Indica a data/hora de expiração.")
            else:
                if timezone.is_naive(expires_at):
                    expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())
                expires_at_final = expires_at
        elif expiration_mode != self.EXPIRY_MODE_NONE:
            self.add_error("expiration_mode", "Modo de expiração inválido.")

        if status == ListingStatus.ACTIVE and expires_at_final and expires_at_final <= now:
            self.add_error("expires_at", "Para manter ativo, a expiração tem de ser no futuro.")

        if status == ListingStatus.EXPIRED and not expires_at_final:
            expires_at_final = now

        if listing_source == self.LISTING_SOURCE_FORECAST and forecast and not self.errors.get("forecast"):
            cleaned_data["product"] = forecast.product

        cleaned_data["expires_at_final"] = expires_at_final
        return cleaned_data


class MarketplaceEditForm(forms.Form):
    EXPIRY_MODE_NONE = "none"
    EXPIRY_MODE_TIMER = "timer"
    EXPIRY_MODE_DATE = "date"

    EXPIRY_MODE_CHOICES = (
        (EXPIRY_MODE_NONE, "Sem prazo"),
        (EXPIRY_MODE_TIMER, "Definir por timer"),
        (EXPIRY_MODE_DATE, "Definir data e hora"),
    )

    quantity_total = forms.DecimalField(
        label="Quantidade listada",
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "any",
            "placeholder": "Ex: 500",
        }),
    )

    unit_price = forms.DecimalField(
        label="Preço por unidade",
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "any",
            "placeholder": "Ex: 0.45",
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
        label="Mostrar localização no mapa",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    notes = forms.CharField(
        label="Observações",
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 4,
            "placeholder": "Informações adicionais sobre o anúncio...",
        }),
    )

    photo = forms.ImageField(
        label="Nova foto do anúncio",
        required=False,
        widget=forms.ClearableFileInput(attrs={
            "class": "form-control",
            "accept": "image/*",
        }),
    )
    photo_crop = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )

    status = forms.ChoiceField(
        label="Estado da listing",
        choices=(
            (ListingStatus.ACTIVE, "Ativo"),
            (ListingStatus.CANCELLED, "Desativado"),
            (ListingStatus.EXPIRED, "Expirado"),
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    expiration_mode = forms.ChoiceField(
        label="Modo de expiração",
        required=False,
        choices=EXPIRY_MODE_CHOICES,
        initial=EXPIRY_MODE_NONE,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    expires_in = forms.IntegerField(
        label="Timer de expiração (horas)",
        required=False,
        min_value=6,
        max_value=24 * 30,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "1",
            "placeholder": "Entre 6 e 720 horas",
        }),
    )

    expires_at = forms.DateTimeField(
        label="Data e hora de expiração",
        required=False,
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(
            attrs={
                "class": "form-control",
                "type": "datetime-local",
            },
            format="%Y-%m-%dT%H:%M",
        ),
    )

    def __init__(self, *args, **kwargs):
        self.listing = kwargs.pop("listing", None)
        super().__init__(*args, **kwargs)

        if self.listing:
            self.fields["quantity_total"].initial = self.listing.quantity_total
            self.fields["unit_price"].initial = self.listing.unit_price
            self.fields["delivery_mode"].initial = self.listing.delivery_mode
            self.fields["delivery_radius_km"].initial = self.listing.delivery_radius_km
            self.fields["delivery_fee"].initial = self.listing.delivery_fee
            self.fields["show_location_on_map"].initial = bool(
                getattr(self.listing, "show_location_on_map", True)
            )
            self.fields["notes"].initial = self.listing.notes
            self.fields["status"].initial = self.listing.status

            expires_at = getattr(self.listing, "expires_at", None)
            if expires_at:
                local_expires = timezone.localtime(expires_at)
                self.fields["expiration_mode"].initial = self.EXPIRY_MODE_DATE
                self.fields["expires_at"].initial = local_expires.strftime("%Y-%m-%dT%H:%M")
            else:
                self.fields["expiration_mode"].initial = self.EXPIRY_MODE_NONE

    def clean(self):
        cleaned_data = super().clean()

        quantity_total = cleaned_data.get("quantity_total")
        status = cleaned_data.get("status")
        expiration_mode = cleaned_data.get("expiration_mode") or self.EXPIRY_MODE_NONE
        expires_in = cleaned_data.get("expires_in")
        expires_at = cleaned_data.get("expires_at")
        delivery_mode = cleaned_data.get("delivery_mode")
        delivery_radius_km = cleaned_data.get("delivery_radius_km")
        now = timezone.now()

        if self.listing:
            has_stock_source = bool(self.listing.stock_id)
            has_forecast_source = bool(self.listing.forecast_id)
            if has_stock_source == has_forecast_source:
                raise forms.ValidationError(
                    "Este anúncio está com origem inválida (stock/previsão). Ajuste primeiro os dados da listing."
                )

        if self.listing and quantity_total is not None:
            reserved_quantity = Decimal(str(self.listing.quantity_reserved or 0))
            if quantity_total < reserved_quantity:
                self.add_error(
                    "quantity_total",
                    f"A quantidade listada não pode ser inferior à reservada ({reserved_quantity}).",
                )

            if has_stock_source:
                source_available = get_max_publishable_quantity(self.listing.stock)
                source_unit = self.listing.product.unit
            else:
                source_available = get_forecast_available_quantity(
                    self.listing.forecast,
                    exclude_listing_id=self.listing.id,
                )
                source_unit = self.listing.product.unit

            max_allowed = max(
                source_available + reserved_quantity,
                Decimal(str(self.listing.quantity_total or 0)),
            )
            if quantity_total > max_allowed:
                self.add_error(
                    "quantity_total",
                    f"A quantidade excede o máximo disponível para esta origem ({max_allowed} {source_unit}).",
                )

        if delivery_mode in {DeliveryMode.DELIVERY, DeliveryMode.BOTH}:
            if not delivery_radius_km:
                self.add_error("delivery_radius_km", "Indica o raio de entrega.")
        else:
            cleaned_data["delivery_radius_km"] = None
            cleaned_data["delivery_fee"] = None

        expires_at_final = None
        if expiration_mode == self.EXPIRY_MODE_TIMER:
            if expires_in is None:
                self.add_error("expires_in", "Indica a duração entre 6 e 720 horas.")
            else:
                expires_at_final = now + timedelta(hours=expires_in)
        elif expiration_mode == self.EXPIRY_MODE_DATE:
            if not expires_at:
                self.add_error("expires_at", "Indica a data/hora de expiração.")
            else:
                if timezone.is_naive(expires_at):
                    expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())
                expires_at_final = expires_at

        if status == ListingStatus.ACTIVE and expires_at_final and expires_at_final <= now:
            self.add_error("expires_at", "Para manter ativo, a expiração tem de ser no futuro.")

        if status == ListingStatus.EXPIRED and not expires_at_final:
            expires_at_final = now

        cleaned_data["expires_at_final"] = expires_at_final
        return cleaned_data
