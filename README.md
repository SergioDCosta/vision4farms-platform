# VISION4FARMS

[![Django](https://img.shields.io/badge/Django-6.0.3-0C4B33?logo=django&logoColor=white)](https://www.djangoproject.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-schema--first-336791?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![HTMX](https://img.shields.io/badge/HTMX-server--rendered-3366CC)](https://htmx.org/)
[![Railway](https://img.shields.io/badge/Deploy-Railway-0B0D0E?logo=railway&logoColor=white)](https://railway.com/)

VISION4FARMS é uma plataforma web B2B para produtores agrícolas e cooperativas. A aplicação ajuda produtores a gerir stock, planear entregas a clientes externos, publicar excedentes, responder a procuras no marketplace, receber recomendações operacionais, acompanhar encomendas, trocar mensagens e abrir pedidos de suporte.

A interface é server-rendered com Django Templates e HTMX. A aplicação evita uma SPA separada e concentra as regras de negócio em serviços Django, com PostgreSQL como fonte principal de dados.

## Índice

- [Funcionalidades](#funcionalidades)
- [Fluxos principais](#fluxos-principais)
- [Stack técnica](#stack-técnica)
- [Módulos da aplicação](#módulos-da-aplicação)
- [Rotas principais](#rotas-principais)
- [Setup local](#setup-local)
- [Base de dados](#base-de-dados)
- [Testes e validação](#testes-e-validação)
- [Deploy](#deploy)
- [Documentação](#documentação)
- [Segurança](#segurança)

## Funcionalidades

- Gestão de produtos do produtor, stock atual, stock reservado, movimentos e previsões de produção.
- Registo de pedidos externos de clientes com datas de entrega pretendidas.
- Cálculo temporal de défices por produto e data de entrega.
- Criação de necessidades/procuras agregadas a partir desses défices.
- Marketplace com ofertas públicas, pré-vendas e procuras publicadas.
- Respostas privadas a procuras, tratadas como propostas dentro do marketplace.
- Conversão de propostas aceites em encomendas, com associação à necessidade original.
- Recomendações de compra e venda com base em stock, procura e margem disponível.
- Alertas operacionais para risco de stock, oportunidades, encomendas e propostas.
- Mensagens entre produtores com anexos e atualização em tempo real.
- Suporte conversacional para clientes e gestores.
- Consola de gestão para utilizadores, catálogo, categorias, auditoria e suporte.
- Convites administrativos com dados pré-preenchidos, expiração, reenvio, revogação e auditoria.

## Fluxos principais

### Planeamento de procura

```text
Pedido externo de cliente
        |
        v
Cálculo temporal de défice
        |
        v
Necessidade agregada
        |
        v
Publicação no marketplace
```

O sistema compara pedidos acumulados até cada data de entrega com o stock atual disponível e previsões úteis até essa data. Quando existe défice, é gerada uma necessidade que pode ser publicada como procura no marketplace.

### Propostas no marketplace

```text
Procura publicada
        |
        v
Outro produtor envia proposta
        |
        v
Dono da procura aceita ou rejeita
        |
        v
Proposta aceite cria encomenda
```

As propostas privadas vivem no domínio do marketplace:

- responder a uma procura: `/marketplace/procuras/<need_id>/responder/`;
- consultar uma proposta: `/marketplace/propostas/<listing_id>/`;
- ver histórico de propostas: `/marketplace/?tab=respostas`.

As rotas antigas em `/necessidades/respostas/...` existem apenas por compatibilidade e redirecionam para o marketplace.

### Encomendas e cobertura

Quando uma proposta é aceite, a encomenda criada mantém ligação à necessidade através de `order_items.need_id`. Essa ligação permite recalcular a cobertura da necessidade quando a encomenda muda de estado, é cancelada, entregue ou concluída.

## Stack técnica

| Área | Tecnologia |
| --- | --- |
| Backend | Django 6.0.3 |
| Base de dados | PostgreSQL |
| UI | Django Templates + HTMX |
| Realtime | Django Channels + Daphne + Redis |
| Static files | WhiteNoise |
| Media storage | Cloudinary via `default_storage` |
| Email | Resend através de `django-anymail` |
| Gráficos | Chart.js |
| Configuração | `python-decouple` |
| Deploy | Railway |

## Arquitetura de produção

Em produção, a aplicação corre no Railway como serviço ASGI com Daphne. O PostgreSQL guarda os dados operacionais, Redis suporta Channels e caches selecionadas, Cloudinary guarda imagens e anexos, Resend envia email transacional e WhiteNoise serve os ficheiros estáticos compilados.

Os uploads e assets dinâmicos não dependem do disco do container.

## Módulos da aplicação

| App | Responsabilidade |
| --- | --- |
| `accounts` | Autenticação, registo, verificação de email, recuperação de password e convites |
| `catalog` | Produtos e categorias globais |
| `inventory` | Produtos do produtor, stock, previsões, movimentos e compras/vendas |
| `needs` | Pedidos externos de clientes e necessidades agregadas |
| `marketplace` | Ofertas, pré-vendas, procuras publicadas e propostas privadas |
| `recommendations` | Recomendações de compra/venda e wizard operacional |
| `orders` | Encomendas, grupos, itens e estados operacionais |
| `alerts` | Alertas e notificações operacionais |
| `notifications_app` | Notificações informativas recentes |
| `messaging` | Conversas, anexos, leitura e realtime |
| `support` | Tickets de suporte para clientes e gestores |
| `settings_app` | Perfil, localização, fotografia, notificações e segurança |
| `dashboard` | Painel do produtor e consola de gestão |
| `common` | Decorators, auditoria, redirects, helpers HTMX e formatação |
| `integrations` | Integrações externas e serviços auxiliares |

## Rotas principais

### Produtor

| Rota | Descrição |
| --- | --- |
| `/painel/` | Painel operacional |
| `/inventario/produtos/?tab=stock` | Stock e produção |
| `/inventario/produtos/?tab=compras` | Compras, vendas e encomendas relacionadas |
| `/necessidades/` | Pedidos, necessidades próprias e publicação de procuras |
| `/necessidades/pedidos-clientes/` | Planeamento de pedidos externos de clientes |
| `/marketplace/` | Marketplace público |
| `/marketplace/?tab=respostas` | Histórico de propostas recebidas e enviadas |
| `/marketplace/publicar/` | Publicar oferta ou pré-venda |
| `/marketplace/procuras/<need_id>/responder/` | Responder a uma procura publicada |
| `/marketplace/propostas/<listing_id>/` | Detalhe de uma proposta privada |
| `/encomendas/` | Encomendas |
| `/recomendacoes/` | Recomendações |
| `/mensagens/` | Mensagens |
| `/alertas/` | Alertas |
| `/suporte/` | Suporte |
| `/definicoes/` | Definições do utilizador |

### Gestor

| Rota | Descrição |
| --- | --- |
| `/gestor/` | Dashboard de gestão |
| `/gestor/produtos/` | Catálogo de produtos |
| `/gestor/categorias/` | Categorias |
| `/gestor/utilizadores/` | Utilizadores |
| `/gestor/auditoria/` | Auditoria |
| `/gestor/suporte/` | Fila de suporte |

## Setup local

### 1. Criar ambiente virtual

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configurar variáveis de ambiente

Cria um ficheiro `.env` local. Não faças commit de segredos.

Exemplo mínimo:

```env
SECRET_KEY=...
DEBUG=true
ALLOWED_HOSTS=127.0.0.1,localhost

DB_NAME=...
DB_USER=...
DB_PASSWORD=...
DB_HOST=...
DB_PORT=5432

REDIS_URL=redis://127.0.0.1:6379/0
EMAIL_PROVIDER=smtp
```

### 3. Validar e arrancar

```powershell
python manage.py check
python manage.py runserver
```

## Base de dados

O projeto é schema-first para a maioria das tabelas de negócio. Muitos modelos usam `managed = False`, por isso as migrations Django não são a fonte principal de verdade para alterações estruturais da base de dados.

Ficheiros relevantes:

| Ficheiro | Função |
| --- | --- |
| `sqlscript.sql` | Export schema-only atual da base de dados PostgreSQL configurada no Railway |
| `PROJECT_CONTEXT.md` | Mapa funcional e técnico atualizado do projeto |

Exportar novamente o schema:

```powershell
$env:PGPASSWORD = "..."
pg_dump --schema-only --no-owner --no-privileges `
  --host $env:DB_HOST `
  --port $env:DB_PORT `
  --username $env:DB_USER `
  --dbname $env:DB_NAME `
  --file sqlscript.sql
```

## Testes e validação

Validação base:

```powershell
python manage.py check
```

Suites focadas:

```powershell
python manage.py test apps.needs.tests apps.inventory.tests apps.marketplace.tests --keepdb
python manage.py test apps.orders.tests apps.recommendations.tests apps.alerts.tests --keepdb
python manage.py test apps.dashboard.tests apps.support.tests apps.messaging.tests --keepdb
```

Smoke test manual recomendado antes de deploy:

1. Criar ou atualizar um produto do produtor.
2. Registar stock atual e uma previsão de produção.
3. Criar um pedido externo de cliente.
4. Confirmar o plano temporal em `/necessidades/pedidos-clientes/`.
5. Publicar a necessidade gerada no marketplace.
6. Responder a essa procura com outro produtor.
7. Aceitar a proposta recebida.
8. Confirmar que a encomenda foi criada.
9. Confirmar que a página da proposta mostra a encomenda associada.
10. Confirmar que cobertura da necessidade e alertas foram atualizados.

## Deploy

Checklist Railway:

- Definir `DEBUG=false`, `SECRET_KEY`, `ALLOWED_HOSTS` e `CSRF_TRUSTED_ORIGINS`.
- Definir `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST` e `DB_PORT` para PostgreSQL.
- Definir `REDIS_URL` para Channels e cache de meteorologia.
- Definir `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY` e `CLOUDINARY_API_SECRET`.
- Definir `EMAIL_PROVIDER=resend`, `RESEND_API_KEY`, `DEFAULT_FROM_EMAIL`, `DEFAULT_REPLY_TO_EMAIL` e `SUPPORT_CONTACT_EMAIL`.
- Definir `APP_BASE_URL` com o URL público da aplicação.
- Aplicar alterações SQL manuais antes do deploy de código que dependa delas.
- Executar `python manage.py check`.
- Agendar ou executar `python manage.py sync_operational_alerts --apply`.

O `Procfile` arranca a aplicação como ASGI:

```Procfile
web: python manage.py collectstatic --noinput && daphne -b 0.0.0.0 -p $PORT config.asgi:application
```

Nota: `DEBUG` deve ser booleano (`true` ou `false`). Valores como `release` são inválidos para `python-decouple`. Em produção, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `APP_BASE_URL`, `DB_HOST` e `REDIS_URL` não usam defaults locais: o arranque falha se estiverem em falta.

## Documentação

- `PROJECT_CONTEXT.md`: contexto completo para desenvolvimento, relatório e continuidade entre sessões.
- `README.md`: visão técnica pública e guia de setup.
- `entrega/`: materiais finais e guia detalhado de configuração do Railway.
- `sqlscript.sql`: schema-only exportado da base de dados PostgreSQL configurada.
- `memory/`: notas temporárias locais, ignoradas pelo Git.

## Segurança

- Não commitar `.env`, tokens, passwords ou dados reais de produção.
- Validar ownership em needs, marketplace, orders, messaging, support e settings.
- Manter propostas privadas fora do feed público do marketplace.
- Bloquear edições em anúncios fechados e estados de proposta inválidos.
- Usar transações nas operações que sincronizam encomendas, reservas, stock, propostas e cobertura de necessidades.
- Auditar eventos críticos sem guardar passwords, tokens ou conteúdo privado desnecessário.
