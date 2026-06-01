# Guia Passo a Passo: Deploy no Railway

Este documento explica como colocar o VISION4FARMS a funcionar no Railway a
partir do repositório GitHub. Não incluir passwords, tokens ou chaves reais
neste ficheiro.

## 1. Componentes Utilizados

| Componente | Onde fica | Função |
| --- | --- | --- |
| Serviço web VISION4FARMS | Railway | Executa a aplicação Django com Daphne e serve pedidos HTTP e WebSockets |
| PostgreSQL | Railway | Guarda utilizadores, inventário, necessidades, marketplace, encomendas, mensagens e restantes dados operacionais |
| Redis | Railway | Suporta Django Channels para atualizações em tempo real e a cache de meteorologia em produção |
| Domínio público | Railway | Disponibiliza a aplicação por HTTPS |
| Tarefa agendada | Railway | Executa periodicamente a sincronização de alertas e anúncios expirados |
| Cloudinary | Serviço externo | Guarda fotografias e anexos fora do disco temporário do container |
| Resend | Serviço externo | Envia emails transacionais |

O disco local do container Railway não é usado para guardar uploads. Assim,
um novo deploy não elimina fotografias nem anexos.

## 2. Preparar Cloudinary

O Cloudinary é configurado antes do Railway porque serão necessárias três
credenciais.

1. Criar uma conta ou iniciar sessão em [Cloudinary](https://console.cloudinary.com/).
2. Abrir as definições do Product Environment.
3. Abrir a página `API Keys`.
4. Copiar estes valores:

```text
Cloud Name
API Key
API Secret
```

O `API Secret` é confidencial. Não o colocar no GitHub, em screenshots, no
relatório ou no vídeo.

### Como o projeto usa Cloudinary

O ficheiro `config/settings.py` configura:

```python
STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
    },
}

CLOUDINARY_STORAGE = {
    "CLOUD_NAME": config("CLOUDINARY_CLOUD_NAME"),
    "API_KEY": config("CLOUDINARY_API_KEY"),
    "API_SECRET": config("CLOUDINARY_API_SECRET"),
}
```

Isto faz com que o `default_storage` do Django envie ficheiros para o
Cloudinary. A aplicação usa esse storage para:

- fotografias de perfil;
- imagens dos anúncios do marketplace;
- anexos das mensagens;
- imagens anexadas a pedidos de suporte.

Não é necessário criar pastas manualmente no Cloudinary. A aplicação organiza
os caminhos ao guardar os ficheiros.

## 3. Preparar Resend

1. Criar uma conta ou iniciar sessão em [Resend](https://resend.com/).
2. Criar uma API key.
3. Configurar e verificar o domínio usado no endereço de envio.
4. Guardar a API key para adicionar posteriormente ao Railway.

Em produção, o projeto usa `EMAIL_PROVIDER=resend`.

## 4. Criar o Projeto Railway

1. Iniciar sessão em [Railway](https://railway.com/).
2. Criar um novo projeto vazio.
3. No Project Canvas, clicar em `+ New`.
4. Adicionar `Database` > `PostgreSQL`.
5. Voltar a clicar em `+ New`.
6. Adicionar `Database` > `Redis`.
7. Voltar a clicar em `+ New`.
8. Escolher `GitHub Repo`.
9. Selecionar o repositório do VISION4FARMS.
10. Dar ao serviço da aplicação um nome claro, por exemplo `vision4farms`.

Os nomes dos serviços PostgreSQL e Redis devem ser confirmados no Canvas. Nos
exemplos seguintes são usados os nomes `Postgres` e `Redis`.

## 5. Gerar o Domínio Público

1. Abrir o serviço `vision4farms`.
2. Abrir `Settings`.
3. Procurar `Networking` > `Public Networking`.
4. Clicar em `Generate Domain`.
5. Guardar o hostname gerado, por exemplo:

```text
vision4farms-production.up.railway.app
```

O Railway fornece HTTPS automaticamente para o domínio gerado.

## 6. Configurar as Variáveis

1. Abrir o serviço `vision4farms`.
2. Abrir o separador `Variables`.
3. Adicionar as variáveis abaixo.
4. Usar variáveis de referência para PostgreSQL e Redis. Assim, se uma
   credencial interna mudar, o serviço continua sincronizado.
5. Substituir apenas os valores entre `<...>`.

```env
DEBUG=false
SECRET_KEY=<gerar-uma-chave-longa-e-aleatória>

ALLOWED_HOSTS=${{RAILWAY_PUBLIC_DOMAIN}}
CSRF_TRUSTED_ORIGINS=https://${{RAILWAY_PUBLIC_DOMAIN}}
APP_BASE_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}

DB_NAME=${{Postgres.PGDATABASE}}
DB_USER=${{Postgres.PGUSER}}
DB_PASSWORD=${{Postgres.PGPASSWORD}}
DB_HOST=${{Postgres.PGHOST}}
DB_PORT=${{Postgres.PGPORT}}

REDIS_URL=${{Redis.REDIS_URL}}

CLOUDINARY_CLOUD_NAME=<cloud-name>
CLOUDINARY_API_KEY=<api-key>
CLOUDINARY_API_SECRET=<api-secret>

EMAIL_PROVIDER=resend
RESEND_API_KEY=<resend-api-key>
DEFAULT_FROM_EMAIL=VISION4FARMS <no-reply@dominio-verificado.pt>
DEFAULT_REPLY_TO_EMAIL=<email-de-suporte>
SUPPORT_CONTACT_EMAIL=<email-de-suporte>
```

O Railway injeta automaticamente a variável `PORT`; não é necessário criá-la.

As variáveis de branding são opcionais porque o projeto já inclui URLs por
omissão:

```env
BRAND_LOGO_COLOR_URL=<url-opcional>
BRAND_LOGO_WHITE_URL=<url-opcional>
BRAND_LOGIN_LOGO_WHITE_URL=<url-opcional>
BRAND_SIDEBAR_COMPACT_LOGO_URL=<url-opcional>
BRAND_FAVICON_URL=<url-opcional>
```

### Formato correto do domínio

| Variável | Formato |
| --- | --- |
| `ALLOWED_HOSTS` | Apenas hostname, sem `https://` |
| `CSRF_TRUSTED_ORIGINS` | URL completo, com `https://` |
| `APP_BASE_URL` | URL completo, com `https://` |

## 7. Definir o Comando de Arranque

1. Abrir o serviço `vision4farms`.
2. Abrir `Settings`.
3. Procurar a secção `Deploy`.
4. Definir este `Custom Start Command`:

```sh
python manage.py collectstatic --noinput && daphne -b 0.0.0.0 -p $PORT config.asgi:application
```

O projeto usa Daphne porque inclui Django Channels e WebSockets. Não substituir
este comando por Gunicorn sem rever a configuração ASGI.

## 8. Importar o Schema PostgreSQL

O projeto é maioritariamente `schema-first`: várias tabelas têm
`managed = False`. Por isso, `python manage.py migrate` não cria todas as
tabelas de negócio.

Numa base de dados PostgreSQL nova:

1. Confirmar que o cliente `psql` está instalado no computador.
2. Abrir o serviço `Postgres` no Railway.
3. Copiar o URL público da base de dados. O TCP Proxy deve estar ativo.
4. Na raiz deste repositório, executar:

```powershell
psql "<DATABASE_PUBLIC_URL>" -f .\sqlscript.sql
```

5. Confirmar no serviço `Postgres` que as tabelas foram criadas.

Não importar `sqlscript.sql` sobre uma base de dados já preenchida sem rever
primeiro o impacto.

## 9. Fazer Deploy

1. Rever as alterações pendentes no Railway.
2. Clicar em `Deploy`.
3. Abrir os logs do serviço `vision4farms`.
4. Confirmar que `collectstatic` termina sem erros.
5. Confirmar que Daphne fica ativo na porta fornecida pelo Railway.
6. Abrir:

```text
https://<dominio>/login/
```

## 10. Criar a Tarefa Agendada

O projeto inclui uma sincronização operacional para alertas e anúncios
expirados.

1. Criar um novo serviço Railway ligado ao mesmo repositório.
2. Configurar nesse serviço as mesmas variáveis necessárias à aplicação.
3. Definir o comando:

```sh
python manage.py sync_operational_alerts --apply
```

4. Configurar o serviço como Cron Job com a periodicidade pretendida.

## 11. Testes Depois do Deploy

Executar esta checklist:

1. Abrir `/login/` e confirmar HTTPS.
2. Iniciar sessão.
3. Criar ou consultar dados para validar PostgreSQL.
4. Alterar uma fotografia de perfil para validar Cloudinary.
5. Publicar um anúncio com imagem para validar Cloudinary no marketplace.
6. Enviar um anexo numa mensagem para validar Cloudinary e mensagens.
7. Abrir mensagens ou suporte em duas sessões para validar WebSockets e Redis.
8. Testar envio de email para validar Resend.
9. Executar manualmente `python manage.py sync_operational_alerts --apply`.

## 12. Diagnóstico Rápido

| Sintoma | Verificação |
| --- | --- |
| Erro no arranque sobre variáveis obrigatórias | Confirmar o separador `Variables` do serviço web |
| Erro de ligação ao PostgreSQL | Confirmar as referências `Postgres.PG*` e a importação de `sqlscript.sql` |
| Erro `DisallowedHost` | Confirmar `ALLOWED_HOSTS` sem `https://` |
| Erro CSRF após login ou formulário | Confirmar `CSRF_TRUSTED_ORIGINS` com `https://` |
| WebSockets não atualizam | Confirmar o serviço Redis e `REDIS_URL` |
| Uploads falham | Confirmar as três credenciais Cloudinary |
| Emails falham | Confirmar o domínio no Resend e `RESEND_API_KEY` |

## 13. Segurança

- Não commitar `.env`.
- Não incluir segredos em screenshots, relatórios ou vídeos.
- Usar `DEBUG=false` em produção.
- Gerar uma `SECRET_KEY` longa e aleatória.
- Considerar selar as variáveis secretas no Railway depois de confirmar o
  funcionamento.
- Avaliar HSTS apenas depois de confirmar o domínio HTTPS final. Ativar HSTS
  sem planeamento pode bloquear acessos HTTP futuros no browser.
- Rodar imediatamente qualquer chave exposta acidentalmente.

## Referências Oficiais

- [Railway: deploy de aplicações Django](https://docs.railway.com/guides/django)
- [Railway: variáveis e referências entre serviços](https://docs.railway.com/variables)
- [Railway: PostgreSQL](https://docs.railway.com/databases/postgresql/)
- [Railway: Redis](https://docs.railway.com/databases/redis)
- [Railway: comando de arranque](https://docs.railway.com/guides/start-command)
- [Cloudinary: integração com Django](https://cloudinary.com/documentation/django_integration)
- [Cloudinary: credenciais e API Keys](https://cloudinary.com/documentation/image_upload_api_reference)
