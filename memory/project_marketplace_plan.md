---
name: project-marketplace-plan
description: Plano de implementação para módulos Marketplace, Pedidos e Necessidades — 8 tarefas priorizadas com ficheiros exatos
metadata:
  type: project
---

O plano completo está em `PLANO_MARKETPLACE_PEDIDOS.md` na raiz do projeto.

**Prioridade crítica (bugs):**
- T1: Corrigir `FOR UPDATE` em `apps/orders/services.py` linhas 1279–1283 e 1317–1320 → adicionar `of=("self",)`
- T2: Bloquear anúncios duplicados acima da margem em `apps/marketplace/services.py` → nova função `_get_open_stock_published_quantity()`

**Prioridade alta (UX + lógica):**
- T3: `/necessidades/` mostrar pedidos de clientes em destaque (antes da secção "Procuras Geradas")
- T4: Badges contextuais nos cards do marketplace (listing_card.html, need_card.html)
- T5: Detalhe de stock ligar pedidos externos (inventory/views.py + stock_detalhe.html)

**Prioridade média:**
- T6: Marketplace 4 tabs: adicionar "Compras & Follow-up" e "Respostas às Necessidades"
- T7: Pedidos: linha temporal por urgência + análise de risco em destaque

**Prioridade baixa:**
- T8: Empty state marketplace: remover CTA "Anunciar necessidade" (index.html linhas 136–151)

**Why:** Feedback combinado do orientador (PDF + documento de feedback detalhado).
**How to apply:** Implementar sempre na ordem de prioridade. Zero alterações ao schema da BD.
