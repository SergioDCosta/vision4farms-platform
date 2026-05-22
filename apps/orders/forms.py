from decimal import Decimal

from django import forms

from apps.orders.models import OrderStatus


ORDER_NOTES_MAX_LENGTH = 1000

SELLER_CANCEL_REASON_CHOICES = (
    ("Sem stock disponível", "Sem stock disponível"),
    ("Problema logístico", "Problema logístico"),
    ("Erro no anúncio", "Erro no anúncio"),
    ("Produto indisponível", "Produto indisponível"),
    ("Outro", "Outro"),
)


class OrderCreateForm(forms.Form):
    quantity = forms.DecimalField(
        max_digits=14,
        decimal_places=3,
        min_value=Decimal("0.001"),
        error_messages={
            "required": "Indique a quantidade a comprar.",
            "invalid": "Quantidade inválida.",
            "min_value": "A quantidade tem de ser superior a zero.",
        },
    )
    buyer_notes = forms.CharField(required=False, max_length=ORDER_NOTES_MAX_LENGTH)
    need_id = forms.CharField(required=False)


class SellerStatusUpdateForm(forms.Form):
    cancel_reason = forms.ChoiceField(
        required=False,
        choices=SELLER_CANCEL_REASON_CHOICES,
    )
    notes = forms.CharField(required=False, max_length=ORDER_NOTES_MAX_LENGTH)

    def __init__(self, *args, status=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.status = status

    def clean_cancel_reason(self):
        cancel_reason = self.cleaned_data.get("cancel_reason", "").strip()
        if self.status == OrderStatus.CANCELLED and not cancel_reason:
            raise forms.ValidationError("Escolha um motivo para cancelar a encomenda.")
        return cancel_reason
