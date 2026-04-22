from django import forms


class SupportTicketCreateForm(forms.Form):
    subject = forms.CharField(
        label="Assunto",
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Resumo curto do pedido",
            }
        ),
    )
    message = forms.CharField(
        label="Mensagem",
        max_length=4000,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 5,
                "placeholder": "Descreve o que precisas de suporte.",
            }
        ),
    )

    def clean_subject(self):
        value = " ".join((self.cleaned_data.get("subject") or "").split()).strip()
        if not value:
            raise forms.ValidationError("Indica o assunto do pedido.")
        return value

    def clean_message(self):
        value = (self.cleaned_data.get("message") or "").strip()
        if not value:
            raise forms.ValidationError("Indica a mensagem do pedido.")
        return value


class SupportTicketReplyForm(forms.Form):
    reply_message = forms.CharField(
        label="Resposta ao utilizador",
        max_length=4000,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 6,
                "placeholder": "Escreve a resposta final para o utilizador.",
            }
        ),
    )

    def clean_reply_message(self):
        value = (self.cleaned_data.get("reply_message") or "").strip()
        if not value:
            raise forms.ValidationError("Indica a resposta para fechar o ticket.")
        return value

