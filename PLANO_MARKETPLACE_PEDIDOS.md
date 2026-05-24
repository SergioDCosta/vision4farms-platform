# Plano de Implementação — Marketplace, Pedidos & Necessidades

> Documento consolidado: PDF do orientador + feedback detalhado + análise do código atual.
> Cada tarefa tem ficheiros exatos, linhas de referência e critérios de teste.

---

## Prioridades

| # | Tarefa | Tipo | Prioridade |
|---|--------|------|------------|
| 1 | ~~Corrigir erro `FOR UPDATE` ao aceitar recomendação~~ | Bug crítico | ✅ Resolvido |
| 2 | ~~Bloquear anúncios duplicados acima da margem de stock~~ | Bug crítico | ✅ Resolvido |
| 3 | ~~`/necessidades/` mostrar pedidos dos clientes em destaque~~ | UX + lógica | ✅ Resolvido |
| 4 | ~~Badges contextuais nos cards do marketplace~~ | UX + lógica | ✅ Resolvido |
| 5 | ~~Detalhe de stock ligar pedidos externos~~ | UX + lógica | ✅ Resolvido |
| 6 | ~~Marketplace: 2 novos tabs (Compras & Respostas)~~ | Feature | ✅ Resolvido |
| 7 | ~~Pedidos: linha temporal por urgência + risco em destaque~~ | UX | ✅ Resolvido |
| 8 | ~~Empty state do marketplace: remover CTA errado~~ | UX | ✅ Resolvido |

---

## TAREFA 1 — Corrigir erro `FOR UPDATE` ao aceitar recomendação

### Problema

Ao aceitar uma recomendação (`/recomendacoes/<id>/aceitar/`), ocorre erro PostgreSQL:

```
FOR UPDATE cannot be applied to the nullable side of an outer join
```

### Localização exata

**`apps/orders/services.py`, linhas 1317–1320:**

```python
locked_listings = {
    listing.id: listing
    for listing in (
        MarketplaceListing.objects
        .select_for_update()                          # ← sem of=("self",)
        .select_related("product", "producer", "stock", "forecast", "need", "need__producer")
        .filter(id__in=required_by_listing.keys())
    )
}
```

`stock`, `forecast` e `need` são FKs **nullable** → Django gera `LEFT OUTER JOIN` → PostgreSQL não consegue aplicar `FOR UPDATE` ao lado nullable.

Também verificar a linha 1279–1283 (lock da recomendação):

```python
recommendation = (
    recommendation.__class__.objects
    .select_for_update()
    .select_related("product", "producer", "need")  # "need" é nullable
    .get(id=recommendation.id)
)
```

### Correção

**Passo 1** — linhas 1317–1320: adicionar `of=("self",)`:

```python
locked_listings = {
    listing.id: listing
    for listing in (
        MarketplaceListing.objects
        .select_for_update(of=("self",))              # ← só bloqueia o listing
        .select_related("product", "producer", "stock", "forecast", "need", "need__producer")
        .filter(id__in=required_by_listing.keys())
    )
}
```

**Passo 2** — linhas 1279–1283: mesma correção:

```python
recommendation = (
    recommendation.__class__.objects
    .select_for_update(of=("self",))                  # ← só bloqueia a recomendação
    .select_related("product", "producer", "need")
    .get(id=recommendation.id)
)
```

**Pesquisar** por outros `select_for_update()` sem `of=()` em `apps/orders/services.py` e `apps/recommendations/views.py` e aplicar o mesmo padrão se houver relações nullable.

### Ficheiros a alterar

- `apps/orders/services.py`

### Testes

- Criar recomendação e aceitar → sem erro PostgreSQL
- Confirmar que `order_group`, `orders` e `order_items` são criados
- Confirmar que reservas e listings são atualizados corretamente

---

## TAREFA 2 — Bloquear anúncios duplicados acima da margem de stock

### Problema

Para **stock atual**: é possível criar múltiplos anúncios ativos do mesmo `stock_id` excedendo a quantidade disponível.

```
Stock disponível: 200 kg
Anúncio 1 ativo: 200 kg  ← permitido
Anúncio 2 novo: 200 kg   ← TAMBÉM permitido (errado)
```

Para **pré-venda (forecast)**: a verificação já existe em `_get_open_forecast_published_quantity()` e funciona.

### Causa raiz

**`apps/marketplace/services.py`, função `get_max_publishable_quantity()` (linhas ~376–391):**

Chama `calculate_inventory_commitment_state()` que retorna `temporal_sellable_quantity` — mas este valor **não subtrai** a quantidade já anunciada em listings ACTIVE/RESERVED do mesmo `stock_id`.

Em contraste, `get_forecast_available_quantity()` (linhas ~148–157) **subtrai** explicitamente via `_get_open_forecast_published_quantity()`.

### Correção

**`apps/marketplace/services.py`** — corrigir `get_max_publishable_quantity()`:

```python
def get_max_publishable_quantity(stock, exclude_listing_id=None):
    """
    Retorna a quantidade máxima publicável para um stock,
    descontando o que já está em anúncios ativos do mesmo stock.
    """
    from apps.inventory.services import calculate_inventory_commitment_state

    state = calculate_inventory_commitment_state(stock)
    base_max = state.get("temporal_sellable_quantity", Decimal("0"))

    # Subtrair quantidade já anunciada em listings ativos do mesmo stock
    already_published = _get_open_stock_published_quantity(
        stock, exclude_listing_id=exclude_listing_id
    )

    return max(Decimal("0"), base_max - already_published)


def _get_open_stock_published_quantity(stock, exclude_listing_id=None):
    """
    Soma quantity_available + quantity_reserved dos listings ACTIVE/RESERVED
    para o mesmo stock_id (excluindo o próprio ao editar).
    """
    qs = MarketplaceListing.objects.filter(
        stock=stock,
        status__in=["ACTIVE", "RESERVED"],
    )
    if exclude_listing_id:
        qs = qs.exclude(id=exclude_listing_id)

    result = qs.aggregate(
        total=models.Sum(
            models.F("quantity_available") + models.F("quantity_reserved")
        )
    )
    return result["total"] or Decimal("0")
```

**`apps/marketplace/forms.py`** — corrigir validação de STOCK (linhas ~254–265):

```python
# Ao editar, passar exclude_listing_id=self.instance.id
max_publishable = get_max_publishable_quantity(
    stock,
    exclude_listing_id=getattr(self.instance, "id", None)
)
if max_publishable <= 0:
    self.add_error("quantity", "Já tem toda a quantidade disponível anunciada para este produto.")
elif quantity > max_publishable:
    self.add_error("quantity", f"Só pode anunciar mais {max_publishable} kg sem comprometer os pedidos existentes.")
```

**`apps/marketplace/views.py`** — corrigir `get_publishable_products_summary()` para stock:

```python
# Ao calcular publishable_quantity para o resumo de produtos, passar exclude_listing_id
# quando for edição de um listing existente
publishable_quantity = get_max_publishable_quantity(
    stock,
    exclude_listing_id=editing_listing_id  # None na publicação, listing.id na edição
)
```

### Ficheiros a alterar

- `apps/marketplace/services.py`
- `apps/marketplace/forms.py`
- `apps/marketplace/views.py`

### Testes

- Publicar anúncio de 200 kg com stock de 200 kg → OK
- Tentar publicar 2.º anúncio de 1 kg → bloqueado com mensagem clara
- Fechar 1.º anúncio → quantidade volta a ficar publicável
- Editar anúncio existente → não se bloqueia a si próprio
- Pré-venda: comportamento mantido igual (já funcionava)
- `python manage.py check` sem erros

---

## TAREFA 3 — `/necessidades/` mostrar pedidos dos clientes em destaque

### Problema

O orientador espera ver **imediatamente** ao entrar em `/necessidades/` os pedidos dos seus clientes.
Atualmente estão numa página separada (`/necessidades/pedidos-clientes/`).

### Lógica a adicionar

**`apps/needs/views.py`** — função `needs_index_view`: adicionar ao contexto:

```python
from apps.needs.models import ExternalCustomerDemand
from django.utils import timezone
from django.db.models import Sum

today = timezone.now().date()

# Pedidos ativos do produtor, ordenados por urgência
active_demands = ExternalCustomerDemand.objects.filter(
    producer=producer,
    status__in=["OPEN", "PARTIALLY_COVERED", "COVERED"]
).select_related("product", "product__category").order_by("requested_delivery_date")

# Calcular urgência e défice em Python (reutiliza demand_plans já calculado em external_customer_demands_view)
# Ou chamar o mesmo serviço que calcula os demand_plans (já existe em apps/needs/services.py)
# Verificar nome exato da função de planeamento e reutilizar

# KPIs para o bloco de resumo
demands_total_qty = active_demands.aggregate(t=Sum("requested_quantity"))["t"] or 0
demands_open_count = active_demands.filter(status="OPEN").count()
demands_covered_count = active_demands.filter(status="COVERED").count()

context.update({
    "active_demands_preview": active_demands[:10],   # máximo 10 no preview
    "demands_total_qty": demands_total_qty,
    "demands_open_count": demands_open_count,
    "demands_covered_count": demands_covered_count,
    "has_more_demands": active_demands.count() > 10,
})
```

### UI a criar

**`templates/needs/index.html`** — adicionar bloco **antes** da secção "Procuras Geradas", logo após os KPI cards:

```
┌──────────────────────────────────────────────────────────────┐
│ PEDIDOS DOS MEUS CLIENTES                    [Ver todos →]   │
│                                                              │
│ [ Total: 1200 kg ] [ Abertos: 3 ] [ Cobertos: 2 ]          │
│                                                              │
│ Tabela:                                                      │
│ Cliente   | Produto    | Qtd  | Entrega  | Stock | Dif | Est │
│ João S.   | Tom. Cherry| 200kg| 15 Jun   | 120kg | -80 | 🔴 │
│ Maria C.  | Batata     | 300kg| 20 Jun   | 450kg | +150| 🟢 │
│ ...                                                          │
│                                                              │
│ [+ Novo pedido de cliente]    [Ver planeamento completo →]  │
└──────────────────────────────────────────────────────────────┘
```

**Colunas da tabela:**
- Cliente (nome + contacto em tooltip)
- Produto
- Quantidade pedida
- Data de entrega (+ badge de urgência: 🔴/🟠/🟡/🟢)
- Stock disponível (do `Stock.available_quantity` desse produto)
- Diferença (stock - pedido; negativo = défice em vermelho)
- Estado (badge colorido)
- Ações: Editar | Cancelar

**"Ver todos"** → link para `/necessidades/pedidos-clientes/`

### Ficheiros a alterar

- `apps/needs/views.py`
- `templates/needs/index.html`

### Testes

- Entrar em `/necessidades/` com pedidos externos ativos → bloco aparece no topo
- Valores de stock e diferença correspondem ao planeamento temporal
- Link "Ver todos" funciona
- Sem pedidos → bloco mostra empty state (não colapsa, mostra "Sem pedidos de clientes")

---

## TAREFA 4 — Badges contextuais nos cards do marketplace

### Problema

O orientador perguntou: "Como saber a quais anúncios eu fiz o pedido? Ou as propostas que tenho para as necessidades?"

Atualmente `listing_card.html` e `need_card.html` **não mostram** badges de estado do utilizador atual.

### Lógica a adicionar

**`apps/marketplace/views.py`** — `marketplace_index_view`, quando `tab == 'explorar'`:

```python
from apps.orders.models import OrderItem

# 1. Anúncios onde o utilizador atual já fez pedido
#    Mapear listing_id → item_status do OrderItem mais recente
buyer_order_items = OrderItem.objects.filter(
    order__buyer_producer=producer,
    listing_id__in=[l.id for l in listings]
).select_related("order").order_by("-created_at")

order_status_by_listing = {}
for item in buyer_order_items:
    if item.listing_id not in order_status_by_listing:
        order_status_by_listing[item.listing_id] = {
            "item_status": item.item_status,
            "order_id": item.order_id,
        }

# 2. Necessidades onde o utilizador já respondeu
#    Mapear need_id → need_response_status da proposta enviada
my_responses = MarketplaceListing.objects.filter(
    producer=producer,
    need__isnull=False,
    need_id__in=[r.need_id for r in marketplace_need_rows]
).values("need_id", "need_response_status", "id")

response_status_by_need = {
    r["need_id"]: {"status": r["need_response_status"], "listing_id": r["id"]}
    for r in my_responses
}

context.update({
    "order_status_by_listing": order_status_by_listing,
    "response_status_by_need": response_status_by_need,
})
```

**Alternativa mais simples** (se listings já chegam como lista de dicts): anotar diretamente cada listing/need no loop da view com `viewer_order_status` e `viewer_response_status`.

### UI — `templates/marketplace/partials/listing_card.html`

Adicionar badge **após** o badge de estado do listing (linha ~17):

```html
{% if viewer_order_status %}
  <span class="mk-badge mk-badge--{{ viewer_order_status.item_status|lower }}">
    {% if viewer_order_status.item_status == "PENDING" %}Pedido pendente
    {% elif viewer_order_status.item_status == "CONFIRMED" %}Encomenda confirmada
    {% elif viewer_order_status.item_status == "IN_DELIVERY" %}Em entrega
    {% elif viewer_order_status.item_status == "COMPLETED" %}Concluído
    {% elif viewer_order_status.item_status == "CANCELLED" %}Cancelado
    {% endif %}
  </span>
  <a href="{% url 'orders:detail' viewer_order_status.order_id %}" class="mk-card-action mk-card-action--secondary">
    Ver encomenda
  </a>
{% endif %}
```

### UI — `templates/marketplace/partials/need_card.html`

Substituir o botão "Editar proposta" existente por lógica mais completa de badge:

```html
{% if viewer_response_status %}
  <span class="mk-badge mk-badge--{{ viewer_response_status.status|lower }}">
    {% if viewer_response_status.status == "PENDING" %}Proposta pendente
    {% elif viewer_response_status.status == "ACCEPTED" %}Proposta aceite
    {% elif viewer_response_status.status == "REJECTED" %}Proposta rejeitada
    {% elif viewer_response_status.status == "CANCELLED" %}Proposta cancelada
    {% elif viewer_response_status.status == "COMPLETED" %}Proposta concluída
    {% endif %}
  </span>
  <a href="{% url 'needs:response_detail' viewer_response_status.listing_id %}" class="mk-card-action mk-card-action--secondary">
    Ver proposta enviada
  </a>
{% else %}
  <a href="..." class="mk-card-action mk-card-action--primary">Concorrer</a>
{% endif %}
```

### Ficheiros a alterar

- `apps/marketplace/views.py`
- `templates/marketplace/partials/listing_card.html`
- `templates/marketplace/partials/need_card.html`

### Testes

- Fazer pedido num anúncio → voltar ao marketplace → badge "Pedido pendente" visível
- Responder a uma necessidade → voltar → badge "Proposta pendente" visível
- Estado muda conforme order/proposal avança

---

## TAREFA 5 — Detalhe de stock ligar pedidos externos

### Problema

O detalhe de stock mostra "compromissos externos" e "margem vendável" mas não mostra **quais pedidos** originam esses compromissos.

### Localização

**`apps/inventory/views.py`**, função de detalhe de stock (linhas ~410–459):
- Já chama `get_buyer_incoming_forecast_projection()` mas **não** consulta `ExternalCustomerDemand`

### Lógica a adicionar

**`apps/inventory/views.py`** (ou `apps/inventory/services.py`):

```python
from apps.needs.models import ExternalCustomerDemand

# Dentro do contexto do detalhe de stock:
external_demands_for_product = ExternalCustomerDemand.objects.filter(
    producer=producer,
    product=stock.product,
    status__in=["OPEN", "PARTIALLY_COVERED", "COVERED"]
).order_by("requested_delivery_date")

context.update({
    "external_demands_for_product": external_demands_for_product,
    "external_demands_count": external_demands_for_product.count(),
})
```

### UI a adicionar

**`templates/inventory/stock_detalhe.html`** — nova secção antes das previsões de produção:

```
┌─────────────────────────────────────────────────────────────┐
│ PEDIDOS DE CLIENTES DESTE PRODUTO (3)                       │
│                                                             │
│ Cliente   | Quantidade | Data Entrega | Estado | Cobertura  │
│ João S.   | 200 kg     | 15 Jun       | 🔴 Aberto | -80kg  │
│ Maria C.  | 100 kg     | 20 Jun       | 🟢 Coberto| +50kg  │
│                                                             │
│              [Ver todos os pedidos deste produto →]         │
└─────────────────────────────────────────────────────────────┘
```

**Link "Ver todos":**
```html
<a href="{% url 'needs:external_demands' %}?product={{ stock.product.id }}">
  Ver todos os pedidos deste produto
</a>
```

**Nota:** Verificar se a view `external_customer_demands_view` já suporta filtro `?product=<id>` no URL. Se não, adicionar esse filtro.

### Ficheiros a alterar

- `apps/inventory/views.py`
- `templates/inventory/stock_detalhe.html`
- `apps/needs/views.py` (adicionar filtro por produto se não existir)

### Testes

- Criar pedidos externos para um produto
- Abrir detalhe de stock desse produto
- Confirmar que a tabela de pedidos aparece
- Confirmar que "Ver todos" vai para a página filtrada

---

## TAREFA 6 — Marketplace: 2 novos tabs (Compras & Respostas)

### Contexto

O marketplace tem hoje 2 tabs: "Marketplace" (tab=todos) e "Meus anúncios" (tab=meus).
O PDF pede 4 tabs. Os 2 que faltam agregam dados de `orders/` e `needs/`.

### Alteração na view

**`apps/marketplace/views.py`** — `marketplace_index_view`:

```python
VALID_TABS = ["explorar", "meus", "compras", "respostas"]
tab = request.GET.get("tab", "explorar")
if tab not in VALID_TABS:
    tab = "explorar"

# Renomear tab "todos" → "explorar" (retrocompatível: redirecionar tab=todos → tab=explorar)

if tab == "compras":
    from apps.orders.models import Order

    buyer_orders = (
        Order.objects.filter(buyer_producer=producer)
        .select_related("group")
        .prefetch_related(
            "items__listing__product",
            "items__listing__producer",
            "items__product",
            "status_history",
        )
        .order_by("-created_at")
    )

    context.update({
        "active_orders": buyer_orders.filter(
            status__in=["PENDING", "CONFIRMED", "IN_PROGRESS", "DELIVERING"]
        ),
        "completed_orders": buyer_orders.filter(status="COMPLETED")[:20],
        "pending_sent_proposals": MarketplaceListing.objects.filter(
            producer=producer,
            need__isnull=False,
            need_response_status="PENDING",
        ).select_related("need__producer", "product"),
    })

elif tab == "respostas":
    from apps.needs.models import Need

    my_open_needs = (
        Need.objects.filter(
            producer=producer,
            status__in=["OPEN", "PARTIALLY_COVERED"],
        )
        .prefetch_related(
            models.Prefetch(
                "marketplacelisting_set",
                queryset=MarketplaceListing.objects.select_related("producer", "product").order_by("-created_at"),
            )
        )
        .select_related("product")
    )

    context.update({
        "my_open_needs": my_open_needs,
        "past_needs": Need.objects.filter(
            producer=producer,
            status__in=["COVERED", "CANCELLED", "IGNORED"],
        ).prefetch_related("marketplacelisting_set").order_by("-updated_at")[:20],
    })
```

### UI — Tab "Compras & Follow-up"

Criar `templates/marketplace/partials/tab_compras.html`:

```
KPIs: [ X em curso ] [ Y aguardam resposta ] [ Z concluídas ]

SECÇÃO "Em Curso"
  Card por ordem ativa:
  - Vendedor + produto + quantidade + preço
  - Linha de progresso: Pendente → Confirmada → Em produção → A entregar → Concluída
  - Método de entrega + data estimada
  - [Ver detalhes] → /encomendas/<uuid>/

SECÇÃO "Aguardam Resposta" (propostas enviadas pendentes)
  Card por proposta:
  - Para quem foi enviada + produto + quantidade + preço proposto
  - "há X dias" + estado "Aguarda decisão"
  - [Ver proposta] → /necessidades/respostas/<listing_id>/

SECÇÃO "Histórico" (colapsável)
  Tabela: Produto | Vendedor | Qtd | Preço | Estado | Data
```

### UI — Tab "Respostas às Necessidades"

Criar `templates/marketplace/partials/tab_respostas.html`:

```
KPIs: [ X necessidades abertas ] [ Y propostas pendentes ] [ Z aceites ]

Por necessidade aberta:
  Cabeçalho: Produto + Qtd necessária + Prazo + Estado de cobertura
  Lista de propostas recebidas:
    - Produtor + quantidade + preço/kg + método entrega
    - [Aceitar] [Rejeitar] [Ver perfil]
  [Editar necessidade] [Ver todas as propostas]

SECÇÃO "Histórico" (colapsável)
  Necessidades cobertas/canceladas com resumo de propostas
```

### URL necessário: aceitar proposta

Verificar se existe `needs:response_accept`. Se não:

```python
# apps/needs/urls.py
path("respostas/<uuid:listing_id>/aceitar/", need_response_accept_view, name="response_accept"),

# apps/needs/views.py — nova view:
@require_POST
@login_required
def need_response_accept_view(request, listing_id):
    listing = get_object_or_404(
        MarketplaceListing,
        id=listing_id,
        need__producer=request.user.producer_profile,
    )
    # Atualizar need_response_status → ACCEPTED
    # Atualizar Need.status → COVERED ou PARTIALLY_COVERED
    # Questão em aberto: criar Order aqui ou deixar fluxo separado?
    ...
```

### Ficheiros a alterar

- `apps/marketplace/views.py`
- `apps/needs/views.py` (nova view se necessário)
- `apps/needs/urls.py` (novo URL se necessário)
- `templates/marketplace/index.html` (adicionar 2 tabs ao HTML)
- `templates/marketplace/partials/tab_compras.html` (novo)
- `templates/marketplace/partials/tab_respostas.html` (novo)

---

## TAREFA 7 — Pedidos: linha temporal por urgência + risco em destaque

### Alteração na view

**`apps/needs/views.py`** — `external_customer_demands_view`:

```python
from django.utils import timezone

today = timezone.now().date()

# Separar em 3 grupos
active_demands = ExternalCustomerDemand.objects.filter(
    producer=producer,
    status__in=["OPEN", "PARTIALLY_COVERED", "COVERED"]
).select_related("product").order_by("requested_delivery_date")  # urgência

fulfilled_demands = ExternalCustomerDemand.objects.filter(
    producer=producer, status="FULFILLED"
).order_by("-fulfilled_at")

cancelled_demands = ExternalCustomerDemand.objects.filter(
    producer=producer, status="CANCELLED"
).order_by("-cancelled_at")

# Anotar urgência em Python
for demand in active_demands:
    days = (demand.requested_delivery_date - today).days
    if days < 0:
        demand.urgency = "overdue"
    elif days <= 7:
        demand.urgency = "critical"
    elif days <= 14:
        demand.urgency = "warning"
    else:
        demand.urgency = "ok"
    demand.days_remaining = days
```

### Nova estrutura da template

**`templates/needs/external_demands.html`** — reorganizar em 3 secções:

**Secção 1 — Linha Temporal (topo, primária)**

```
TÍTULO: "Pedidos por Urgência de Entrega"

LEGENDA: 🔴 Atrasado | 🟠 Crítico ≤7d | 🟡 Atenção ≤14d | 🟢 OK

Cards verticais ordenados por data:
┌─ [🔴 ATRASADO — 10 Mai] ────────────────────────────────────┐
│ João Silva · Tomate Cherry · 200 kg                         │
│ Défice: 80 kg  |  Estado: Em aberto                         │
│ [Editar]  [Ver análise]  [Cancelar]                         │
└─────────────────────────────────────────────────────────────┘
```

**Secção 2 — Análise de Risco (destaque, após timeline)**

Mover os "Planning by Product" cards para cima e torná-los proeminentes:

```
TÍTULO: "Análise de Risco de Incumprimento"  + badge "X produtos em défice"

Por produto:
┌─ 🔴 Tomate Cherry — DÉFICE 280 kg ──────────────────────────┐
│  Pedidos totais:   480 kg                                   │
│  Stock agora:      120 kg                                   │
│  Produção prevista: 80 kg (até data mais próxima)           │
│  Reservado:         20 kg                                   │
│  ────────────────────────                                   │
│  FALTA:            280 kg                                   │
│                                                             │
│  Pedidos afetados:                                          │
│  · João Silva 200kg · data: 10 Mai (mais urgente)           │
│  · Maria Costa 80kg · data: 20 Mai                          │
│                                                             │
│  [Criar procura no marketplace]  [Ver stock]                │
└─────────────────────────────────────────────────────────────┘

┌─ 🟢 Batata — Totalmente coberto ────────────────────────────┐
│  Pedidos: 300kg · Disponível: 450kg · Excesso: 150kg        │
└─────────────────────────────────────────────────────────────┘
```

**Secção 3 — Histórico (colapsável)**

```
[▼ Histórico de Pedidos]
Tabela: Cliente | Produto | Qtd | Data Entrega | Estado | Data Fecho
```

### Ficheiros a alterar

- `apps/needs/views.py`
- `templates/needs/external_demands.html`
- `templates/needs/partials/demand_timeline_card.html` (novo partial)
- `templates/needs/partials/risk_analysis_card.html` (novo partial)

---

## TAREFA 8 — Empty state do marketplace: remover CTA errado

### Problema

**`templates/marketplace/index.html`, linhas 136–151:**

```html
<a href="{% url 'needs:index' %}?show_need_form=1..."
   class="mk-card-action mk-card-action--primary">
  Anunciar necessidade   ← REMOVER ISTO
</a>
```

### Correção

Substituir por copy sem CTA (ou CTA de publicar excedente):

```html
{% if not listings and not marketplace_need_rows %}
  <article class="mk-empty">
    <i class="bi bi-search"></i>
    <h3>Sem resultados disponíveis</h3>
    <p>
      {% if q or selected_category_id or selected_origin or selected_kind != "all" %}
        Não encontrámos ofertas ou procuras com os filtros aplicados.
        <a href="{% url 'marketplace:index' %}">Limpar filtros</a>
      {% else %}
        Não existem ofertas nem procuras ativas no marketplace neste momento.
      {% endif %}
    </p>
  </article>
{% endif %}
```

O botão **"+ Publicar excedente"** já deve existir no topo direito da página. Confirmar que está presente e visível no header da tab "Explorar Marketplace".

### Ficheiros a alterar

- `templates/marketplace/index.html`

---

## Questões em aberto (definir antes de implementar TAREFA 6)

1. **Aceitar proposta cria Order?** Ao aceitar uma proposta numa necessidade, cria-se uma `Order` automaticamente ou apenas muda o estado e o comprador confirma depois?
2. **Rejeição automática das outras?** Ao aceitar uma proposta, as restantes pendentes ficam automaticamente rejeitadas?
3. **Tab padrão do marketplace:** Manter "Explorar" como default ou personalizar (ex: se tiver ordens ativas, abrir em "Compras")?
4. **Filtro por produto em pedidos externos:** A view `external_customer_demands_view` já suporta `?product=<id>` no filtro? Se não, confirmar antes da TAREFA 5.

---

## Resumo de ficheiros a alterar

| Ficheiro | Tarefas |
|---|---|
| `apps/orders/services.py` | T1 |
| `apps/marketplace/services.py` | T2 |
| `apps/marketplace/forms.py` | T2 |
| `apps/marketplace/views.py` | T2, T4, T6 |
| `apps/needs/views.py` | T3, T6, T7 |
| `apps/needs/urls.py` | T6 |
| `apps/inventory/views.py` | T5 |
| `templates/marketplace/index.html` | T6, T8 |
| `templates/marketplace/partials/listing_card.html` | T4 |
| `templates/marketplace/partials/need_card.html` | T4 |
| `templates/marketplace/partials/tab_compras.html` | T6 (novo) |
| `templates/marketplace/partials/tab_respostas.html` | T6 (novo) |
| `templates/needs/index.html` | T3 |
| `templates/needs/external_demands.html` | T7 |
| `templates/needs/partials/demand_timeline_card.html` | T7 (novo) |
| `templates/needs/partials/risk_analysis_card.html` | T7 (novo) |
| `templates/inventory/stock_detalhe.html` | T5 |

**Schema da base de dados: zero alterações.** Todos os dados já existem nos modelos atuais.
