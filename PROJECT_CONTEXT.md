# VISION4FARMS - Contexto Atual do Projeto

Última revisão: 2026-05-28.

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

Arquitetura em produção:

- aplicação alojada no Railway, executada como serviço web Django/ASGI com Daphne;
- PostgreSQL de produção alojado no Railway e acedido apenas por variáveis de ambiente;
- Redis alimenta Django Channels para realtime e cache de integrações como o clima;
- Cloudinary guarda imagens, anexos e SVGs de branding sem depender de disco persistente do container;
- Resend fornece envio transacional por API HTTP para confirmação de conta, recuperação de password, alertas e suporte;
- WhiteNoise serve assets estáticos compilados do deploy; assets de marca dinâmicos usam URLs Cloudinary.

Decisão arquitetural sobre API REST:

- a aplicação integra serviços externos por API, nomeadamente Resend para email e Cloudinary para media, mas não expõe uma API REST própria nesta versão;
- a interface entregue é uma aplicação web server-rendered com Django Templates e HTMX, adequada aos fluxos atuais de produtor e gestor sem necessidade de um frontend separado;
- não existia requisito de aplicação móvel, integração ERP, portal de clientes ou consumo externo que justificasse endpoints REST, autenticação de API, versionamento, documentação e testes adicionais;
- esta opção reduziu superfície de ataque e complexidade operacional numa aplicação já em produção, mantendo permissões, validações e auditoria concentradas nos fluxos Django existentes;
- a evolução futura está preparada: `external_customer_demands.source_system` e `external_id` permitem importar pedidos de sistemas externos, caso seja posteriormente necessário criar uma API com Django REST Framework;
- `djangorestframework` encontra-se declarado/instalado, mas não existem endpoints DRF funcionais na versão atual; pode ser removido se não for planeada uma integração futura.

Dependências e tecnologias relevantes para o relatório:

- `Django`: framework backend, autenticação, formulários, templates, views e regras de negócio;
- `psycopg` + PostgreSQL: persistência relacional em produção no Railway;
- `Daphne`, `channels`, `channels_redis` e `redis`: execução ASGI, comunicação realtime das mensagens e cache;
- `django-htmx`: navegação e atualização parcial da UI sem SPA/React/Vue;
- `django-anymail[resend]`: envio transacional por Resend via HTTP, sem dependência de SMTP em produção;
- `django-cloudinary-storage` e `cloudinary`: armazenamento de fotografias, anexos e logos;
- `whitenoise`: distribuição segura de ficheiros estáticos do deploy;
- `django-ratelimit`: proteção de operações sensíveis, como autenticação e submissões de suporte;
- `Pillow`: validação/processamento de imagens submetidas no marketplace;
- `python-decouple`: configuração por variáveis de ambiente;
- `requests`: consumo de serviços HTTP, incluindo dados meteorológicos;
- `Chart.js`: gráficos dos painéis de compras, vendas, produção e gestão, carregado no frontend.

Notas sobre o `requirements.txt`:

- várias bibliotecas listadas são dependências transitivas necessárias para Django, Channels/Daphne, TLS, Cloudinary ou o extra Resend de Anymail;
- o pacote `resend` não é importado diretamente pela aplicação: o provider é utilizado através de `django-anymail[resend]`;
- `djangorestframework` está instalado mas sem uso funcional atual, de acordo com a decisão de não expor API REST nesta entrega.

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
5. Pedidos e Necessidades;
6. Marketplace;
7. Encomendas;
8. Stocks e Compras;
9. Suporte;
10. Definições.

Comportamento visual:

- quando não há foto de perfil, o avatar mostra as iniciais do primeiro e último nome;
- o sino da topbar só mostra ping vermelho quando existem alertas pendentes;
- o logo compacto da sidebar usa SVG Cloudinary, sem moldura/quadrado, e fica centrado no modo mobile/colapsado.
- a área de necessidades foi renomeada na navegação para "Pedidos e Necessidades", porque agora inclui pedidos externos de clientes e procuras entre produtores.

## 5) Accounts

Páginas principais:

- `/login/`;
- `/registo/`;
- `/logout/`;
- `/recuperar-password/`;
- `/recuperar-password/<token>/`;
- `/convite/<token>/` para concluir convites administrativos.

Regras atuais:

- login usa rate limit por IP e email;
- registo usa rate limit por IP;
- passwords usam validadores Django configurados;
- email de confirmação é enviado no registo;
- se o email de confirmação falhar, a conta continua criada quando aplicável e a UI orienta o utilizador a contactar suporte;
- admins podem convidar utilizadores com nome, email profissional, acesso, empresa, tipo de entidade e mensagem opcional;
- dados empresariais indicados pelo admin ficam provisoriamente em `account_verification_tokens.invite_payload` e aparecem pré-preenchidos na conclusão do registo;
- o perfil operacional do produtor só é criado quando o convidado define a própria password e ativa a conta;
- convites administrativos expiram ao fim de 48 horas e podem ser reenviados ou revogados;
- admins podem validar manualmente o email de contas pendentes mediante justificação auditada; utilizadores convidados continuam obrigados a concluir o registo;
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
- botão do card de clima abre as definições já focadas na localização e pode iniciar pedido de localização ao browser;
- card de localização nas definições agrupa morada/código postal e cidade/distrito em linhas mais legíveis;
- coordenadas ficam num bloco próprio, com leitura mais técnica e menos intrusiva.

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

- saudação contextual com data (ver detalhe abaixo);
- KPIs: alertas ativos, stock crítico, encomendas pendentes, excedentes/anúncios;
- card de alertas prioritários e ações recomendadas (quando há situações urgentes);
- card de clima simplificado;
- card operacional "Hoje na exploração" (encomendas, stock, necessidades, anúncios);
- ações rápidas com acesso direto a recomendações, marketplace, stock, encomendas e necessidades.

Saudação (Bom dia / Boa tarde / Boa noite):

- calculada no browser com o algoritmo solar de Spencer (1971), sem chamada de API;
- usa coordenadas centrais de Portugal Continental (39.5°N, 8.0°W), com erro máximo de ≈10 min;
- lógica: antes do nascer do sol → "Boa noite"; nascer do sol ao meio-dia solar → "Bom dia"; meio-dia solar ao pôr do sol → "Boa tarde"; após pôr do sol → "Boa noite";
- substitui o anterior cutoff fixo por hora (`h < 12`, `h < 19`) que dizia "Bom dia" às 2h da manhã.

Inventário no painel:

- produtos críticos são apenas os que têm défice temporal real (`max_deficit > 0`);
- produção futura útil é considerada antes de mostrar risco;
- produtos com pedidos externos cobertos por stock atual + previsão disponível a tempo não contam como críticos;
- o painel usa a mesma fonte central do inventário: `calculate_inventory_commitment_state`.

Ações recomendadas (`_build_recommended_actions`):

- alertas críticos → "Resolver alertas críticos";
- stock crítico → "Reforçar stock de X";
- encomendas pendentes → "Acompanhar encomendas pendentes";
- necessidades abertas → "Necessidades à espera de cobertura" (adicionado);
- excedente não anunciado → "Publicar um possível excedente";
- sem urgências → "Tudo controlado".

Contexto servido por `build_client_dashboard_context` inclui agora `active_needs_count` exposto diretamente (além de alimentar `today_operations`).

Regras de UX:

- botões "Recomendações" e "Publicar excedente" só aparecem quando o produtor tem produtos ativos no inventário;
- "Atualizar stock"/inventário continua visível para permitir adicionar produtos;
- ações rápidas incluem "Necessidades" em ambos os layouts (alertas e ok);
- se não houver tarefas urgentes, o card lateral mostra estado simples.

Widget de clima:

- mostra localização, estado do tempo, temperatura mínima/máxima e chuva;
- se não houver localização, mostra CTA "Definir localização" que encaminha para o fluxo de geolocalização nas definições;
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
- gráfico de atividade comercial semanal;
- card "Suporte e utilizadores" com totais de utilizadores, suspensos, online/offline e suporte ativo;
- informação relevante para administração e monitorização diária da cooperativa;
- acesso a catálogo, utilizadores, suporte e auditoria sem misturar as regras de negócio dos módulos operacionais.

Gráfico de atividade comercial semanal:

- cobre as últimas 12 semanas;
- série "Pedidos criados" = encomendas criadas por semana com origem `MARKETPLACE` ou `RECOMMENDATION`;
- série "Itens vendidos e concluídos" = itens de encomenda `COMPLETED` por semana nas mesmas origens;
- a distinção evita sugerir que comprar e vender são operações independentes: a primeira série mede intenção/pedido criado e a segunda realização concluída;
- mostra estado vazio quando não existem dados no período.

Gestão de utilizadores:

- listagem clicável;
- detalhe com dados do utilizador;
- convite administrativo com dados iniciais pré-preenchidos, mensagem opcional e validade de 48 horas;
- detalhe do convite com último envio, validade e estado pendente/aceite/expirado/revogado;
- reenviar ou revogar convites pendentes;
- validar email manualmente apenas com justificação auditada;
- suspender/reativar utilizador;
- atividade relacionada com alterações de conta, login, password, suporte e ações admin.

Auditoria:

- pesquisa livre por ação, label, utilizador, email, IP, User-Agent e snapshots JSON;
- filtros estruturados por módulo, ação, utilizador, entidade e intervalo de datas;
- filtros de ação e utilizador com seleção pesquisável, evitando dropdowns extensos;
- badges visuais para Stock, Produção, Marketplace, Pedidos, Necessidades, Encomendas, Reservas, Suporte, Utilizadores e Catálogo;
- linhas clicáveis/expansíveis, com vista inicial focada no evento, entidade afetada, autor e momento;
- detalhe expandido com comparação de alterações, metadados de acesso/dispositivo e referência técnica;
- links read-only para abrir anúncios, encomendas, stocks, necessidades, pedidos externos, previsões e movimentos associados ao evento;
- exportação CSV real, respeitando os filtros ativos;
- paginação 10/25/50;
- texto de paginação dentro de `.adm-pagination`.

Arquitetura do backoffice:

- a interface de gestão vive atualmente sobretudo em `dashboard`, enquanto `support` mantém as ações admin próprias do domínio de suporte;
- as regras de negócio continuam nas apps responsáveis (`inventory`, `marketplace`, `needs`, `orders`, `support`), e o backoffice apenas consulta ou aciona serviços públicos desses domínios;
- esta separação evita duplicar validações críticas no painel administrativo;
- evolução futura possível: extrair rotas/templates de `/gestor/` para uma app `admin_panel` e extrair `AuditLog`, consulta/exportação e convenções de eventos para uma app `audit`;
- essa refatoração estrutural não é necessária para a entrega atual e foi evitada para reduzir risco de regressões em produção.

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
- no planeamento de pedidos externos, o stock imediatamente utilizável também desconta quantidades anunciadas em listings públicas `ACTIVE`/`RESERVED` ainda não ligadas a necessidades, evitando comprometer o mesmo stock duas vezes;
- `safety_stock`: campo técnico mantido por compatibilidade, atualmente sincronizado com a soma dos pedidos externos em aberto do produtor/produto;
- a BD de produção ainda contém `stocks.max_quantity` como coluna residual de uma iteração anterior; o modelo Django e a lógica funcional atual não utilizam esse campo;
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
- inclui ligação operacional para pedidos de clientes associados ao produto;
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

- mostra anúncios de venda (`MarketplaceListing`) do produtor autenticado e as procuras publicadas por esse mesmo produtor (`Need` com `is_marketplace_published=True`);
- procuras aparecem com cards visuais distintos (borda verde, cabeçalho "A sua procura", badge "Publicada") incluindo contagem de propostas recebidas;
- ações do dono de procura: "Ver detalhes e propostas" (navega para `/necessidades/?need=<id>`) e "Retirar do marketplace";
- a linha de contagem de resultados separa "X oferta(s) · Y procura(s)";
- o contador do tab soma listings + procuras publicadas para o badge numérico;
- não usa badge "meu anúncio" no feed público porque anúncios/procuras próprios já não aparecem ali.

Tabs adicionais do marketplace:

- "Compras" resume compras/encomendas originadas no marketplace;
- "Respostas" mostra propostas recebidas para procuras próprias e propostas enviadas pelo produtor para procuras/necessidades;
- cards usam badges contextuais para estados como proposta pendente, aceite, rejeitada ou encomenda associada;
- empty states evitam CTAs errados como criar necessidade manual quando o contexto é marketplace.

Tipos de anúncio:

- stock atual (`stock_id`);
- pré-venda (`forecast_id`);
- resposta privada a necessidade (`need_id`).

Procuras no marketplace:

- vêm de `needs`, não de `marketplace_listings`;
- são cards de procura visualmente distintos das ofertas;
- aparecem quando a necessidade está `OPEN` ou `PARTIALLY_COVERED` e ainda existe quantidade por planear;
- CTA principal abre `/marketplace/procuras/<need_id>/responder/?product=<id>`;
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
- compras a partir de anúncio usam bloqueios transacionais compatíveis com PostgreSQL, evitando `FOR UPDATE` em joins nullable.

## 13) Needs

Páginas:

- `/necessidades/`;
- `/necessidades/pedidos-clientes/`;
- `/necessidades/pedidos-clientes/criar/`;
- `/necessidades/pedidos-clientes/<uuid>/editar/`;
- `/necessidades/pedidos-clientes/<uuid>/cancelar/`;
- `/necessidades/criar/`;
- `/necessidades/<uuid>/ignorar/`;
- `/necessidades/<uuid>/editar/`;
- propostas privadas são geridas no marketplace:
  `/marketplace/procuras/<need_id>/responder/`,
  `/marketplace/propostas/<listing_id>/`,
  `/marketplace/propostas/<listing_id>/editar/`,
  `/marketplace/propostas/<listing_id>/rejeitar/`;
- rotas antigas `/necessidades/responder/` e `/necessidades/respostas/<listing_id>/...`
  ficam como compatibilidade.

Estados da necessidade:

- `OPEN`;
- `PARTIALLY_COVERED`;
- `COVERED`;
- `IGNORED`;
- `CANCELLED`.

Página `/necessidades/`:

- layout master-detail;
- "As minhas necessidades";
- destaque para pedidos externos de clientes quando existem compromissos relevantes;
- detalhe da necessidade selecionada;
- propostas recebidas pendentes para uma procura selecionada;
- KPIs e CTAs encaminham históricos de propostas recebidas/enviadas para `/marketplace/?tab=respostas`;
- a página fica focada em pedidos externos, procuras próprias e publicação/retirada de procuras no marketplace.

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
- a página funciona como painel operacional, não como tabela administrativa pura;
- mostra totais abertos, produtos com pedidos, maior défice, próxima data crítica e planeamento por produto;
- cálculo temporal por produto/data:
  - compara pedidos acumulados até cada data com stock disponível atual e produção prevista útil até essa data;
  - forecast conta se `period_end <= requested_delivery_date`; se não houver `period_end`, conta por `period_start`; forecasts sem data válida não contam;
  - mostra maior défice e primeira data crítica;
- a tabela de planeamento apresenta "Stock inicial", "Produção prevista útil acumulada", "Capacidade acumulada" e "Capacidade restante após pedidos", deixando explícito que o stock não reinicia em cada data;
- a pré-visualização em `/necessidades/` usa o mesmo cálculo temporal cumulativo, evitando comparar cada pedido isoladamente contra o stock atual;
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

Publicação de needs no marketplace:

- needs `MANUAL` são criadas com `is_marketplace_published=True` e `published_at` preenchido — publicação automática na criação;
- needs `CUSTOMER_DEMAND` são criadas com `is_marketplace_published=False` e `published_at=NULL` — exigem ação explícita "Publicar" na tabela de `/necessidades/`;
- o botão de publicação mostra "Republicar" (ícone `bi-arrow-clockwise`) quando `published_at IS NOT NULL` mas a need não está publicada atualmente, indicando que já esteve publicada antes;
- badge "Publicada" aparece na coluna Estado da tabela quando `is_marketplace_published=True`;
- todos os formulários de ação (Publicar, Retirar, Ignorar) usam `hx-disabled-elt="find button"` para prevenir duplo-clique que causava sequências publicar/retirar imediatas;
- `published_at` é preservado tanto no retirar explícito como no auto-retirar (nunca apagado), servindo de memória da última publicação;
- auto-retirar ocorre em dois cenários: (1) quando `required_quantity` ou `needed_by_date` muda ao recalcular pedidos externos (criação ou edição de pedido externo) — é mostrado um toast de aviso HTMX "Pedido atualizado. A procura no marketplace foi retirada porque a quantidade ou data mudou — republique se ainda quiser receber propostas."; (2) quando o status da need passa a `COVERED` por `recalculate_need_status` — silencioso, sem toast;
- evento de auditoria `NEED_MARKETPLACE_UNPUBLISHED_AFTER_RECALCULATION` é registado em ambos os casos de auto-retirar;
- toasts de confirmação HTMX são emitidos via `with_htmx_toast()` em `apps/common/htmx.py` que adiciona `HX-Trigger: {"app:toast": {...}}` ao response sem sobrescrever outros triggers — necessário porque swaps HTMX nunca re-executam `base.html` onde os `messages` Django seriam renderizados.

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

- abre `/marketplace/procuras/<need_id>/responder/?product=<id>`;
- vê card "O seu inventário" com stock atual, reservado, disponível, compromissos externos, produção futura e máximo publicável;
- envia proposta com quantidade, preço, entrega e observações;
- se já existir proposta pendente para essa necessidade, edita a proposta existente;
- só pode criar nova proposta depois de a anterior deixar de estar pendente.

Fluxo do dono da necessidade:

- vê ofertas recebidas;
- abre detalhe em `/marketplace/propostas/<listing_id>/`;
- pode comprar/aceitar proposta pendente;
- pode rejeitar proposta pendente;
- não pode comprar proposta rejeitada/cancelada/concluída;
- quando a proposta já originou encomenda, o detalhe mostra a encomenda associada e link para `/encomendas/<order_id>/`.

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

- mostrar alertas operacionais que exigem atenção ou acompanhamento com uma hierarquia visual limpa e despoluída.

Tabs:

- ativos;
- ignorados/adiados;
- resolvidos.

Filtros:

- tipo;
- categoria;
- pesquisa;
- apenas alertas que exigem ação.
- interface colapsável de filtros com ponto sinalizador de filtros ativos.

Hierarquia Visual e Badges (Fase 2):
- **Badges de Status (Prioridade Máxima):** Badges pulsantes `Novo` (azul) e `Ação Requerida` (vermelho com ping animado) no topo esquerdo do cartão.
- **Badges de Risco (Condicionais):** Badges de severidade `Crítico` (vermelho) ou `Atenção` (laranja) são exibidos apenas para severidade `CRITICAL` ou `WARNING`. Nenhuma badge de severidade é exibida para a severidade `INFO` (reduzindo ruído).
- **Metadados Inline (Context Row):** Categoria e Tipo detalhado de alerta foram movidos para uma linha de contexto em texto cinzento discreto acima do título (Exemplo: `STOCK · Défice nos pedidos externos`).
- Apenas um máximo de 2 badges visuais são mostrados em simultâneo.

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
- adiar/lembrar mais tarde (menu popover estético para adiar por 1h, amanhã ou 1 semana);
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

- mostra últimas 6 notificações recentes na barra lateral.
- **Ícones Contextuais Dinâmicos:** Exibe ícones específicos baseados no tipo (`MESSAGE` -> envelope/chat azul, `ORDER_UPDATE` -> caixa verde, `ALERT` -> sino laranja, `RECOMMENDATION` -> lâmpada amarela, etc.) com cores de fundo suaves.
- **Destaque de Não Lido:** Itens não lidos apresentam um ponto de leitura azul pulsante no topo superior do ícone.
- botão "Limpar notificações" remove apenas notificações do utilizador atual com transição HTMX;
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
- indicador "Ligação ativa"/"Sem ligação" mostra estado da ligação WebSocket do utilizador atual à conversa, não presença real do outro utilizador.

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
- respostas privadas a necessidades usam o fluxo de propostas do marketplace (`/marketplace/propostas/...`) e não o contacto normal por mensagem.
- abrir `/mensagens/` sem conversa explícita não marca automaticamente a primeira conversa como lida;
- quando uma mensagem nova chega a uma conversa arquivada, a conversa volta para "Ativas" para os destinatários;
- notificações recentes de mensagens são deduplicadas por conversa e marcadas como lidas quando a conversa é aberta.
- pós-envio de mensagem passa por helper único: atualiza conversa, marca remetente como lido, cria/atualiza notificação e emite WebSocket.

Funcionalidades adicionais implementadas (2026-05-25):

- **Indicador de "a escrever"**: ao escrever no composer, o cliente envia `typing.start`/`typing.stop` via WebSocket; o servidor broadcast `typing.update` ao grupo (excluindo o remetente); a UI mostra animação de três pontos com o nome do utilizador; o indicador desaparece automaticamente após 3,5 s ou quando chega uma mensagem.

- **Recuperação de mensagens após reconexão**: ao reconectar o WebSocket, o cliente envia `conversation.sync` com o `last_message_id` da última mensagem renderizada; o servidor devolve `messages.catchup` com as mensagens perdidas desde esse id (máximo 50).

- **Read receipts**: quando um participante lê mensagens, o servidor faz broadcast de `read.update` ao grupo; o cliente que enviou as mensagens mostra "Lido" sob a última mensagem sua; o estado inicial de leitura é calculado no carregamento da página a partir de `ConversationParticipant.last_read_at`.

- **Pesquisa dentro da conversa**: botão de lupa no cabeçalho da thread abre painel de pesquisa; endpoint `GET /mensagens/conversa/<uuid>/pesquisar/?q=` devolve JSON com até 25 mensagens de texto que contenham o termo; resultados clicáveis fazem scroll e highlight da mensagem na thread.

- **Agrupamento visual**: mensagens consecutivas do mesmo remetente recebem classe `.is-grouped` que reduz o espaçamento e ajusta os border-radius dos balões, criando um aspeto de "bloco" semelhante a apps de chat modernas.

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

Realtime no detalhe do ticket (implementado 2026-05-25):

- novo WebSocket consumer `SupportTicketConsumer` na rota `ws/suporte/ticket/<uuid>/`;
- aceita ligações do requester do ticket OU de qualquer admin;
- quando admin responde (`reply_support_ticket`) ou cliente responde (`reply_support_ticket_as_requester`), o serviço serializa a mensagem e faz broadcast via `transaction.on_commit` ao grupo `support_ticket_<id>`;
- a view de detalhe do ticket do cliente (`/suporte/<uuid>/`) liga ao WebSocket e adiciona novas mensagens ao DOM sem recarregar a página;
- indicador de estado "Ligação ativa"/"Sem ligação" visível no cabeçalho da conversa;
- quando admin responde, é criada notificação in-app (`NotificationType.SYSTEM`) para o requester com link para o ticket.

Badge de suporte:

- o evento `support.badge.changed` emitido ao grupo `support_admin_badge` inclui agora o campo `count` com o número de tickets que precisam de atenção admin;
- conta tickets `OPEN` + tickets `CLAIMED` com última mensagem do requester.

FAQ:

- redesenhada com categorias (Conta, Mercado, Stock, Ajuda), pesquisa de texto e accordion animado;
- 13 perguntas frequentes cobrindo conta, marketplace, necessidades, stock e suporte;
- interatividade completamente em JavaScript vanilla (sem dependências externas).

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

Infraestrutura:

- helper transversal: `apps.common.audit.log_audit_event(...)`;
- o log guarda utilizador/ator quando existe, ação, entidade, `entity_id`, `old_values`, `new_values`, notas, IP, User-Agent e data/hora;
- ações automáticas podem ser registadas com ator vazio e notas que explicam a origem de sistema;
- quando um evento é emitido dentro de uma transação de negócio, a escrita da auditoria fica isolada por savepoint; uma falha de auditoria não inutiliza a transação principal;
- fora de transações, o helper também captura falhas e não impede a operação funcional.

Eventos de conta, administração e suporte já registados:

- início de sessão e alterações relevantes de conta/perfil/preferências/password;
- criação, reenvio e revogação de convites, confirmação justificada de email por admin e alteração/suspensão/reativação de utilizadores;
- criação, atualização, claim, resposta do admin, resposta do utilizador e fecho de tickets de suporte;
- criação, atualização e remoção de produtos/categorias do catálogo.

Auditoria operacional crítica implementada:

- pedidos externos:
  - `EXTERNAL_DEMAND_CREATED`;
  - `EXTERNAL_DEMAND_UPDATED`;
  - `EXTERNAL_DEMAND_CANCELLED`;
- necessidades e propostas:
  - `CUSTOMER_DEMAND_NEED_CREATED`;
  - `CUSTOMER_DEMAND_NEED_UPDATED`;
  - `CUSTOMER_DEMAND_NEED_COVERED`;
  - `NEED_MARKETPLACE_PUBLISHED`;
  - `NEED_MARKETPLACE_WITHDRAWN`;
  - `NEED_MARKETPLACE_UNPUBLISHED_AFTER_RECALCULATION`;
  - `NEED_RESPONSE_CREATED`;
  - `NEED_RESPONSE_UPDATED`;
  - `NEED_RESPONSE_REJECTED`;
  - `NEED_COVERAGE_CHANGED`;
- stock e produção futura:
  - `STOCK_CREATED`;
  - `STOCK_UPDATED`;
  - `STOCK_MOVEMENT_CREATED`;
  - `FORECAST_CREATED`;
  - `FORECAST_UPDATED`;
  - `FORECAST_DELETED`;
  - `FORECAST_ASSIMILATED`;
- marketplace:
  - `LISTING_CREATED`;
  - `LISTING_UPDATED`;
  - `LISTING_STATUS_CHANGED`;
  - `LISTING_RETIRED`;
  - `LISTING_EXPIRED`;
  - `LISTING_INVALID_ATTEMPT`;
- encomendas e reservas:
  - `ORDER_CREATED`;
  - `ORDER_STATUS_CHANGED`;
  - `ORDER_RECEIPT_CONFIRMED`;
  - `ORDER_CANCELLED`;
  - `STOCK_RESERVATION_CHANGED`;
  - `FORECAST_RESERVATION_CHANGED`.

Snapshots operacionais:

- pedidos/needs guardam produtor, produto, nome do produto, quantidade, data pretendida, estado e ligação à procura agregada quando aplicável;
- stock/forecast guardam produto, quantidades atuais/reservadas/comprometidas, períodos da previsão e origem da operação;
- anúncios/propostas guardam origem (`stock`, `forecast` ou `need_response`), quantidades, preço, entrega, estado e `need_id` quando privado;
- encomendas/reservas guardam encomenda, listing, produto, comprador/vendedor, quantidade, mudança de estado e alterações de reserva;
- não são guardadas passwords, tokens, conteúdo privado de mensagens ou contactos completos desnecessários dos clientes externos.

Regras:

- não guardar segredos;
- guardar IP, user-agent e descrição de dispositivo;
- guardar snapshots `old_values`/`new_values` quando útil;
- manter snapshots pequenos e orientados a investigação;
- falhas de auditoria não devem quebrar ações principais já concluídas.

UI admin:

- a tabela permite pesquisa e filtros por módulo, ação, utilizador, entidade e datas;
- seletores de ação e utilizador são pesquisáveis para funcionar com muitos eventos/utilizadores;
- cada linha prioriza o evento, a referência afetada, o autor e o momento, mantendo IP/dispositivo no detalhe;
- a tabela é expansível por linha;
- o detalhe mostra resumo operacional, alterações legíveis e referência técnica;
- a referência técnica ocupa a largura útil do detalhe expandido para evitar layout desequilibrado;
- dados técnicos continuam disponíveis, mas deixam de ser a primeira leitura visual;
- links read-only permitem investigar entidades relacionadas sem entrar em fluxos operacionais do produtor;
- export CSV permite análise externa dos eventos filtrados.

Monitorização operacional:

- o dashboard admin resume atividade comercial, utilizadores, suporte e operações relevantes;
- badges de suporte e alertas ajudam a identificar trabalho pendente;
- auditoria complementa métricas agregadas com rastreabilidade individual: quem fez a ação, sobre que entidade, em que momento e com que alteração;
- alertas e notificações são mecanismos operacionais para o produtor; `audit_log` é o mecanismo de investigação administrativa e não substitui esses fluxos.

Trabalho posterior não incluído nesta versão:

- auditar eventos secundários como login falhado, logout, anexos de suporte/mensagens, falhas de email e falhas de entrega de alertas;
- eventual extração para apps dedicadas `admin_panel` e `audit`, mantendo sempre as regras de negócio nas apps de domínio.

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
- `sqlscript.sql` é atualizado através de exportação apenas de schema da base PostgreSQL de produção, sem conteúdo de utilizadores ou segredos;
- o snapshot de produção de 2026-05-24 confirma a presença residual de `stocks.max_quantity`; a remoção física exige SQL explícito numa iteração controlada;
- como os modelos de negócio são `managed=False`, alterações de schema não são aplicadas por migrations Django.

## 26) Organização de Documentação e Memória Local

Documentação oficial do projeto:

- `README.md`: guia geral técnico/funcional, setup, deploy e validação;
- `PROJECT_CONTEXT.md`: mapa funcional detalhado para relatório e continuidade do desenvolvimento;
- `sqlscript.sql`: schema consolidado atual;
- ficheiros SQL/TXT operacionais que sejam necessários para reproduzir alterações de BD.

Memórias locais e planos temporários:

- ficam em `memory/`;
- estão ignorados pelo Git;
- não devem ser usados como fonte de verdade para relatório sem validação contra `PROJECT_CONTEXT.md`.

Objetivo:

- manter o repositório limpo;
- separar documentação estável de notas de trabalho;
- evitar que planos antigos ou apontamentos temporários entrem em produção ou confundam futuras análises.
