# VISION4FARMS

Plataforma web B2B para gestão agrícola cooperativa. O projeto permite a produtores gerir inventário, compromissos externos com clientes, produção futura, marketplace interno, necessidades/procuras, recomendações, encomendas, mensagens e suporte.

## Visão Geral

O VISION4FARMS foi desenhado para apoiar uma cooperativa agrícola no fluxo completo entre produção, stock, procura e venda:

- o produtor regista produtos, stock atual e previsões de produção;
- regista pedidos externos dos seus próprios clientes;
- o sistema calcula se existe capacidade para cumprir esses pedidos a tempo;
- quando existe défice, gera uma procura agregada;
- outros produtores podem responder a essa procura;
- as respostas podem originar encomendas;
- alertas, recomendações e dashboards ajudam a tomar decisões.

A aplicação usa Django Templates e HTMX, sem frontend SPA. A base de dados é PostgreSQL e a maior parte dos modelos de negócio é `managed=False`, com schema mantido por SQL manual.

## Stack Técnica

- Backend: Django 6.0.3
- Base de dados: PostgreSQL
- Templates: Django Templates
- Interações parciais: HTMX
- Realtime: Django Channels + Daphne + Redis
- Storage de imagens e anexos: Cloudinary via `default_storage`
- Static files: WhiteNoise
- Email: Resend via `django-anymail`
- Gráficos: Chart.js
- Configuração: `python-decouple`

## Apps Principais

### `accounts`

Autenticação, registo, confirmação de conta, recuperação de palavra-passe e emails de segurança.

Fluxos relevantes:

- `/login/`
- `/registo/`
- `/logout/`
- `/recuperar-password/`

### `dashboard`

Painel do produtor e dashboard de administração.

Produtor:

- visão operacional diária;
- clima;
- alertas;
- encomendas;
- oportunidades;
- estado de inventário.

Admin:

- métricas de utilizadores;
- suporte;
- encomendas;
- atividade comercial;
- auditoria.

### `catalog`

Gestão de categorias e produtos globais.

Admin gere:

- `/gestor/produtos/`
- `/gestor/categorias/`

Produtos usam `kg` como unidade operacional principal.

### `inventory`

Inventário do produtor, stock, movimentos e produção futura.

Rotas principais:

- `/inventario/produtos/?tab=stock`
- `/inventario/produtos/?tab=compras`
- `/inventario/produtos/adicionar/`
- `/inventario/stock/<uuid>/`

Conceitos principais:

- `current_quantity`: stock físico atual;
- `reserved_quantity`: quantidade reservada em anúncios/encomendas;
- `available = current_quantity - reserved_quantity`;
- `safety_stock`: campo técnico reaproveitado como compromissos externos derivados de pedidos de clientes;
- `production_forecasts`: produção futura que só conta para cobertura quando está disponível até à data de entrega do pedido externo;
- `stock_movements`: histórico de alterações ao stock.

A fonte central do estado operacional é `calculate_inventory_commitment_state(producer, product)`, que considera pedidos externos e produção futura temporal antes de marcar um produto como crítico.

### `needs`

Necessidades/procuras entre produtores e pedidos externos de clientes.

Rotas principais:

- `/necessidades/`
- `/necessidades/pedidos-clientes/`
- `/necessidades/responder/`
- `/necessidades/respostas/<listing_id>/`

Arquitetura atual:

- `external_customer_demands`: pedidos externos reais dos clientes do agricultor;
- `needs`: procura agregada por produtor/produto;
- `marketplace_listings` com `need_id`: propostas privadas de outros produtores;
- `order_items.need_id`: ligação entre encomenda e necessidade.

Regra temporal:

```text
défice por data =
pedidos externos acumulados até à data
-
(stock disponível atual + produção prevista útil até essa data)
```

Uma previsão só conta se:

- `period_end <= requested_delivery_date`; ou
- se não houver `period_end`, `period_start <= requested_delivery_date`;
- previsões sem data válida não contam;
- usa `forecast_quantity - reserved_quantity`.

Quando existe défice, é criada/atualizada uma `Need` agregada com `source_system=CUSTOMER_DEMAND`. Pedidos externos individuais não são fechados automaticamente nesta versão.

### `marketplace`

Marketplace interno para ofertas e procuras.

Rotas principais:

- `/marketplace/`
- `/marketplace/publicar/`
- `/marketplace/<uuid>/`

O feed público mostra:

- ofertas públicas de outros produtores;
- procuras agregadas vindas de `needs`;
- não mostra anúncios do próprio produtor;
- não mostra listings privadas com `need_id`.

Tabs atuais:

- anúncios disponíveis;
- meus anúncios;
- compras;
- respostas/propostas.

Tipos de oferta:

- stock atual;
- pré-venda;
- resposta privada a necessidade.

As procuras não são transformadas em `MarketplaceListing`; continuam a ser `Need`.

### `recommendations`

Wizard para ajudar o produtor a decidir entre comprar ou vender.

Regra atual:

- comprar quando existe `max_deficit` temporal para cumprir pedidos externos;
- vender quando existe `temporal_sellable_quantity` acima dos compromissos externos;
- não recomendar compra quando a produção futura útil cobre os pedidos a tempo.

### `orders`

Gestão de encomendas e fluxo operacional entre comprador e vendedor.

Rotas principais:

- `/encomendas/`
- `/encomendas/<uuid>/`
- `/encomendas/criar/anuncio/<uuid>/`

As encomendas atualizam cobertura de necessidades através de `order_items.need_id`. Compra, cancelamento e conclusão sincronizam estados de propostas quando aplicável.

### `alerts` e `notifications_app`

Alertas operacionais e notificações informativas.

Alertas críticos de stock usam a fonte temporal central e só devem aparecer quando existe défice real para cumprir pedidos externos a tempo.

Notificações recentes podem ser limpas pelo utilizador sem resolver os alertas reais.

### `messaging`

Mensagens entre produtores, com anexos via storage configurado.

Melhorias já aplicadas:

- anexos de imagem como thumbnails;
- validação mais segura;
- labels de ligação mais claras;
- divisores de data por dia.

### `support`

Página dedicada de ajuda e suporte.

Rotas principais:

- `/suporte/`
- `/suporte/<ticket_id>/`
- `/gestor/suporte/`
- `/gestor/suporte/<ticket_id>/`

Suporte é conversacional:

- múltiplas mensagens por ticket;
- anexos de imagem;
- admin responde sem fechar automaticamente;
- fecho é ação explícita.

### `settings_app`

Definições do utilizador:

- identidade e perfil;
- localização;
- foto;
- notificações;
- segurança.

O suporte foi removido das definições e vive em `/suporte/`.

## Branding

Os logos e favicon usam Cloudinary por variáveis de ambiente:

- `BRAND_LOGO_COLOR_URL`
- `BRAND_LOGO_WHITE_URL`
- `BRAND_LOGIN_LOGO_WHITE_URL`
- `BRAND_SIDEBAR_COMPACT_LOGO_URL`
- `BRAND_FAVICON_URL`

Se as variáveis não estiverem configuradas, `settings.py` fornece defaults Cloudinary. A aplicação já não depende de `static/brand/*.svg`, evitando erros de manifest em produção.

## Email

Produção usa Resend via API HTTP:

- `EMAIL_PROVIDER=resend`
- `EMAIL_BACKEND=anymail.backends.resend.EmailBackend`
- `RESEND_API_KEY`

Variáveis comuns:

- `DEFAULT_FROM_EMAIL`
- `DEFAULT_REPLY_TO_EMAIL`
- `SUPPORT_CONTACT_EMAIL`

SMTP fica apenas como fallback quando `EMAIL_PROVIDER=smtp`.

## Variáveis de Ambiente

Obrigatórias/relevantes:

- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS`
- `APP_BASE_URL`
- `DATABASE_URL` ou `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_PORT`
- `REDIS_URL`
- `EMAIL_PROVIDER`
- `EMAIL_BACKEND`
- `RESEND_API_KEY`
- `DEFAULT_FROM_EMAIL`
- `DEFAULT_REPLY_TO_EMAIL`
- `SUPPORT_CONTACT_EMAIL`
- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`
- `BRAND_LOGO_COLOR_URL`
- `BRAND_LOGO_WHITE_URL`
- `BRAND_LOGIN_LOGO_WHITE_URL`
- `BRAND_SIDEBAR_COMPACT_LOGO_URL`
- `BRAND_FAVICON_URL`

`DEBUG` deve ser booleano (`true` ou `false`). Valores como `release` são inválidos.

## Instalação Local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Criar `.env` local com as variáveis necessárias.

Validar configuração:

```powershell
python manage.py check
```

Arrancar servidor local:

```powershell
python manage.py runserver
```

## Base de Dados

O schema é gerido manualmente por SQL.

Ficheiros relevantes:

- `sqlscript.sql`: schema consolidado atual;
- `EXTERNAL_CUSTOMER_DEMANDS_SQL.txt`: SQL específico da tabela de pedidos externos;
- outros ficheiros SQL/TXT pontuais documentam alterações aplicadas ao longo do desenvolvimento.

Como vários models usam `managed=False`, não confiar em migrations Django para criar/alterar tabelas de negócio.

## Testes e Validação

Comando base:

```powershell
python manage.py check
```

Suites úteis:

```powershell
python manage.py test apps.needs.tests apps.inventory.tests apps.marketplace.tests --keepdb
python manage.py test apps.orders.tests apps.recommendations.tests apps.alerts.tests --keepdb
python manage.py test apps.dashboard.tests apps.support.tests apps.messaging.tests --keepdb
```

Validações manuais recomendadas antes de deploy:

- criar produto no inventário;
- criar produção futura;
- criar pedido externo de cliente;
- confirmar cálculo temporal em `/necessidades/pedidos-clientes/`;
- confirmar que a procura aparece no marketplace;
- responder a uma procura com outro produtor;
- aceitar proposta e criar encomenda;
- confirmar cobertura da necessidade;
- confirmar que alertas não aparecem quando a produção prevista cobre os pedidos a tempo.

## Deploy em Railway

Cuidados principais:

- definir variáveis de ambiente;
- garantir `DEBUG=false`;
- configurar Redis;
- configurar Cloudinary;
- configurar Resend;
- executar SQL manual necessário antes/depois do deploy conforme o caso;
- correr `python manage.py check`;
- configurar agendamento para `sync_operational_alerts --apply`.

## Organização de Documentos

Documentos oficiais rastreados no projeto:

- `README.md`;
- `PROJECT_CONTEXT.md`;
- `sqlscript.sql`;
- SQLs operacionais necessários para reproduzir o schema.

Notas locais, memórias, planos temporários e ficheiros de trabalho ficam em `memory/` e são ignorados pelo Git.

## Segurança e Integridade

Regras importantes:

- não guardar segredos no repositório;
- validar ownership em fluxos de needs, marketplace, orders, support e messaging;
- bloquear edição de anúncios fechados;
- bloquear edição direta de needs automáticas `CUSTOMER_DEMAND`;
- manter listings privadas com `need_id` fora do feed público;
- evitar duplicação de propostas pendentes para a mesma necessidade;
- usar transações em operações que sincronizam need, stock, propostas e encomendas;
- fallback seguro quando serviços auxiliares falham.

## Estado Atual

O projeto está focado na lógica de compromissos externos:

- pedidos de clientes são fonte de verdade da procura do agricultor;
- necessidades são agregadas por produto;
- produção futura só conta quando chega a tempo;
- marketplace mostra ofertas e procuras de forma distinta;
- recomendações, alertas, dashboard e inventário usam cálculo temporal para evitar falsos críticos.
