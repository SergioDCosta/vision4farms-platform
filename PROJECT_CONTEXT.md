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
  - `django-ratelimit` em submissão de tickets de suporte (`5/30m`, chave por user/IP);
  - validação de anexos por extensão + MIME (`content_type`) + limite 10MB.
  - `SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"` para compatibilidade com tiles de mapas cross-origin.

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
  - `support`: ticketing de suporte utilizador->admin (criação, claim, resposta/fecho).
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
- Card "Produção futura" (detalhe de stock):
  - `Reservada` reflete `production_forecasts.reserved_quantity`;
  - `Disponível pré-venda` reflete quantidade ainda disponível nos anúncios do marketplace associados à previsão
    (`sum(listing.quantity_available)` para listings `ACTIVE/RESERVED` dessa previsão).
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
- Assimilação de previsão para stock atual:
  - usa a quantidade disponível pré-venda (do(s) anúncio(s) aberto(s) da previsão), não o `forecast_quantity` bruto;
  - fecha listings abertas associadas (`quantity_available=0`, `status=CLOSED`);
  - mantém a previsão (não faz delete), reduzindo `forecast_quantity` pelo valor assimilado;
  - se a previsão ficar sem quantidade, desativa `is_marketplace_enabled`.

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
- Classificação de origem:
  - helper central `forecast-only` para identificar pré-venda pura (sem mistura com stock).
- Workflow operacional:
  - `PENDING`: já reserva quantidade na origem correta (stock ou forecast) no momento da criação da encomenda;
  - `CONFIRMED`: não volta a reservar; apenas avança estado dos items;
  - `IN_PROGRESS` / `DELIVERING`: transições com guardas e idempotência;
  - `COMPLETED` (confirm_receipt): consome reserva, debita stock do vendedor e dá entrada no inventário do comprador.
- Reconciliação de reservas por listing:
  - reserva efetiva é reconciliada por soma de `order_items` abertos (`PENDING/CONFIRMED/IN_DELIVERY`);
  - evita dupla-reserva e garante release correto também em cancelamento de pendentes.
- Integração com needs:
  - `order_items.need_id` é propagado no fluxo por listing e por recommendation;
  - recálculo de need ocorre em eventos de criação/transição/receção;
  - compra de listing privada (`listing.need_id`) só pode ser feita pelo produtor dono da need.
- Listings sincronizadas com reservas:
  - esgotado em reserva pode ir para `RESERVED`;
  - sem disponível e sem reservado fecha para `CLOSED`.
- UX de encomendas para pré-venda:
  - nova aba dedicada `tab=pre_vendas` com duas colunas: "Como comprador" e "Como vendedor";
  - `compras` e `recebidas` excluem encomendas `forecast-only` para evitar duplicação visual;
  - criar encomenda por anúncio forecast redireciona para `tab=pre_vendas`;
  - no fluxo de recommendation, se todas as sub-encomendas criadas forem `forecast-only`, redireciona para `tab=pre_vendas`.
- Comando de reconciliação existe para corrigir `orders.status` sem mexer em stock/reservas.

### 5.6 Messaging
- Conversas 1:1 entre produtores.
- HTTP:
  - `/mensagens/` (inbox + thread);
  - start/reuse por listing;
  - start/reuse por encomenda (`ORDER_CONTACT`) em `/mensagens/encomenda/<uuid>/iniciar/`;
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
- Conversa `ORDER_CONTACT`:
  - criada lazy no primeiro clique em detalhe de encomenda de pré-venda;
  - reutilizada nos cliques seguintes (1:1 comprador-vendedor por encomenda).

### 5.7 Alerts
- Página dedicada `/alertas/` com tabs: `active`, `ignored`, `resolved`.
- Sincronização automática no load (`sync_alerts_for_producer`) para gerar/atualizar/resolver alertas geridos.
- Tipos geridos atualmente:
  - `CRITICAL_STOCK` (stock disponível <= safety stock);
  - `SURPLUS_AVAILABLE` (excedente real >= limiar);
  - `EXTERNAL_DEFICIT` (need em aberto/parcial sem cobertura suficiente);
  - `SELL_SUGGESTION` (previsão com quantidade vendável).
- Tipos orientados a evento (fora do ciclo de sync gerido):
  - encomendas: `ORDER_PURCHASE_CREATED`, `ORDER_CONFIRMED`, `ORDER_IN_PROGRESS`,
    `ORDER_DELIVERING`, `ORDER_CANCELLED`, `ORDER_COMPLETED`;
  - mensagens: `MESSAGE_UNREAD` (upsert por conversa).
- Motor inclui deduplicação por contexto lógico (`product`, `need`, `forecast`, `listing`) e auto-resolução de duplicados.
- Alertas ignorados não são recriados enquanto a condição persiste e o alerta ignorado não tiver sido limpo.
- Regra de expiração de ignorados:
  - `IGNORED` expira ao fim de 30 minutos e transita para `CLEARED` (limpeza lazy ao entrar/interagir em `/alertas/`).
  - `CLEARED` não tem tab dedicada na UI.
- Semântica de `RESOLVED` em alertas geridos (`CRITICAL_STOCK`, `SURPLUS_AVAILABLE`, `EXTERNAL_DEFICIT`, `SELL_SUGGESTION`):
  - resolução manual define `status=RESOLVED` com `cleared_at=NULL` para suprimir reabertura enquanto a condição persistir;
  - quando a condição deixa de existir, o `sync_alerts_for_producer` preenche `cleared_at` e regista evento `CLEARED`;
  - se a condição voltar depois disso, pode ser criado novo alerta `ACTIVE`.
- Ações utilizador:
  - ignorar (`/alertas/<id>/ignorar/`);
  - ignorar todos os ativos (`/alertas/ignorar-todos/`);
  - reativar ignorado (`/alertas/<id>/reativar/`);
  - resolver (`/alertas/<id>/resolver/`).
- Eventos em `alert_events` são registados para criação/ignorar/resolução/limpeza (`CLEARED`).
- Dashboard cliente consome `alerts` para KPI de ativos/críticos e lista de prioritários.

### 5.8 Support
- Nova app `support` com tabela `support_tickets` (SQL manual), modelo `managed=False`.
- Estados finais do ticket:
  - `OPEN`;
  - `CLAIMED`;
  - `CLOSED`.
- Campos operacionais:
  - `ticket_number` sequencial (sequence BD);
  - snapshots do requerente no momento da criação;
  - `claimed_at`, `admin_replied_at`, `closed_at`.
- Fluxo utilizador:
  - card “Contactar suporte” em Definições;
  - submit para rota dedicada `POST /suporte/tickets/`;
  - persistência do ticket acontece antes de tentar emails;
  - falha de email não faz rollback do ticket.
- Fluxo admin:
  - fila em `/gestor/suporte/` + detalhe em `/gestor/suporte/<uuid>/`;
  - claim por `POST /gestor/suporte/<uuid>/claim/` com lock transacional;
  - resposta por `POST /gestor/suporte/<uuid>/reply/` (fecha automaticamente na 1.ª resposta).
- Badge realtime na sidebar admin:
  - WebSocket dedicado em `/ws/suporte/sidebar/` (Channels);
  - consumer de suporte adiciona admins ao grupo `support_admin_badge`;
  - eventos emitidos em criação/claim/fecho de ticket fazem refresh imediato do estado do badge;
  - polling contínuo removido do frontend (mantido refresh por navegação HTMX + evento realtime).
- Auditoria obrigatória implementada:
  - `SUPPORT_TICKET_CREATED`
  - `SUPPORT_TICKET_CLAIMED`
  - `SUPPORT_TICKET_REPLIED`
  - `SUPPORT_TICKET_CLOSED`
- Histórico em Definições e fila admin expõem `claimed_at`.
- Nota técnica de concorrência:
  - em operações com `select_for_update`, evitar `select_related` em FKs nullable (ex.: `assigned_admin`) para não disparar erro PostgreSQL em outer join com lock.

### 5.9 Marketplace (Entrega + Mapa no detalhe)
- Formulários publish/edit:
  - quando `delivery_mode = PICKUP`, campos de `delivery_radius_km` e `delivery_fee` ficam ocultos no frontend;
  - backend mantém limpeza defensiva de raio/taxa em pickup.
  - novo toggle por anúncio `show_location_on_map` para o produtor escolher se quer expor localização no mapa.
- Detalhe do anúncio:
  - mapa Leaflet com marcador da exploração (modo `exact`, quando coordenadas válidas);
  - fallback `approximate` por `cidade/distrito` via geocoding no frontend (Nominatim), com aviso de que a localização exata não foi divulgada;
  - modo `hidden` quando o produtor desativa a exibição do mapa no anúncio;
  - mensagem de indisponibilidade apenas quando não há coordenadas nem cidade/distrito;
  - círculo de entrega quando há raio no anúncio;
  - popup no marcador com produtor + produto do anúncio;
  - marcador adicional do comprador (verde), quando coordenadas do comprador estão disponíveis no contexto;
  - círculo de entrega também interativo (popup com raio).
- Robustez frontend mapa:
  - guard para evitar double-init em navegação HTMX (`_leaflet_id`);
  - carregamento robusto do Leaflet no detalhe (inclui carregamento dinâmico do script quando necessário);
  - reflow pós-swap HTMX (`htmx:afterSettle`) para evitar mapa cinza no primeiro load;
  - ajuste de viewport com `invalidateSize` + `requestAnimationFrame`;
  - fallback de provider de tiles em caso de erro.

## 6) Modelo de Dados (Entidades-Chave)
- Identidade: `users`, `producer_profiles`, `user_preferences`, `account_verification_tokens`.
- Catálogo global: `product_categories`, `products`.
- Inventário do produtor: `producer_products`, `stocks`, `stock_movements`, `production_forecasts`, `needs`.
- Marketplace: `marketplace_listings` (inclui `need_id` nullable para resposta privada e `show_location_on_map` para controlo de privacidade no mapa).
- Recomendações: `recommendations`, `recommendation_items`.
- Encomendas: `order_groups`, `orders`, `order_items`, `order_status_history`.
- Mensagens: `conversations`, `conversation_participants`, `messages`.
- Alertas: `alerts`, `alert_events`.
- Suporte: `support_tickets`.
- Operacional/integrações: `notifications`, `audit_log`, `vision4farms_sync_log`.

## 7) Relações e Regras-Chave
- `users` 1-1 `producer_profiles`.
- `producer_profiles` N-N `products` via `producer_products`.
- `stocks` e `production_forecasts` são por `(producer, product)`.
- `marketplace_listings` referencia sempre uma origem operacional (stock ou forecast).
- Constraint DB `marketplace_listings_source_xor_chk` impõe XOR estrito:
  - `(stock_id IS NOT NULL AND forecast_id IS NULL) OR (stock_id IS NULL AND forecast_id IS NOT NULL)`.
- `marketplace_listings.need_id` liga oferta privada ao dono de uma need.
- `order_items.need_id` e `recommendations.need_id` suportam rastreio de cobertura da need.
- `orders` pode ter `group_id` nulo (legado suportado).
- `messages` pertence a `conversation`; acesso só para participantes não arquivados.
- `support_tickets.requester_user_id` referencia `users` (cascade delete).
- `support_tickets.assigned_admin_id` referencia `users` (set null).

## 8) Notas Operacionais
- Sem migrations em tabelas de negócio: alterações estruturais via SQL manual + map em `models.py`.
- Em produção com Cloudinary, render de media deve usar URL resolvida pela storage.
- Padrão de autenticação no projeto: usar `request.current_user` (não `request.user`) nas views de negócio.
- Atenção ao `.env`: `DEBUG` precisa de valor booleano parseável (`true/false`), não `"release"`.
- Em produção, configurar `SUPPORT_CONTACT_EMAIL` como fallback quando não houver admins ativos com email para notificação de novo ticket.
