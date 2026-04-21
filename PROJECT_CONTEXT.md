# COOPERATIVA — Contexto Atual do Projeto (Resumo Técnico)

## 1) Visão Geral
- Plataforma B2B entre produtores para:
  - gestão de inventário e previsão de produção;
  - marketplace (stock atual + pré-venda);
  - recomendações de compra/venda;
  - encomendas com workflow operacional;
  - mensagens em tempo real entre produtores.
- Fonte de verdade do domínio: SQL manual (`sqlscript.sql` + alterações aplicadas diretamente na BD).
- Modelos de negócio Django usam `managed = False` (sem migrations nessas tabelas).

## 2) Stack e Aplicação
- Backend: Django 6.0.3.
- DB: PostgreSQL.
- Realtime: Django Channels + Redis + Daphne.
- Frontend: Django Templates + HTMX (shell server-rendered).
- Storage: `default_storage` via Cloudinary (`MediaCloudinaryStorage`) em produção.
- Static: WhiteNoise (`CompressedManifestStaticFilesStorage`).
- Imagem/crop: Pillow (crop de foto de anúncio).
- Config/env: `python-decouple` (`.env`).
- Segurança recente:
  - `django-ratelimit` em login/registo;
  - validação de anexos por extensão + MIME (`content_type`) + limite 10MB.

## 3) Organização do Código
- `config/`
  - `settings.py`: apps, middleware, DB, storages, channels.
  - `urls.py`: composição de rotas por app.
  - `asgi.py`: HTTP + WS (`ProtocolTypeRouter`).
- `apps/`
  - `accounts`: autenticação/registo/verificação/reset.
  - `inventory`: produtos do produtor, stocks, movimentos, previsões.
  - `marketplace`: publicação, detalhe, edição, gestão de anúncios.
  - `recommendations`: wizard e recomendações persistidas.
  - `orders`: encomendas, grupos, status, reservas.
  - `messaging`: conversas 1:1, texto+anexo, WS.
  - `dashboard`, `settings_app`, `alerts`, `notifications_app`, `integrations`, `catalog`.
- `templates/`: estrutura por domínio (`inventory/`, `marketplace/`, `orders/`, `messaging/`, etc.).
- `static/`: assets globais.

## 4) Convenções Arquiteturais
- Views tendencialmente finas; regras em `services.py`.
- Autenticação principal custom:
  - `request.current_user` (middleware de sessão próprio).
  - decorators custom (`login_required`, `client_only_required`, `admin_required`).
- Para uploads/URLs, usar sempre `default_storage` (nunca hardcode `/media/...`).

## 5) Módulos e Estado Funcional

### 5.1 Accounts / Segurança
- Login, registo, verificação por token, convite admin, reset password.
- Rate limit ativo:
  - login por IP: `10/5m`;
  - login por email: `5/5m`;
  - registo por IP: `5/30m`;
  - modo `block=False` com mensagem amigável.
- NIF continua em texto simples (sem encriptação nesta iteração).

### 5.2 Inventory
- `products.unit` é global.
- `producer_products.producer_description` é descrição específica do produtor.
- `stocks` usa:
  - `current_quantity`, `reserved_quantity`, `safety_stock`, `surplus_threshold`.
- Regra de estado (apenas stock atual):
  - `available = current - reserved`
  - `real_surplus = max(available - safety_stock, 0)`
  - Crítico: `available <= safety_stock`
  - Normal: `available > safety_stock` e `real_surplus < surplus_threshold`
  - Excedente: `real_surplus >= surplus_threshold`
- Previsão futura (`production_forecasts`) separada do stock real.
- Previsão por produto/produtor funciona com unicidade funcional (update do mesmo registo quando existe 1).
- “Stock previsto” do comprador calculado em runtime via orders (não persistido em coluna).
- Needs (`needs`) como procura anunciada:
  - estados: `OPEN`, `PARTIALLY_COVERED`, `COVERED`, `IGNORED`;
  - cobertura conservadora:
    - `planned_qty`: itens `CONFIRMED/IN_DELIVERY/COMPLETED` com order elegível (`CONFIRMED/IN_PROGRESS/DELIVERING`);
    - `completed_qty`: apenas itens `COMPLETED`;
    - `PENDING` não conta para cobertura.
- Recalculo de need é idempotente e, quando há cobertura planeada/em curso, sincroniza projeção no stock do comprador com log:
  - `StockMovement.reference_type="NEED"` + `reference_id=<need.id>`;
  - notas explicam origem do ajuste por necessidade.

### 5.3 Marketplace
- `marketplace_listings` suporta 2 origens:
  - stock atual (`stock_id`);
  - pré-venda (`forecast_id`).
- Listings também podem estar ligadas a necessidade:
  - `need_id IS NULL` => anúncio público normal;
  - `need_id IS NOT NULL` => resposta privada dirigida à need.
- Regra XOR de origem aplicada no fluxo (stock XOR forecast).
- Publicação:
  - validações por origem;
  - lock de origem/produto quando vem do inventário em flows guiados;
  - no fluxo `from=need`, produto bloqueado e origem editável; anúncio é criado com `need_id`.
  - recorte de imagem no publish/edit;
  - tendência de preço por produto+origem (min/max/count de outros produtores).
- Estados de listing:
  - `ACTIVE`, `RESERVED`, `CLOSED`, `EXPIRED`, `CANCELLED`.
- URLs de foto resolvidas por storage (Cloudinary/local) via `default_storage.url(...)`.
- Visibilidade/autorização para respostas a need:
  - não aparecem no feed público (`tab=todos`) nem entram em recomendações;
  - aparecem em `tab=meus` para o criador;
  - aparecem em “Respostas recebidas” para o dono da need;
  - detalhe acessível apenas a criador e dono da need;
  - compra permitida apenas ao dono da need.
- UX atual da tab `necessidades`:
  - botão “Ver respostas” filtra por `need=<id>` com destaque visual da need selecionada;
  - para need `COVERED`, ação “Eliminar” faz soft delete via `IGNORED`;
  - formulário “Anunciar necessidade” abre apenas por botão (`show_need_form=1`).

### 5.4 Recommendations
- Wizard HTMX em 3 passos.
- Passo 1 mostra défice e quantidade atual com pré-seleção por query param quando vem do inventário.
- Passo 2 já considera listings de pré-venda.
- Priorização: recomenda primeiro “disponível agora”, depois pré-venda para completar défice.
- Badges distinguem disponibilidade imediata vs futura (data/período quando disponível).
- Motor de recomendações exclui respostas privadas a needs (`need_id IS NULL` obrigatório nas candidatas).

### 5.5 Orders
- Modelos: `order_groups`, `orders`, `order_items`, `order_status_history`.
- Criação:
  - por listing: cria grupo + sub-encomenda.
  - por recommendation: split por `(vendedor, origem)` dentro do mesmo grupo.
- Buyer vê grupo; seller trabalha por encomenda individual.
- Recálculo conservador de estado global + helper central para estado agregado do grupo.
- Workflow operacional:
  - `PENDING`: sem reservas;
  - `CONFIRMED`: reserva na origem correta (stock ou forecast);
  - `IN_PROGRESS` / `DELIVERING`: transições com guardas e idempotência;
  - `COMPLETED` (confirm_receipt): consome reserva, debita stock do vendedor e dá entrada no inventário do comprador.
- Integração com needs:
  - `order_items.need_id` é propagado no fluxo por listing e por recommendation;
  - recálculo de need ocorre em eventos de criação/transição/receção;
  - compra de listing privada (`listing.need_id`) só pode ser feita pelo produtor dono da need.
- Listings sincronizadas com reservas:
  - esgotado em reserva pode ir para `RESERVED`;
  - sem disponível e sem reservado fecha para `CLOSED`.
- Comando de reconciliação existe para corrigir `orders.status` sem mexer em stock/reservas.

### 5.6 Messaging
- Conversas 1:1 entre produtores.
- HTTP:
  - `/mensagens/` (inbox + thread);
  - start/reuse por listing;
  - upload de anexos.
- WebSocket:
  - `/ws/mensagens/<conversation_id>/`.
  - consumer resolve utilizador por `scope["user"]` com fallback `scope["session"]["user_id"]`.
- Mensagens:
  - `TEXT`, `SYSTEM_EVENT`, `FILE`;
  - anexos com `attachment_url`, `attachment_name`, `attachment_type`.
- Upload de anexos:
  - `default_storage.save(...)`;
  - valida extensão + MIME + tamanho 10MB.
- Unread sem N+1 (agregação na listagem).
- Delete de conversa:
  - one-sided com `is_archived=True`;
  - purge físico apenas quando ambos os participantes arquivarem.
- Header da thread abre detalhe do anúncio quando a conversa está ligada a listing.

## 6) Modelo de Dados (Entidades-Chave)
- Identidade: `users`, `producer_profiles`, `user_preferences`, `account_verification_tokens`.
- Catálogo global: `product_categories`, `products`.
- Inventário do produtor: `producer_products`, `stocks`, `stock_movements`, `production_forecasts`, `needs`.
- Marketplace: `marketplace_listings` (inclui `need_id` nullable para resposta privada).
- Recomendações: `recommendations`, `recommendation_items`.
- Encomendas: `order_groups`, `orders`, `order_items`, `order_status_history`.
- Mensagens: `conversations`, `conversation_participants`, `messages`.
- Suporte: `notifications`, `alerts`, `alert_events`, `audit_log`, `vision4farms_sync_log`.

## 7) Relações e Regras-Chave
- `users` 1-1 `producer_profiles`.
- `producer_profiles` N-N `products` via `producer_products`.
- `stocks` e `production_forecasts` são por `(producer, product)`.
- `marketplace_listings` referencia sempre uma origem operacional (stock ou forecast).
- `marketplace_listings.need_id` liga oferta privada ao dono de uma need.
- `order_items.need_id` e `recommendations.need_id` suportam rastreio de cobertura da need.
- `orders` pode ter `group_id` nulo (legado suportado).
- `messages` pertence a `conversation`; acesso só para participantes não arquivados.

## 8) Notas Operacionais
- Sem migrations em tabelas de negócio: alterações estruturais via SQL manual + map em `models.py`.
- Em produção com Cloudinary, render de media deve usar URL resolvida pela storage.
- Padrão de autenticação no projeto: usar `request.current_user` (não `request.user`) nas views de negócio.
- Atenção ao `.env`: `DEBUG` precisa de valor booleano parseável (`true/false`), não `"release"`.
