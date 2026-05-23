# VISION4FARMS - Contexto Atual do Projeto

Última revisão: 2026-05-23.

Este ficheiro serve como mapa funcional e técnico atual da aplicação. O objetivo é ajudar a explicar o projeto em relatório, manter o contexto entre sessões de desenvolvimento e evitar decisões baseadas em informação desatualizada.

## 1) Visão Geral

VISION4FARMS é uma plataforma B2B para produtores agrícolas. A aplicação ajuda produtores a gerir stock, anunciar excedentes, responder a necessidades de outros produtores, receber recomendações de compra/venda, acompanhar encomendas, comunicar por mensagens e pedir suporte.

Funcionalidades principais:

- gestão de conta, perfil de produtor e localização;
- catálogo global de produtos e categorias;
- inventário do produtor com stock atual, compromissos externos, previsões de produção e movimentos;
- marketplace de anúncios públicos de stock atual, pré-venda e procuras agregadas;
- necessidades/procuras entre produtores geradas manualmente ou a partir de pedidos externos de clientes;
- recomendações bidirecionais para comprar ou vender;
- encomendas com workflow operacional;
- alertas operacionais e notificações informativas;
- mensagens entre produtores;
- suporte conversacional com anexos;
- dashboard de produtor e consola de gestão/admin.

## 2) Stack e Infraestrutura

- Backend: Django 6.0.3.
- Base de dados: PostgreSQL.
- Templates: Django Templates.
- Interações parciais: HTMX.
- Realtime: Django Channels + Daphne + Redis.
- Storage de ficheiros/media: `default_storage`; em produção usa Cloudinary.
- Static files: WhiteNoise com `CompressedManifestStaticFilesStorage`.
- Gráficos: Chart.js via frontend.
- Email em produção: Resend via API HTTP usando `django-anymail`.
- Configuração: `python-decouple` e variáveis de ambiente.
- Fonte estrutural da BD: `sqlscript.sql`.
- A maioria dos modelos de negócio usa `managed = False`; alterações de schema são aplicadas manualmente na BD e refletidas nos models.

Branding atual:

- logos/favicons usam SVGs alojados no Cloudinary;
- variáveis suportadas:
  - `BRAND_LOGO_COLOR_URL`;
  - `BRAND_LOGO_WHITE_URL`;
  - `BRAND_LOGIN_LOGO_WHITE_URL`;
  - `BRAND_SIDEBAR_COMPACT_LOGO_URL`;
  - `BRAND_FAVICON_URL`;
- se as variáveis não forem definidas, `settings.py` usa defaults Cloudinary;
- a app já não depende de ficheiros estáticos `static/brand/*.svg`, evitando erros de manifest do WhiteNoise.

Email atual:

- `EMAIL_PROVIDER=resend` ativa `anymail.backends.resend.EmailBackend`;
- `RESEND_API_KEY` é obrigatória em produção;
- `DEFAULT_FROM_EMAIL` atual: `VISION4FARMS <onboarding@resend.dev>`;
- `DEFAULT_REPLY_TO_EMAIL` e `SUPPORT_CONTACT_EMAIL` apontam para o email de suporte da empresa;
- SMTP só é usado quando `EMAIL_PROVIDER=smtp`.

## 3) Estrutura de Apps

- `accounts`: autenticação, registo, confirmação, recuperação de password e convites.
- `catalog`: produtos e categorias globais.
- `inventory`: produtos do produtor, stock, movimentos e previsões.
- `marketplace`: anúncios públicos e listings técnicas.
- `needs`: necessidades e respostas a necessidades.
- `recommendations`: wizard de recomendações de compra/venda.
- `orders`: encomendas, itens, grupos e histórico de estados.
- `alerts`: alertas operacionais.
- `notifications_app`: notificações informativas recentes.
- `messaging`: conversas entre produtores.
- `support`: suporte cliente/admin.
- `settings_app`: definições do utilizador.
- `dashboard`: painel do produtor e consola admin.
- `common`: helpers transversais de sessão, permissões, auditoria, redirects, HTMX, contexto global e formatação.
- `integrations`: integrações externas/serviços auxiliares.

## 4) Layout Global e Navegação

Layout base da aplicação autenticada:

- sidebar com logo VISION4FARMS;
- topbar com perfil, avatar/iniciais e badge de alertas;
- navegação HTMX onde aplicável;
- menu do perfil com acesso a definições e logout.

Sidebar cliente, por ordem:

1. Painel;
2. Alertas;
3. Mensagens;
4. Recomendações;
5. Necessidades;
6. Marketplace;
7. Encomendas;
8. Stocks e Compras;
9. Suporte;
10. Definições.

Comportamento visual:

- quando não há foto de perfil, o avatar mostra as iniciais do primeiro e último nome;
- o sino da topbar só mostra ping vermelho quando existem alertas pendentes;
- o logo compacto da sidebar usa SVG Cloudinary, sem moldura/quadrado, e fica centrado no modo mobile/colapsado.

## 5) Accounts

Páginas principais:

- `/login/`;
- `/registo/`;
- `/logout/`;
- `/recuperar-password/`;
- `/recuperar-password/<uidb64>/<token>/`;
- fluxos de convite admin quando aplicável.

Regras atuais:

- login usa rate limit por IP e email;
- registo usa rate limit por IP;
- passwords usam validadores Django configurados;
- email de confirmação é enviado no registo;
- se o email de confirmação falhar, a conta continua criada quando aplicável e a UI orienta o utilizador a contactar suporte;
- admins podem confirmar manualmente contas pendentes;
- recuperação de password invalida tokens pendentes anteriores antes de emitir novo token;
- depois de resetar password, é enviado email de segurança de password alterada.

Email de segurança de password alterada:

- enviado quando a password muda nas definições;
- enviado quando a password muda por recuperação/reset;
- informa que, se o utilizador não realizou a alteração, deve contactar suporte;
- após alteração nas definições, a sessão é terminada e outras sessões são invalidadas.

## 6) Common

`apps.common` concentra infraestrutura transversal:

- decorators:
  - `login_required`;
  - `client_only_required`;
  - `admin_required`;
- sessão:
  - `resolve_active_session_user`;
  - valida utilizador ativo, conta ativa e fingerprint de sessão;
- auditoria:
  - `log_audit_event`;
  - `get_client_ip`;
  - `describe_user_agent`;
- redirects:
  - `get_safe_next_url` para validar `next`/`next_url`;
- HTMX:
  - helpers para triggers/toasts sem sobrescrever eventos existentes;
- formatação:
  - `format_quantity`;
  - filtro template `|quantity`;
- context processors:
  - foto/avatar;
  - badges de alertas, mensagens e suporte admin.

Formato de quantidades:

- inteiros aparecem sem casas decimais: `20 kg`;
- decimais preservam apenas o necessário: `20,5 kg`, `20,55 kg`, `20,345 kg`;
- evita strings como `20.000 kg` em templates, alertas, notificações, encomendas e marketplace.

## 7) Settings App

Página principal:

- `/definicoes/`.

Secções atuais:

- Identidade e perfil;
- Localização;
- Foto;
- Notificações;
- Segurança.

Identidade e perfil:

- existe apenas um local para editar nome;
- `User.first_name` e `User.last_name` são a identidade pública;
- `ProducerProfile.display_name` é sincronizado a partir do nome completo;
- email fica bloqueado para edição;
- perfil do produtor guarda empresa, telefone, NIF e tipo de utilizador.

Localização:

- morada, código postal, cidade, distrito, latitude e longitude;
- usada por clima, marketplace, logística e contexto de encomendas;
- botão de geolocalização automática;
- botão do card de clima aponta para esta área das definições.

Segurança:

- alteração de password termina sessão;
- invalida outras sessões;
- envia email de segurança;
- auditoria regista apenas metadados seguros, nunca hashes/segredos.

Suporte já não vive nas definições; existe página dedicada em `/suporte/`.

## 8) Dashboard Cliente

Página principal:

- `/painel/`;
- partial de clima: `/painel/weather-card/`.

O painel do produtor mostra:

- resumo operacional do produtor;
- ações úteis consoante o estado do inventário;
- card de clima simplificado;
- card operacional "Hoje na exploração";
- indicadores de encomendas, necessidades, anúncios e stock.

Inventário no painel:

- produtos críticos são apenas os que têm défice temporal real (`max_deficit > 0`);
- produção futura útil é considerada antes de mostrar risco;
- produtos com pedidos externos cobertos por stock atual + previsão disponível a tempo não contam como críticos;
- o painel usa a mesma fonte central do inventário: `calculate_inventory_commitment_state`.

Regras de UX:

- botões "Recomendações" e "Publicar excedente" só aparecem quando o produtor tem produtos ativos no inventário;
- "Atualizar stock"/inventário continua visível para permitir adicionar produtos;
- se não houver tarefas urgentes, o card lateral mostra estado simples.

Widget de clima:

- mostra localização, estado do tempo, temperatura mínima/máxima e chuva;
- se não houver localização, mostra CTA "Definir localização";
- mantém "Tentar novamente" para erros temporários.

## 9) Dashboard Admin

Páginas admin:

- `/gestor/`;
- `/gestor/produtos/`;
- `/gestor/categorias/`;
- `/gestor/utilizadores/`;
- `/gestor/utilizadores/<uuid>/`;
- `/gestor/auditoria/`;
- `/gestor/suporte/`;
- `/gestor/suporte/<uuid>/`.

Dashboard gestor:

- KPIs operacionais de utilizadores, suporte, encomendas e atividade;
- gráfico "Compras vs vendas por semana";
- card "Suporte e utilizadores" com totais de utilizadores, suspensos, online/offline e suporte ativo;
- informação relevante para administração da cooperativa.

Gráfico "Compras vs vendas por semana":

- cobre as últimas 12 semanas;
- compras = encomendas criadas por semana com origem `MARKETPLACE` ou `RECOMMENDATION`;
- vendas concluídas = itens de encomenda `COMPLETED` por semana nas mesmas origens;
- mostra estado vazio quando não existem dados no período.

Gestão de utilizadores:

- listagem clicável;
- detalhe com dados do utilizador;
- confirmar email manualmente;
- suspender/reativar utilizador;
- atividade relacionada com alterações de conta, login, password, suporte e ações admin.

Auditoria:

- pesquisa por ação, label, utilizador, email, IP, User-Agent e JSON;
- linhas clicáveis/expansíveis;
- paginação 10/25/50;
- texto de paginação dentro de `.adm-pagination`.

## 10) Catalog

Entidades:

- `ProductCategory` -> `product_categories`;
- `Product` -> `products`.

Unidade operacional:

- todos os produtos usam `kg`;
- admin/produtor não escolhem unidade;
- descrições podem conter detalhes comerciais como "5 caixas de 20kg";
- `products.unit` continua na BD por compatibilidade, com default `kg`.

Páginas admin:

- `/gestor/produtos/`;
- `/gestor/produtos/novo/`;
- `/gestor/produtos/<uuid>/editar/`;
- `/gestor/categorias/`;
- `/gestor/categorias/nova/`;
- `/gestor/categorias/<uuid>/editar/`.

Categorias:

- produtos só listam categorias ativas em criação;
- produto existente com categoria inativa continua editável;
- categoria pode ser eliminada se não estiver em uso ativo por inventário de produtores;
- produtos sem uso ativo podem ficar sem categoria quando a categoria é removida.

Serviços:

- normalização de nomes/slugs;
- criação/edição de produtos e categorias;
- snapshots para auditoria;
- criação/reutilização de produto global quando produtor adiciona produto personalizado ao inventário.

## 11) Inventory

Páginas principais:

- `/inventario/produtos/?tab=stock`;
- `/inventario/produtos/?tab=compras`;
- `/inventario/produtos/adicionar/`;
- `/inventario/stock/<uuid>/`.

Produtos do produtor:

- associação entre produtor e produto global vive em `producer_products`;
- descrição específica do produtor vive em `producer_products.producer_description`;
- adicionar produto pode reutilizar produto global existente ou criar produto global novo via `catalog`.

Stock:

- `current_quantity`: quantidade real em stock;
- `reserved_quantity`: quantidade reservada em encomendas/listings;
- `available = current_quantity - reserved_quantity`;
- `safety_stock`: campo técnico mantido por compatibilidade, atualmente sincronizado com a soma dos pedidos externos em aberto do produtor/produto;
- na UI este valor aparece como "Compromissos externos" ou "Reservado para clientes externos";
- quantidade vendável/publicável é calculada por margem temporal quando existem pedidos externos, não apenas por `available - safety_stock`.

Estado de stock:

- fonte central: `calculate_inventory_commitment_state(producer, product)`;
- quando existem pedidos externos, o estado usa `calculate_external_demand_plan`;
- crítico: existe `max_deficit > 0`, ou seja, stock atual + produção prevista útil não chegam a tempo dos pedidos externos;
- atenção/coberto no limite: não há défice, mas a margem temporal disponível é curta;
- coberto/excedente: pedidos externos estão cobertos e existe margem temporal vendável;
- sem pedidos externos ativos, o fallback considera apenas stock disponível atual e não cria estado crítico por compromissos inexistentes.

Detalhe de stock:

- card de saúde mostra cobertura temporal dos compromissos externos;
- mostra stock disponível, compromissos externos, produção prevista útil, défice temporal e margem vendável;
- progress bars mostram cobertura, reservado, quantidade vendável temporal e défice real;
- movimentos recentes mostram histórico vindo de `stock_movements`;
- produções futuras podem ser assumidas no stock quando chegam ao período definido.

Produções futuras:

- vivem em `production_forecasts`;
- são separadas do stock real até serem assumidas;
- contam para cobertura de pedidos externos apenas quando estão disponíveis a tempo:
  - `period_end <= requested_delivery_date`;
  - se não houver `period_end`, usa `period_start <= requested_delivery_date`;
  - forecast sem data válida não conta;
  - quantidade útil = `forecast_quantity - reserved_quantity`;
- ao assumir previsão:
  - quantidade passa para stock atual;
  - movimento é registado em `stock_movements`;
  - listings abertas associadas à previsão são fechadas/ajustadas.

Compras, vendas e produção:

- `/inventario/produtos/?tab=compras` é o painel "Compras e Vendas";
- filtros por período mensal/anual, ano e mês;
- compras concluídas usam `Order.total_amount` do produtor comprador;
- vendas concluídas usam `OrderItem.subtotal` do produtor vendedor;
- produção/entradas usa movimentos positivos em `StockMovement`;
- inclui métricas, rankings, históricos e gráficos Chart.js.

## 12) Marketplace

Páginas:

- `/marketplace/`;
- `/marketplace/publicar/`;
- `/marketplace/<uuid>/`;
- rota própria para detalhe de anúncio do próprio produtor;
- rotas de edição/eliminação/gestão de anúncio próprio.

Feed público:

- mostra anúncios/ofertas de outros produtores e procuras agregadas de outros produtores;
- não mostra anúncios do próprio produtor;
- não mostra procuras do próprio produtor no feed geral;
- filtros: pesquisa, categoria, origem, ordenação e "apenas disponíveis";
- cards modernos com imagem, origem, estado, produtor, localização, quantidade, preço/kg, entrega e CTAs.

Tab "Meus anúncios":

- mostra anúncios do produtor autenticado;
- mantém ações de gestão;
- não usa badge "meu anúncio" no feed público porque anúncios próprios já não aparecem ali.

Tipos de anúncio:

- stock atual (`stock_id`);
- pré-venda (`forecast_id`);
- resposta privada a necessidade (`need_id`).

Procuras no marketplace:

- vêm de `needs`, não de `marketplace_listings`;
- são cards de procura visualmente distintos das ofertas;
- aparecem quando a necessidade está `OPEN` ou `PARTIALLY_COVERED` e ainda existe quantidade por planear;
- CTA principal abre `/necessidades/responder/?need=<id>&product=<id>`;
- não existe página pública própria de detalhe da necessidade nesta fase.

Regras:

- anúncios públicos têm `need_id IS NULL`;
- respostas a necessidades têm `need_id IS NOT NULL` e não aparecem no feed público;
- publicar excedente usa quantidade vendável temporal calculada pelo inventário;
- editar anúncio fechado é bloqueado;
- eliminar fisicamente anúncio referenciado por recomendação/encomenda pode falhar por FK, por isso o fluxo deve preferir fechar/cancelar em vez de apagar quando já existe histórico relacionado.

Detalhe de anúncio:

- visual centralizado e mais largo;
- card principal e buybox alinhados;
- buybox tem botões de incremento/decremento e botão `Máx.` para comprar toda a quantidade disponível;
- CTAs diferem entre anúncio próprio e anúncio de outro produtor.

## 13) Needs

Páginas:

- `/necessidades/`;
- `/necessidades/pedidos-clientes/`;
- `/necessidades/pedidos-clientes/criar/`;
- `/necessidades/pedidos-clientes/<uuid>/editar/`;
- `/necessidades/pedidos-clientes/<uuid>/cancelar/`;
- `/necessidades/criar/`;
- `/necessidades/responder/`;
- `/necessidades/<uuid>/ignorar/`;
- `/necessidades/<uuid>/editar/`;
- `/necessidades/respostas/<listing_id>/`;
- `/necessidades/respostas/<listing_id>/editar/`;
- `/necessidades/respostas/<listing_id>/rejeitar/`.

Estados da necessidade:

- `OPEN`;
- `PARTIALLY_COVERED`;
- `COVERED`;
- `IGNORED`;
- `CANCELLED`.

Página `/necessidades/`:

- layout master-detail;
- "As minhas necessidades";
- detalhe da necessidade selecionada;
- ofertas ativas;
- responder a necessidades abertas;
- históricos colapsáveis:
  - "Histórico de ofertas recebidas";
  - "Ofertas enviadas".

Criação:

- `create_need` cria uma nova necessidade;
- se já existir necessidade ativa do mesmo produtor/produto, devolve erro claro e link para a necessidade existente;
- criação deixou de atualizar silenciosamente necessidades existentes.

Edição:

- dono pode editar quantidade, data limite e observações;
- produto/produtor/origem não são editáveis;
- quantidade pode aumentar;
- quantidade só pode reduzir até ao maior valor entre quantidade planeada e concluída;
- aumentar uma necessidade coberta pode reabri-la.

Cobertura:

- `calculate_need_coverage` calcula quantidade necessária, planeada, concluída, em falta para planear e em falta para receber;
- encomendas concluídas contam como recebido;
- encomendas em entrega/confirmadas contam como planeado;
- pendentes/canceladas não contam como cobertura efetiva.

GETs:

- views de necessidades não fazem alterações persistentes;
- expiração persistida de listings fica para comando agendado;
- leituras tratam listings expiradas como expiradas em memória.

Pedidos externos de clientes:

- implementados em `external_customer_demands`;
- modelo `ExternalCustomerDemand` é `managed=False`;
- pedidos têm cliente, contacto, referência, produto, quantidade, data pretendida de entrega, estado e notas;
- `requested_delivery_date` é `DATE`, não `TIMESTAMPTZ`;
- `requested_quantity` tem de ser superior a zero;
- CRUD simples disponível em `/necessidades/pedidos-clientes/`;
- cancelar pedido faz soft cancel com estado `CANCELLED`;
- cálculo temporal por produto/data:
  - compara pedidos acumulados até cada data com stock disponível atual e produção prevista útil até essa data;
  - forecast conta se `period_end <= requested_delivery_date`; se não houver `period_end`, conta por `period_start`; forecasts sem data válida não contam;
  - mostra maior défice e primeira data crítica;
- quando existe défice temporal, gera ou atualiza uma `Need` agregada com `source_system=CUSTOMER_DEMAND`;
- `required_quantity` da need automática é o maior défice temporal;
- `needed_by_date` da need automática é a primeira data crítica;
- se o défice desaparecer, a need automática passa para `COVERED`;
- pedidos externos ativos recebem `generated_need_id` da need agregada;
- pedidos externos individuais não são fechados automaticamente na v1;
- `stocks.safety_stock` é sincronizado com a soma dos pedidos externos em aberto do produtor/produto.

Need automática por pedidos externos:

- continua a ser uma `Need`, não uma `MarketplaceListing`;
- não pode ser editada diretamente pelo endpoint de edição de needs;
- para alterar quantidade/data, o produtor deve editar os pedidos externos de origem;
- evita duplicar necessidades ativas para o mesmo produtor/produto.

## 14) Respostas a Necessidades

Uma resposta a necessidade é tecnicamente uma `MarketplaceListing` privada com `need_id`.

Estados da proposta (`need_response_status`):

- `PENDING`;
- `ACCEPTED`;
- `REJECTED`;
- `CANCELLED`;
- `COMPLETED`;
- `WITHDRAWN`;
- `EXPIRED`.

Fluxo do produtor que responde:

- abre `/necessidades/responder/?need=<id>&product=<id>`;
- vê card "O seu inventário" com stock atual, reservado, disponível, compromissos externos, produção futura e máximo publicável;
- envia proposta com quantidade, preço, entrega e observações;
- se já existir proposta pendente para essa necessidade, edita a proposta existente;
- só pode criar nova proposta depois de a anterior deixar de estar pendente.

Fluxo do dono da necessidade:

- vê ofertas recebidas;
- abre detalhe em `/necessidades/respostas/<listing_id>/`;
- pode comprar/aceitar proposta pendente;
- pode rejeitar proposta pendente;
- não pode comprar proposta rejeitada/cancelada/concluída.

Quantidades:

- `offered_quantity`: quantidade originalmente proposta;
- `available_quantity`: quantidade ainda comprável;
- `ordered_quantity`: quantidade associada a encomenda;
- histórico mostra a quantidade correta conforme o estado.

## 15) Recommendations

Página:

- `/recomendacoes/`.

Objetivo:

- orientar o produtor a decidir se deve comprar para repor stock ou vender excedente.

Passo 1:

- mostra duas tabelas:
  - produtos a comprar, quando existe défice temporal para cumprir pedidos externos;
  - produtos a vender, quando existe margem temporal acima dos compromissos externos;
- o card "Ação selecionada" fica centrado e resume o produto/direção/quantidade.

Métricas:

- usa `calculate_inventory_commitment_state`;
- `buy_quantity = max_deficit`;
- `sell_quantity = temporal_sellable_quantity`;
- quando não existem pedidos externos ativos, usa fallback seguro baseado no stock disponível atual;
- não recomenda compra quando a produção prevista útil cobre os pedidos externos a tempo.

Compra:

- procura anúncios públicos compatíveis;
- gera combinação de ofertas;
- permite criar necessidade se não houver cobertura suficiente;
- permite aceitar recomendação e criar encomenda.

Venda:

- mostra excedente recomendado;
- lista necessidades abertas compatíveis;
- permite responder à necessidade;
- permite publicar excedente no marketplace com quantidade pré-preenchida.

## 16) Orders

Páginas:

- `/encomendas/`;
- `/encomendas/<uuid>/`.

Entidades:

- `order_groups`;
- `orders`;
- `order_items`;
- `order_status_history`.

Fluxos:

- compra direta de anúncio;
- compra a partir de recomendação;
- compra/aceitação de resposta a necessidade.

Workflow:

- `PENDING`;
- `CONFIRMED`;
- `IN_PROGRESS`;
- `DELIVERING`;
- `COMPLETED`;
- `CANCELLED`.

Regras:

- buyer vê grupo/encomenda;
- seller trabalha cada encomenda individual;
- pendente reserva quantidade;
- cancelamento liberta reserva;
- receção/conclusão consome reserva e movimenta stock;
- respostas a necessidades atualizam `need_response_status` e recalculam cobertura da necessidade.

Detalhe:

- layout mais limpo;
- ação "Aceitar pedido" posicionada na área de decisão;
- cancelamento tem motivo e notas alinhados visualmente.

## 17) Alerts

Página:

- `/alertas/`.

Objetivo:

- mostrar alertas operacionais que exigem atenção ou acompanhamento.

Tabs:

- ativos;
- ignorados/adiados;
- resolvidos.

Filtros:

- tipo;
- categoria;
- pesquisa;
- apenas alertas que exigem ação.

Secções de alertas ativos:

- "A fazer agora";
- "Risco agrícola";
- "Oportunidades";
- "Informação".

Alertas geridos:

- stock crítico temporal;
- excedente/margem vendável temporal;
- necessidade por cobrir;
- resposta recebida;
- prazo de necessidade a aproximar;
- oportunidade de compra;
- sugestão de venda;
- encomenda a exigir confirmação;
- entrega em atraso;
- anúncio a expirar.

Lógica de stock em alertas:

- usa `calculate_inventory_commitment_state`;
- alerta crítico só é criado quando existe `max_deficit > 0`;
- produção futura útil é considerada antes de gerar alerta;
- texto indica quantidade em falta e primeira data crítica;
- alertas de excedente usam a margem temporal vendável, evitando sugerir venda de stock necessário para pedidos futuros.

Ações:

- resolver;
- adiar/lembrar mais tarde;
- adiar todos/visíveis;
- filtros preservados em ações HTMX.

Realtime:

- badge via Channels/Redis quando disponível;
- se Redis falhar localmente, a página continua a funcionar via HTTP/polling.

## 18) Notifications App

Objetivo:

- guardar notificações informativas recentes, separadas de alertas operacionais.

Exemplos:

- mensagens não lidas;
- mudanças informativas de encomendas;
- notificações derivadas de alertas.

Na página `/alertas/`:

- mostra últimas 6 notificações recentes;
- botão "Limpar notificações" remove apenas notificações do utilizador atual;
- limpar notificações não resolve, ignora nem apaga alertas reais.

Deduplicação:

- `create_alert_notification` atualiza notificação existente do mesmo `user + alert`;
- evita repetições como várias notificações "Oportunidade para cobrir Alface" para o mesmo alerta.

## 19) Messaging

Página:

- `/mensagens/`.

Funcionalidades:

- conversas 1:1 entre produtores;
- iniciar/reutilizar conversa por listing;
- iniciar/reutilizar conversa por encomenda;
- mensagens de texto;
- eventos de sistema;
- anexos.
- código separado por responsabilidade:
  - `apps.messaging.attachments`;
  - `apps.messaging.conversations`;
  - `apps.messaging.messages`;
  - `apps.messaging.unread`;
  - `apps.messaging.notifications`;
  - `apps.messaging.services` mantém fachada compatível para imports existentes.

Realtime:

- WebSocket por conversa;
- origem dos WebSockets validada contra `ALLOWED_HOSTS`;
- indicador `Online/Offline` mostra estado da ligação WebSocket do utilizador atual à conversa, não presença real do outro utilizador.

Anexos:

- endpoint HTTP de upload;
- usa `default_storage`, Cloudinary em produção;
- valida extensão, MIME e limite 10MB;
- imagens são mostradas como thumbnails clicáveis;
- ficheiros não-imagem mantêm card de ficheiro.
- se o anexo for guardado mas o broadcast realtime falhar, o envio continua válido e a UI mostra aviso em vez de erro 500.

Mobile:

- layout master-detail;
- botão "Conversas" volta à lista;
- composer com auto-resize;
- erros aparecem em toast visual.
- histórico da thread carrega as mensagens recentes e permite "Carregar mensagens anteriores" quando há mais histórico.
- anexos mostram estado por ficheiro: pronto, a enviar, enviado ou falhou.

Regras de segurança/abuso:

- mensagens de texto têm limite server-side de 2000 caracteres;
- envio de mensagens/anexos tem rate limit por utilizador/conversa;
- contacto por anúncio só pode ser iniciado para anúncios públicos, ativos, com quantidade disponível e sem `need_id`;
- respostas privadas a necessidades continuam a usar as páginas de `needs`, não o contacto normal de marketplace.
- abrir `/mensagens/` sem conversa explícita não marca automaticamente a primeira conversa como lida;
- quando uma mensagem nova chega a uma conversa arquivada, a conversa volta para "Ativas" para os destinatários;
- notificações recentes de mensagens são deduplicadas por conversa e marcadas como lidas quando a conversa é aberta.
- pós-envio de mensagem passa por helper único: atualiza conversa, marca remetente como lido, cria/atualiza notificação e emite WebSocket.

## 20) Support

Páginas cliente:

- `/suporte/`;
- `/suporte/<uuid>/`;
- `/suporte/tickets/` para criação.

Páginas admin:

- `/gestor/suporte/`;
- `/gestor/suporte/<uuid>/`.

Estados:

- `OPEN`;
- `CLAIMED`;
- `CLOSED`.

Cliente:

- cria ticket com mensagem e imagens opcionais;
- vê histórico em cards;
- consulta FAQ funcional;
- abre conversa do ticket;
- responde enquanto ticket não estiver fechado.

Admin:

- vê fila de tickets;
- linhas da tabela são clicáveis;
- pode assumir ticket;
- responde sem fechar automaticamente;
- fecha explicitamente com "Marcar como resolvido";
- pode continuar a conversa até fechar.

Suporte conversacional:

- `support_ticket_messages` guarda cada mensagem;
- `support_ticket_attachments` guarda imagens por mensagem;
- `support_tickets.last_message_at` e `last_message_by_role` alimentam ordenação e badge admin.

Anexos:

- apenas imagens `.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`;
- limite 10MB por imagem;
- usa Cloudinary em produção via `default_storage`.

FAQ atual:

- explica recuperação de conta;
- problemas em encomendas;
- ajuda sobre marketplace;
- ajuda sobre necessidades;
- interpretação de stock atual, reservado, disponível, compromissos externos, produção futura e máximo publicável.

## 21) Notifications, Badges e Realtime

Badges principais:

- alertas pendentes do produtor;
- mensagens não lidas;
- suporte admin ativo.

Redis/Channels:

- usado para updates realtime;
- falha de Redis não deve impedir carregamento das páginas;
- dados continuam disponíveis por render HTTP/context processors.

## 22) Auditoria

Tabela:

- `audit_log`.

Eventos relevantes:

- login;
- alterações de conta;
- alterações de perfil de produtor;
- preferências;
- foto;
- password;
- reset de password;
- ações admin;
- suporte;
- alterações de catálogo.

Regras:

- não guardar segredos;
- guardar IP, user-agent e descrição de dispositivo;
- guardar snapshots `old_values`/`new_values` quando útil;
- falhas de auditoria não devem quebrar ações principais já concluídas.

## 23) Modelo de Dados Atual

Identidade:

- `users`;
- `producer_profiles`;
- `user_preferences`;
- `account_verification_tokens`.

Catálogo:

- `product_categories`;
- `products`.

Inventário:

- `producer_products`;
- `stocks`;
- `stock_movements`;
- `production_forecasts`.

Marketplace e necessidades:

- `external_customer_demands`;
- `marketplace_listings`;
- `needs`;
- `recommendations`;
- `recommendation_items`.

Encomendas:

- `order_groups`;
- `orders`;
- `order_items`;
- `order_status_history`.

Comunicação:

- `conversations`;
- `conversation_participants`;
- `messages`.

Alertas/notificações:

- `alerts`;
- `alert_events`;
- `alert_deliveries`;
- `notifications`.

Suporte:

- `support_tickets`;
- `support_ticket_messages`;
- `support_ticket_attachments`.

Operacional:

- `audit_log`;
- `vision4farms_sync_log`.

## 24) Relações-Chave

- `users` 1-1 `producer_profiles`.
- `producer_profiles` N-N `products` via `producer_products`.
- `stocks` e `production_forecasts` pertencem a produtor/produto.
- `external_customer_demands` pertencem a produtor/produto e podem apontar para uma need agregada em `generated_need_id`.
- `marketplace_listings.stock_id` aponta para stock atual.
- `marketplace_listings.forecast_id` aponta para pré-venda.
- `marketplace_listings.need_id` transforma a listing em resposta privada a necessidade.
- `marketplace_listings.need_response_status` controla o estado explícito da proposta.
- `needs.producer_id` aponta para o produtor que precisa do produto.
- `needs.source_system=CUSTOMER_DEMAND` identifica procura gerada por pedidos externos de clientes.
- `order_items.need_id` permite recalcular cobertura da necessidade.
- `recommendations.need_id` liga recomendação a necessidade criada/atualizada.
- `messages` pertencem a `conversation`.
- `support_ticket_messages.ticket_id` pertence a `support_tickets`.
- `support_ticket_attachments.message_id` pertence a `support_ticket_messages`.

## 25) Operação em Produção

Variáveis importantes:

- `DEBUG=false`;
- `SECRET_KEY`;
- `ALLOWED_HOSTS`;
- `CSRF_TRUSTED_ORIGINS`;
- `DATABASE_URL` ou variáveis `DB_*`;
- `REDIS_URL`;
- `EMAIL_PROVIDER=resend`;
- `RESEND_API_KEY`;
- `DEFAULT_FROM_EMAIL`;
- `DEFAULT_REPLY_TO_EMAIL`;
- `SUPPORT_CONTACT_EMAIL`;
- `CLOUDINARY_CLOUD_NAME`;
- `CLOUDINARY_API_KEY`;
- `CLOUDINARY_API_SECRET`;
- `BRAND_LOGO_COLOR_URL`;
- `BRAND_LOGO_WHITE_URL`;
- `BRAND_LOGIN_LOGO_WHITE_URL`;
- `BRAND_SIDEBAR_COMPACT_LOGO_URL`;
- `BRAND_FAVICON_URL`.

Comandos/tarefas:

- `python manage.py check` para validação;
- `python manage.py sync_operational_alerts --apply` para sincronização operacional de alertas/listings expiradas;
- Railway Scheduler/cron deve executar a sincronização operacional.

Cuidados:

- `DEBUG` tem de ser booleano (`true`/`false`);
- `DEBUG=release` é inválido;
- `sqlscript.sql` representa o schema consolidado atual;
- como os modelos de negócio são `managed=False`, alterações de schema não são aplicadas por migrations Django.
