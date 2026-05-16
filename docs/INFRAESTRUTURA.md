# Infraestrutura — WiNS Hub

> Inventário operacional vivo. Atualizar **junto com qualquer PR** que mexa em infra
> (vide [§10 — Auto-tracking](#10--auto-tracking-como-funciona)).
> Última verificação: **2026-05-13**.

## Índice

1. [Stack tecnológica](#1--stack-tecnológica)
2. [Containers Docker](#2--containers-docker)
3. [Banco de dados (Postgres 16)](#3--banco-de-dados-postgres-16)
4. [Variáveis de ambiente](#4--variáveis-de-ambiente)
5. [Captadores + orchestrator](#5--captadores--orchestrator)
6. [Cron jobs (host)](#6--cron-jobs-host)
7. [APIs internas + integrações externas](#7--apis-internas--integrações-externas)
8. [Filesystem & paths](#8--filesystem--paths)
9. [Deploy & operações](#9--deploy--operações)
10. [Auto-tracking — como funciona](#10--auto-tracking-como-funciona)

---

## 1 · Stack tecnológica

| Camada       | Tecnologia                                                   |
| ------------ | ------------------------------------------------------------ |
| Linguagem    | Python 3.12-slim (container API)                             |
| Framework    | FastAPI 0.111.0 + uvicorn[standard] 0.29.0                   |
| Banco        | Postgres 16-alpine (`shared_buffers=768MB work_mem=32MB`)    |
| ORM/Driver   | psycopg2-binary 2.9.9 (sem ORM — SQL bruto com RealDictCursor) |
| Validação    | pydantic 2.7.1                                               |
| Auth         | pyjwt 2.10.1 + bcrypt 4.1.3                                  |
| HTTP         | httpx 0.27.0 + requests 2.32.4                               |
| Pagamento    | Mercado Pago + Stripe (somente clientes específicos)         |
| Email        | Resend API                                                   |
| LLM          | anthropic 0.49.0 (`claude-haiku-4-5-20251001` para extração) |
| Frontend     | Alpine.js 3 (CDN, sem build step) + CSS vanilla              |
| Rate-limit   | slowapi 0.1.9                                                |
| Proxy/CDN    | Nginx alpine (TLS via certbot)                               |
| CAPTCHA bypass | flaresolverr (containerizado, Cloudflare bypass)           |
| Web scraping | requests + httpx + **Playwright headless** (instalado dia 2 sprint mapeamento) |
| Validação BR | brutils 2.4.0 (CNPJ/CPF/CEP com DV)                          |
| Parsing PDF  | pdfplumber 0.11.9 (texto + tabelas + posição)                |
| Content extraction | trafilatura 2.0.0 (limpa HTML antes do Haiku)          |
| Parsing data | dateparser 1.4.0 (PT-BR linguagem natural)                   |

> `app/requirements.txt` é a fonte canônica. Manter sincronizado com este doc na seção §10.
> Catálogo de candidatas adicionais em [`docs/FERRAMENTAS_CANDIDATAS.md`](./FERRAMENTAS_CANDIDATAS.md).

## 2 · Containers Docker

Definido em [`docker-compose.yml`](../docker-compose.yml). 5 serviços ativos:

| Container               | Imagem                                  | Porta            | Função                                |
| ----------------------- | --------------------------------------- | ---------------- | ------------------------------------- |
| `wins_hub-db-1`         | `postgres:16-alpine`                    | 127.0.0.1:5432   | Banco principal                       |
| `wins_hub-api-1`        | build local (`./app/Dockerfile`)        | 8000 (interno)   | FastAPI (2 workers uvicorn)           |
| `wins_hub-nginx-1`      | `nginx:alpine`                          | 80, 443          | Reverse proxy + TLS                   |
| `wins_hub-flaresolverr` | `ghcr.io/flaresolverr/flaresolverr:latest` | 127.0.0.1:8191 | Cloudflare bypass para captadores    |
| `wins_metabase`         | `metabase/metabase:latest`              | 0.0.0.0:3001     | BI/dashboards internos                |

- **Volumes:** `postgres_data` (named volume) + bind mount `./app:/app` (hot edit, requer restart) + bind mount `/var/log/wins_hub`
- **Network:** `wins_net` (bridge)
- **Healthcheck:** db tem `pg_isready` a cada 10s; api depende `service_healthy`
- **Backups:** `docker-compose.yml.bak_*` em /root/wins_hub (gitignored)
- **Hot-reload:** NÃO. Editar `app/*.py` → `sudo docker restart wins_hub-api-1`

## 3 · Banco de dados (Postgres 16)

**51 tabelas** no schema `public`. Volume principal:

| Tabela                     |     Rows |
| -------------------------- | -------: |
| `matches_obra_prestador`   | 3.44 M   |
| `fornecedores`             | 2.66 M   |
| `obras`                    | 25.8 k   |
| `decisores_obra`           | 2.88 k   |
| `empresa_decisores_cache`  | 171      |
| `empresa_dominios`         | 71       |
| `prestadores`              | 6        |

### Tabelas-chave por domínio

- **Captação:** `obras`, `log_captacao`, `noticias_processadas`, `canais_cadastro_empresa`
- **Matchmaking:** `matches_obra_prestador` (com coluna `escopo` regional/nacional), `categorias_servico`, `setor_categorias`, `fornecedores`, `fornecedor_matches_summary` (MV)
- **Decisores:** `decisores_obra` (curados), `empresa_decisores_cache` (P3.1 search engines), `decisores_cache` (BrasilAPI sócios), `empresa_email_pattern_cache`, `decisor_jobs` (async P3.1 ao vivo)
- **CNPJ/Empresa:** `fornecedores` (Receita Federal), `empresa_dominios`, `cache_brasilapi`/`brasilapi_cache`, `empresa_intel` (subdomínios+tags), `empresa_dossier_cache`
- **Usuários/Wallet:** `prestadores` (com `creditos_ganhos`/`creditos_consumidos` em centavos), `prestador_empresas`, `desbloqueios`, `interacoes`, `pagamentos`, `comissoes`
- **CRM:** `contatos_log`, `leads_outbound`, `outreach_drafts`, `pipeline_obras_log`
- **Notificação:** `alertas_enviados`, `alertas_preferencias`, `newsletter_subscribers`, `acessos_log`, `password_resets`
- **CNAE/IBGE:** `cnae_oficial`, `municipios_ibge`, `municipios_rfb`

### Migrations

`/root/wins_hub/migrations/*.sql` — convenção do projeto trata `.sql` como **gitignored**. Migrations são aplicadas manualmente via `psql` e documentadas neste doc.

- `2026-05-10_camada3_decisores_cache.sql`
- `2026-05-10_camada4_email_validacao_cache.sql`
- `2026-05-10_stack_camadas12.sql`
- `2026-05-13_matches_escopo.sql` ← ALTER TABLE matches ADD COLUMN escopo

### Backup

`30 4 * * * /root/backup_diario.sh` (cron, fora do compose). Dump completo + retenção local.

## 4 · Variáveis de ambiente

Arquivo `.env` (gitignored). Nomes canônicos:

| Variável               | Uso                                                       |
| ---------------------- | --------------------------------------------------------- |
| `DB_NAME`/`DB_USER`/`DB_PASSWORD` | Postgres connection (container db)             |
| `JWT_SECRET`           | Assinatura de tokens (HS256, 24h)                         |
| `CRON_SECRET`          | Auth para endpoints disparados por cron                   |
| `ADMIN_TOKEN`          | Auth para /api/admin/* (querystring `?token=`)            |
| `STRIPE_SECRET_KEY`    | Pagamentos Stripe (cliente piloto)                        |
| `DOMINIO`              | Domínio público (`winshubcomercial.com.br`)               |
| `RESEND_API_KEY` / `RESEND_FROM` | Envio de email transacional + alertas            |
| `APP_URL`              | URL base para links em emails                             |
| `MP_MODE`/`MP_ACCESS_TOKEN`/`MP_ACCESS_TOKEN_TEST`/`MP_USE_SANDBOX` | Mercado Pago |
| `HUNTER_API_KEY`       | Hunter.io (decisor email enrichment, quota 50/mês)        |
| `SERPER_API_KEY`       | Serper.dev (search engines pra P3.1)                      |
| `ANTHROPIC_API_KEY`    | Claude Haiku 4.5 (extração de obra a partir de notícia)   |
| `INLABS_USER` / `INLABS_PASS` | Login inlabs.in.gov.br (DOU diário). Cadastro gratuito. |

> Não existe `.env.example` no repo hoje — criar é uma TODO conhecida.

## 5 · Captadores + orchestrator

Em `app/scripts/`:

| Script                              | Fonte                                       | Tipo técnico    |
| ----------------------------------- | ------------------------------------------- | --------------- |
| `captar_ibama.py`                   | IBAMA SISLIC (licenciamento federal)        | html_scraper    |
| `captar_bndes.py`                   | BNDES operações contratadas                 | html_scraper    |
| `captar_aneel.py`                   | ANEEL SIGA (geração + transmissão)          | rest_api (XLSX) |
| `captar_antaq.py`                   | ANTAQ terminais portuários                  | html_scraper    |
| `captar_anm.py`                     | ANM direitos minerários (CFEM)              | rest_api        |
| `captar_cvm.py`                     | CVM IPE (fatos relevantes B3)               | html_scraper    |
| `captar_cimm.py`                    | CIMM (notícias mineração)                   | rss             |
| `captar_agenciainfra.py`            | Agência iNFRA (RSS + WP API)                | rss             |
| `captar_noticias_setoriais.py`      | RSS multi-fonte + extração via Haiku        | rss + llm       |
| `captar_pncp_obras.py`              | PNCP — Concorrências (obras civis, mod 4+5) | rest_api        |
| `captar_pncp_consulta.py`           | PNCP — Manifestação de Interesse + Credenciamento (mod 10+12) | rest_api |
| `captar_pncp_defesa.py`             | PNCP — filtro órgãos militares (Marinha/Exército/Aeronáutica) | rest_api |
| `captar_dou_inlabs.py`              | DOU via InLabs (Imprensa Nacional) — DO3 + DO1 | rest_api + zip/xml + llm |
| `captar_eletrobras_ri.py`           | Eletrobras/Axia Energia RI (releases + fatos relevantes) | playwright + pdf + llm |
| `captar_anp.py`                     | ANP previsão investimentos exploratórios — CSV PTE upsertado em `obras` (fonte=`anp_pte`) via flag `--commit` | playwright + xlsx + csv |
| `captar_doe.py`                     | DOE/DOM multi-backend (querido_diario / requests_html / playwright_pdf) — CLI `--uf rj/mg/rs/pr` (sprint dia 4) | multi-backend + llm |
| `captar_dnit.py`                    | DNIT scaffold via gov.br/dnit (HTML scrape, links de notícias/licitação) | html_scraper |
| `captar_doe_sp.py`                  | DOE-SP via Base dos Dados — **SCAFFOLD**, requer GCP credentials | bd_sdk |
| `captar_google_alerts.py`           | Serper /news (10 queries industriais, últimas 24h) → `noticias_backlog_manual` (wired no orchestrator + botão admin) | serper + llm |

> Os 3 captadores PNCP compartilham helpers em `app/scripts/_pncp_common.py` (URL base,
> modalidades, `is_obra`, `is_orgao_defesa`, `record_para_dict_obra`, `inserir_obra_pncp`,
> validação CNPJ via brutils). Idempotente por `id_externo = "PNCP:<numeroControlePNCP>"`
> com `ON CONFLICT (id_externo) DO NOTHING`.
>
> **Captadores sprint dia 2:**
> - `captar_dou_inlabs.py` requer `INLABS_USER` + `INLABS_PASS` no `.env` (cadastro
>   gratuito em inlabs.in.gov.br). Edição diária ~115-160 MB, baixa só DO3 + DO1.
>   Filtros keyword + Haiku 4.5 (~$0.50-2.00/edição). id_externo = `DOU:<identifica>`.
> - `captar_eletrobras_ri.py` usa Playwright headless pra superar 403 anti-bot,
>   coleta releases trimestrais + fatos relevantes em PDF (Eletrobras → Axia Energia
>   rebrand 2026). pdfplumber + Haiku. id_externo = `AXIA:<sha1(url)[:16]>`.
> - `captar_google_alerts.py` (V2 16/05): RSS Google Alerts substituído por **Serper /news**
>   (RSS feeds devolviam HTTP 404 — Google Alerts não emite feed quando a entrega está
>   como Email). Mantém o nome do script por compatibilidade com orchestrator/admin/dedup.
>   10 queries (`QUERIES` no topo do script) espelham os Google Alerts originais:
>   "nova fábrica", "anuncia investimento", "greenfield", "BNDES aprova financiamento",
>   "Licença Prévia + complexo industrial", "Promon/AFRY vence contrato" etc.
>   Cada query: POST `https://google.serper.dev/news` com `gl=br`, `hl=pt`, `num=10`,
>   `tbs=qdr:d` (últimas 24h). Dedup por hash do link em
>   `noticias_backlog_manual.fonte_nome='google_alerts:<md5_link_16>'` (prefixo preservado
>   pra coabitar com qualquer registro RSS pré-existente). Haiku 4.5 filtra capex >= R$50mi
>   e rejeita opinião/M&A/lançamento de produto/notícia internacional. INSERT com
>   `status='pending_url'` (fila human review). **Wired** em `orchestrator.py CAPTADORES`
>   (janela 02:00 BRT) e em `RUN_CAPTADORES_MANUAL` do `main.py` (botão Forçar Atualização
>   em `/admin`). Requer `SERPER_API_KEY` no .env (já configurada).
>   Dry-run 16/05 12:35: **37 resultados Serper, 8 aprovados Haiku, 29 rejeitados**.
>
> - `captar_anp.py` (V9 14/05): além de XLSX scaffold (Agendas Antigas — irrelevante),
>   agora parseia o CSV PTE (`previsao-atividades-investimentos-pte.csv`, 261 linhas
>   agregadas por atividade × ambiente × etapa × ano). Com flag `--commit` upserta
>   em `obras` como agregadas macro: `fonte='anp_pte'`, `empresa='ANP - Previsão E&P'`,
>   `id_externo` determinístico — idempotente via `ON CONFLICT`. Dado NÃO acionável
>   pra prospecção (sem empresa/CNPJ por linha) — útil só pra dashboards de capex
>   setorial. Orchestrator chama sem flag (scaffold) — habilitar `--commit` no cron
>   se quiser persistir.

**Orchestrator** ([`app/scripts/orchestrator.py`](../app/scripts/orchestrator.py)):

```
ORCHESTRATOR (05:00 UTC = 02:00 BRT)
  ├─ for cada captador → sub-process com timeout 1h, parsing STATS_JSON
  ├─ MATCHMAKING (gated 02:00-07:00 BRT, obras modificadas no snapshot)
  ├─ DESCRICAO_SINTETICA (obras com desc < 200 chars, gated mesma janela)
  ├─ ENRICHMENT_DECISOR_TOP_OURO (top 5 OURO criadas hoje, P3.1 ao vivo, Hunter off,
  │                               cap 10min runtime, gated mesma janela)
  └─ INTEL_COMERCIAL (subdomínios + tags pra obras Ouro, gated mesma janela)
```

Convenção de STATS_JSON: cada captar_*.py registra `atexit` que emite linha final
`STATS_JSON: {"buscados": N, "novos": N, "erros": N}` lida pelo orchestrator.

### Utilidades de extração

- [`app/utils/parse_data_br.py`](../app/utils/parse_data_br.py) — wrapper sobre `dateparser`
  que normaliza formatos PT-BR informais antes de cair no parser:
  `"1º trimestre 2027"`, `"Q3 2026"`, `"1S2027"`, `"daqui a 6 meses"`,
  `"outubro/27"`, `"início/meados/fim de 2027"`. Usado em
  `captar_noticias_setoriais.py` para normalizar `prazo_inicio_operacao` retornado
  pelo Haiku — valor parseado é persistido em `noticias_processadas.raw_haiku_response`
  como `prazo_inicio_operacao_parsed` (ISO date string), pronto pra ser consumido
  por captadores/lógicas futuras sem re-parsing.

## 6 · Cron jobs (host)

`sudo crontab -l` no host. Servidor em **UTC** (BRT = UTC-3).

| Cron                | Comando                                       | O que faz                                    |
| ------------------- | --------------------------------------------- | -------------------------------------------- |
| `0 3,15 * * *`      | `/root/wins_hub/renew-cert.sh`                | Renovação Let's Encrypt                      |
| `0 2 * * 0`         | `cron_importar_receita.sh`                    | Importa CSVs Receita Federal (semanal)       |
| `0 5 * * *`         | `cron_orchestrator.sh`                        | **Orchestrator principal (02:00 BRT)**       |
| `30 3 * * *`        | `promover_pipeline_via_brasilapi.py --commit` | Promove Pipeline → Prata via BrasilAPI       |
| `0 8 * * 1`         | `alerta_semanal_cnae.py --commit`             | Email semanal alerta CNAE (seg 05:00 BRT)    |
| `*/30 * * * *`      | `alerta_realtime_obras.py --commit`           | Email realtime (a cada 30 min)               |
| `0 9 * * 1`         | `newsletter_semanal.py --commit`              | Newsletter semanal (seg 06:00 BRT)           |
| `0 7 * * 1-5`       | `crm_followup_creditos.py`                    | Follow-up CRM dias úteis                     |
| `30 0 * * *`        | `comissoes_diario.py --commit`                | Liberação comissões disponíveis (30d)        |
| `0 4 1 * *`         | `validate_csp.py`                             | Audit mensal violações CSP                   |
| `30 4 * * *`        | `/root/backup_diario.sh`                      | Backup completo DB                           |
| `0 4 * * 0`         | `VACUUM ANALYZE fornecedores`                 | Manutenção semanal (domingo 01:00 BRT)       |

Scripts shell em [`/root/wins_hub/scripts/`](../scripts/):
- `cron_orchestrator.sh` (entrypoint do orchestrator)
- `cron_importar_*.sh` (legados, substituídos pelo orchestrator desde 04/2026)
- `cron_importar_receita.sh` (ainda ativo, ETL Receita Federal)
- `cron_captar_noticias.sh` (legado)

## 7 · APIs internas + integrações externas

### Internas (FastAPI)

**~103 endpoints** declarados em `app/main.py` + 6 routers modulares em `app/routes/`:

| Router                      | Prefixo                                    | Função                                  |
| --------------------------- | ------------------------------------------ | --------------------------------------- |
| `prestadores.py`            | `/api/prestadores/*`                       | Endpoints prestador (matches, perfil)   |
| `cadastro_prestador.py`     | `/api/cadastro/*`                          | Onboarding wizard                       |
| `dashboard.py`              | `/api/dashboard/*`                         | Filtros, KPIs, mapa UF                  |
| `fornecedores.py`           | `/api/fornecedores/*`                      | Busca de fornecedores                   |
| `password_reset.py`         | `/api/auth/*`                              | Esqueci senha + reset                   |
| `auto_match_demo.py`        | `/auto-match-demo`                         | Página pública demo                     |
| `auto_match_real.py`        | `/api/auto-match/*`                        | Auto-match prestador→obras (wallet)     |
| `decisor_lookup.py`         | `/api/auto-match/decisor/*` + `/status/*`  | P3.1 ao vivo + polling (cache miss)     |

**Auth:**
- JWT 24h via `criar_token(pid, plano, is_representante)` (HS256)
- `requer_auth` dependency em rotas privadas
- `obter_usuario_completo` (DB lookup) onde a flag `is_representante` precisa ser fresca
- `_check_admin_token` (querystring) em `/api/admin/*`

### Externas

| Serviço          | Uso                                           | Quota / Custo                       |
| ---------------- | --------------------------------------------- | ----------------------------------- |
| Anthropic (Claude Haiku 4.5) | Extração estruturada de obra a partir de notícia + filtro decisor (Camada 5) | $$ pay-as-go |
| Hunter.io        | Email finder por domínio (P3.1 fallback)      | Free 50/mês (reset 07/06)           |
| Serper.dev       | Search engines (LinkedIn, CREA, DOU)          | $$$$ pay-as-go                      |
| Mercado Pago     | Pagamentos PIX/cartão (BR)                    | %                                   |
| Stripe           | Pagamentos (cliente piloto)                   | %                                   |
| Resend           | Email transacional + newsletters              | $$                                  |
| BrasilAPI        | Sócios + dados CNPJ                           | Grátis (rate limit)                 |
| Receita Federal  | Bulk CSVs (cron semanal)                      | Grátis                              |
| Let's Encrypt    | TLS certificate                               | Grátis                              |

## 8 · Filesystem & paths

```
/root/wins_hub/
├── docker-compose.yml          ← stack principal
├── docker-compose.dev.yml      ← dev override
├── renew-cert.sh               ← cron certbot
├── .env                        ← gitignored
├── CLAUDE.md                   ← instruções para Claude Code
├── DEBITO_TECNICO.md           ← TODO tech debt
├── app/
│   ├── Dockerfile              ← Python 3.12-slim
│   ├── main.py                 ← FastAPI app (~6500 linhas)
│   ├── requirements.txt        ← deps Python
│   ├── frontend/               ← index.html (SPA Alpine), login.html, etc
│   ├── routes/                 ← routers FastAPI
│   ├── services/               ← matchmaking, recon_intel, etc
│   ├── scripts/                ← captadores + orchestrator + crons admin
│   ├── sales_intelligence/     ← módulo P3.1 (camadas 1-5)
│   └── permissions.py          ← helpers rep × cliente × admin
├── scripts/                    ← shell scripts do host (cron entrypoints)
├── migrations/                 ← SQL migrations (gitignored .sql)
├── docs/                       ← documentação (este arquivo, mapeamento, etc)
├── nginx/conf.d/               ← config nginx
├── certbot/                    ← TLS state
├── data/                       ← ETL Receita Federal artifacts
├── deck_data/                  ← assets de apresentação
├── backups/                    ← backups locais
└── logs/                       ← logs aplicacionais
```

Logs do orchestrator + captadores: `/app/logs/` (dentro do container) e `/var/log/wins_hub/` (bind mount no host).

## 9 · Deploy & operações

### Edit → reload

1. Editar arquivo em `app/` (`main.py`, routes/, services/, frontend/).
2. `sudo docker restart wins_hub-api-1` (sempre — não há hot-reload).
3. `sudo docker logs wins_hub-api-1 --tail 30` para checar startup.

### Operações comuns

```bash
# Restart api
sudo docker restart wins_hub-api-1

# DB psql
sudo docker exec wins_hub-db-1 psql -U postgres -d wins_hub

# Rodar orchestrator manual
sudo docker exec wins_hub-api-1 python /app/scripts/orchestrator.py

# Rodar captador isolado
sudo docker exec wins_hub-api-1 python /app/scripts/captar_ibama.py

# Status admin
curl "http://localhost:8000/api/admin/dashboard?token=$ADMIN_TOKEN"

# Backup ad-hoc
sudo docker exec wins_hub-db-1 pg_dump -U postgres wins_hub | gzip > backup.sql.gz
```

### Convenção de backup pré-edit

Antes de editar arquivos críticos (main.py, index.html, matchmaking.py, etc), o time
cria um `.bak_pre_<contexto>_<timestamp>` no mesmo diretório (gitignored via `*.bak_*`).
Permite rollback rápido sem precisar de git checkout.

### Permissões

`/root/wins_hub/` é root-owned. Acesso via `sudo` (William está em grupo `sudo` + `wheel`).
Edições por usuários não-root requerem `sudo cp /tmp/... /root/wins_hub/...` ou
`sudo -u root tee ...`.

## 9.5 · ICP & Enrichment Pré-Launch

> Sprint dia 6 (Sessão 4) — entrega Top 500 fornecedores priorizados pra outbound.

**Query:** `scripts/extract_icp_top500.sql` — CTE que classifica obras em OURO/PRATA
via `cargo_decisor_keyword(nivel1_cargo)` + filtros, agrega matches por fornecedor,
score = `matches_ouro × 10 + matches_prata × 3`, top 500 por score.

Inputs do schema real:
- CNAEs sem separadores (`4120400`, não `4120-4/00`)
- `valor_estimado` na obras (não `capex_estimado_brl`)
- `m.cnpj` em matches_obra_prestador (não `cnpj_prestador`)
- `f.municipio_nome`, `f.telefone_1/_2`

**Enricher:** `app/scripts/enrich_icp_top500.py` — lê CSV ICP, para cada CNPJ chama
P3.1 (`descobrir_decisores` + `enriquecer_decisores_com_email` com Hunter habilitado),
persiste em `empresa_decisores_cache`. Idempotente (skip CNPJs com decisor <30d).
Cap defensivo: Hunter saldo < 100 → para. Smoke 13/05: 11 CNPJs em ~15min, ~75 Hunter
calls, custo ~$0.07 Haiku.

**Output pra Mari** (em `/tmp/outputs/`):
- `mari_icp_decisores_<YYYYMMDD>.csv` — 500 linhas, 17 colunas
- `mari_icp_decisores_<YYYYMMDD>.xlsx` — mesma data, header amarelo formatado
- Playbook outbound em `docs/PLAYBOOK_MARI.md`

**Top exemplos:** EMISSAO S/A (DF, score 2535, R$ 408 B capex, 17 UFs), EDP Smart
(SP, score 2345, R$ 185 B, 16 UFs).

## 10 · Auto-tracking — como funciona

Este documento (`docs/INFRAESTRUTURA.md`) deve ficar **sincronizado** com o estado real
da infraestrutura. Para garantir isso, um **git hook de pre-commit** bloqueia commits
que mexem em arquivos de infra sem atualizar este doc no mesmo commit.

### Arquivos rastreados

O hook bloqueia commits onde **qualquer** dos seguintes foi modificado e
`docs/INFRAESTRUTURA.md` **não** foi:

| Pattern                          | Razão                                                         |
| -------------------------------- | ------------------------------------------------------------- |
| `**/requirements*.txt`           | Mudança de dep Python → atualizar §1 (Stack) ou §7 (Externas) |
| `docker-compose*.yml`            | Mudança de container/volume/network → atualizar §2            |
| `.env.example`                   | Mudança de variável → atualizar §4                            |
| `**/crontab*`                    | Mudança em scheduler do host → atualizar §6                   |
| `**/scripts/captar_*.py`         | Captador novo/removido → atualizar §5                         |
| `**/scripts/cron_*.sh`           | Cron entrypoint novo/removido → atualizar §6                  |

### Onde fica

- **Hook versionado:** `scripts/git-hooks/pre-commit` (rastreado em git)
- **Install symlink:** `.git/hooks/pre-commit -> ../../scripts/git-hooks/pre-commit`
- **Script de instalação:** `scripts/git-hooks/install.sh` (cria o symlink + chmod +x)

### Como instalar (1ª vez ou após clonar)

```bash
sudo bash /root/wins_hub/scripts/git-hooks/install.sh
```

Idempotente — pode rodar repetidas vezes. Backup do hook anterior (se houver) em
`.git/hooks/pre-commit.bak_<timestamp>`.

### Override deliberado

Em casos pontuais (commit de correção de typo numa cron, refactor que não muda
semântica, etc), pode-se contornar com `--no-verify`:

```bash
sudo git -C /root/wins_hub commit --no-verify -m "..."
```

O hook **não** roda nesse caso. Use **só com justificativa clara**: o objetivo do hook
é forçar a manutenção do doc, não dificultar trabalho válido.

### Manutenção

- Sempre que adicionar **um padrão novo** (ex: novo `**/Dockerfile*`), editar
  `scripts/git-hooks/pre-commit` E esta tabela acima.
- Se a regra ficar restritiva demais (falso-positivo recorrente), preferir
  **refinar o pattern** a desativar o hook.
- Para validar o hook isoladamente:
  ```bash
  cd /root/wins_hub
  scripts/git-hooks/pre-commit
  echo "exit code: $?"
  ```

### Mensagem do hook (preview)

Quando bloqueado, o desenvolvedor vê algo como:

```
ERRO: arquivos de infraestrutura foram modificados mas docs/INFRAESTRUTURA.md NÃO.

Arquivos staged que afetam infra:
  - docker-compose.yml
  - app/scripts/captar_novo.py

Atualize docs/INFRAESTRUTURA.md no mesmo commit ou rode com --no-verify
  (apenas se tiver certeza que o doc continua refletindo a realidade).
```

---

> **Mantenedor:** atualizar este doc é responsabilidade de quem fez a mudança de infra.
> Em dúvida, abrir PR com a alteração + atualização do doc + revisão humana.
