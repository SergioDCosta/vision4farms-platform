from decimal import Decimal
from datetime import timedelta

from django import forms
from django.utils import timezone

from apps.marketplace.models import DeliveryMode, ListingStatus
from apps.marketplace.services import (
    get_publishable_products,
    get_stock_for_product,
    get_max_publishable_quantity,
)


class MarketplacePublishForm(forms.Form):
    EXPIRY_MODE_NONE = "none"
    EXPIRY_MODE_TIMER = "timer"
    EXPIRY_MODE_DATE = "date"

    EXPIRY_MODE_CHOICES = (
        (EXPIRY_MODE_NONE, "Sem prazo"),
        (EXPIRY_MODE_TIMER, "Definir por temporizador"),
        (EXPIRY_MODE_DATE, "Definir data e hora"),
    )

    EXPIRY_TIMER_24H = "24h"
    EXPIRY_TIMER_7D = "7d"
    EXPIRY_TIMER_30D = "30d"
    EXPIRY_TIMER_CHOICES = (
        ("", "Selecionar duração..."),
        (EXPIRY_TIMER_24H, "24 horas"),
        (EXPIRY_TIMER_7D, "7 dias"),
        (EXPIRY_TIMER_30D, "30 dias"),
    )

    product = forms.ModelChoiceField(
        label="Produto",
        queryset=None,
        empty_label="Selecione um produto...",
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

    photo = forms.ImageField(
        label="Foto do anúncio",
        required=False,
        widget=forms.ClearableFileInput(attrs={
            "class": "form-control",
            "accept": "image/*",
        }),
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

    expires_in = forms.ChoiceField(
        label="Timer de expiração",
        required=False,
        choices=EXPIRY_TIMER_CHOICES,
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
        super().__init__(*args, **kwargs)

        if self.producer:
            self.fields["product"].queryset = get_publishable_products(self.producer)
        else:
            self.fields["product"].queryset = self.fields["product"].queryset.none()

    def clean(self):
        cleaned_data = super().clean()

        product = cleaned_data.get("product")
        quantity = cleaned_data.get("quantity")
        delivery_mode = cleaned_data.get("delivery_mode")
        delivery_radius_km = cleaned_data.get("delivery_radius_km")
        status = cleaned_data.get("status")
        expiration_mode = cleaned_data.get("expiration_mode") or self.EXPIRY_MODE_NONE
        expires_in = cleaned_data.get("expires_in")
        expires_at = cleaned_data.get("expires_at")
        now = timezone.now()

        if product and quantity and self.producer:
            stock = get_stock_for_product(self.producer, product)
            max_publishable = get_max_publishable_quantity(stock)
            if quantity > max_publishable:
                self.add_error(
                    "quantity",
                    f"A quantidade excede o máximo publicável ({max_publishable} {product.unit})."
                )

        if delivery_mode in {DeliveryMode.DELIVERY, DeliveryMode.BOTH}:
            if not delivery_radius_km:
                self.add_error("delivery_radius_km", "Indica o raio de entrega.")
        else:
            cleaned_data["delivery_radius_km"] = None
            cleaned_data["delivery_fee"] = None

        expires_at_final = None
        if expiration_mode == self.EXPIRY_MODE_TIMER:
            timer_map = {
                self.EXPIRY_TIMER_24H: timedelta(hours=24),
                self.EXPIRY_TIMER_7D: timedelta(days=7),
                self.EXPIRY_TIMER_30D: timedelta(days=30),
            }
            delta = timer_map.get(expires_in)
            if not delta:
                self.add_error("expires_in", "Seleciona um timer válido.")
            else:
                expires_at_final = now + delta
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
        decimal_places=2,
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

        if self.listing and quantity_total is not None:
            reserved_quantity = Decimal(str(self.listing.quantity_reserved or 0))
            if quantity_total < reserved_quantity:
                self.add_error(
                    "quantity_total",
                    f"A quantidade listada não pode ser inferior à reservada ({reserved_quantity}).",
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
