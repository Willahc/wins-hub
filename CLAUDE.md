# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Big-picture architecture

WiNS Hub é uma plataforma B2B (FastAPI + Postgres + Alpine.js) que captura obras públicas/privadas de fontes oficiais (IBAMA, BNDES, ANEEL, ANTAQ, ANM, CVM), enriquece com decisores e cruza com o cadastro de prestadores. O frontend é uma SPA single-file (`app/frontend/index.html`) servida pelo Nginx; a API é uvicorn em container; o banco é Postgres 16 dedicado.

Stack rodando em produção (docker compose):

- `wins_hub-api-1`     — uvicorn em `:8000`, monta `./app` como `/app` (hot-reload é manual: `docker compose restart api`)
- `wins_hub-db-1`      — postgres 16, db `wins_hub`
- `wins_hub-nginx-1`   — TLS em `:443`, proxy para `api:8000`

Toda mudança de código Python ou SQL exige `docker compose restart api` antes de testar via `https://localhost/api/...`.

## Comandos comuns

```bash
# Restart da API após editar app/
docker compose -f /root/wins_hub/docker-compose.yml restart api

# Logs da API
docker compose -f /root/wins_hub/docker-compose.yml logs api --tail=50

# psql direto
docker exec -it wins_hub-db-1 psql -U postgres -d wins_hub

# Rodar orchestrator manualmente (dry-run)
docker exec wins_hub-api-1 python /app/scripts/orchestrator.py --dry-run

# Rodar 1 captador isolado
docker exec wins_hub-api-1 python /app/scripts/captar_ibama.py

# Backup rápido pré-mudança
docker exec wins_hub-db-1 pg_dump -U postgres wins_hub | gzip > /root/wins_hub_pre_$(date +%Y%m%d_%H%M%S).sql.gz
```

Não há suíte de testes. Validação é manual via curl + psql + UI.

## Critério oficial de Obra-Ouro / Ouro Parcial / Prata

Definido em três lugares que precisam ficar sincronizados (`app/main.py:_cargo_e_decisor` + `app/main.py:filtrar_obra` + função SQL `cargo_decisor_keyword(text)`):

| Tier            | Regra                                                                                | Flag retornado                |
|-----------------|--------------------------------------------------------------------------------------|-------------------------------|
| **Ouro completo** | `nivel1_nome` + `cargo_decisor_keyword(nivel1_cargo)` + (`nivel1_email` OU `nivel1_linkedin`) + `fonte_tipo != 'NOTICIA'` | `is_ouro=true` |
| **Ouro parcial**  | `nivel1_nome` + `cargo_decisor_keyword(nivel1_cargo)`, sem email nem LinkedIn, `fonte_tipo != 'NOTICIA'` | `is_ouro_parcial=true` |
| **Prata**         | `obra_score(obras) >= 80` E não é ouro nem ouro parcial E `fonte_tipo != 'NOTICIA'` | `is_prata=true` |

**Nota sobre `fonte_tipo`** (coluna em `obras`, CHECK `IN ('OFICIAL','NOTICIA','MANUAL')`): obras vindas de RSS/portal de notícias têm campos pouco estruturados (empresa/CNPJ/UF extraídos por regex heurístico) e ficam fora do funil Ouro/Prata até serem validadas manualmente. O filtro `OURO_SQL` em `app/main.py:347`, os endpoints `dashboard/ouro_count`/`prata_count`, `admin/ouro_parcial` e `admin/empresas_ouro_gaps` excluem `fonte_tipo='NOTICIA'`. Backfill da migration 012 marcou `PLANILHA`/`anderson_*` como `MANUAL`, resto como `OFICIAL`.

Score Python (`_obra_score`) espelha a função SQL `obra_score(obras)` — mantenha as duas em sincronia.

`cargo_decisor_keyword` faz match por substring (lowercased, sem acento) contra a tupla `CARGO_KEYWORDS_OURO` em `app/scripts/cargos_decisores.py`. Toda ampliação da lista exige atualizar:

1. `CARGO_KEYWORDS_OURO` em `app/scripts/cargos_decisores.py`
2. `CARGO_KEYWORDS_OURO` em `app/main.py`
3. Função SQL `cargo_decisor_keyword(text)` no banco

## Cargos decisores (validados por Anderson, 2026-04-30)

Anderson (sócio comercial) validou que **C-level (Diretor-Presidente, CEO) NÃO atende procurement** em megaprojetos — quem decide compra é a gerência média. Lista canônica em `app/scripts/cargos_decisores.py:CARGOS_DECISORES`:

- **Procurement:** Gerente/Coordenador de Compras, Gerente/Coordenador de Suprimentos, Supply Chain Manager
- **Engenharia técnica:** Engenheiro Mecânico, Engenheiro Civil, Gerente de Engenharia, Engenheiro de Projetos, Projetista
- **Operação/OPEX:** Coordenador de Manutenção, Gerente Industrial, Coordenador de Obras, Gerente de Projetos

Helpers no mesmo arquivo: `montar_query_websearch(empresa, uf)` para WebSearch padronizada e `CATEGORIA_DO_CARGO` para priorização em UI.

## Tabelas novas (além de `obras`, `prestadores`, `interacoes`)

Schema completo em `app/migrations/001_empresas_receita.sql`. Resumo das tabelas que toda funcionalidade nova de "matchmaking" e "grupo de empresas" toca:

### `grupo`
Holding / consórcio / SPE / grupo empresarial. Identifica que dois CNPJs distintos pertencem à mesma decisão econômica.
- `tipo CHECK IN ('CONSORCIO','GRUPO_EMPRESARIAL','HOLDING','SPE')`

### `cnpj_grupo` (M:N entre `empresas_receita.cnpj` e `grupo.id`)
Define o papel de cada CNPJ dentro do grupo.
- `papel CHECK IN ('CONSORCIO_LIDER','CONSORCIO_MEMBRO','HOLDING_PAI','GRUPO_OPERACIONAL','AMBOS')`
- `participacao_pct` opcional (consórcios)

### `fornecedor_meta`
Metadados de relacionamento por CNPJ (extensão de `empresas_receita`).
- `papel_wins_hub CHECK IN ('FORNECEDOR','CLIENTE','AMBOS')`
- `dominio_email`, `padrao_email` — base para inferência de email de decisor (ex.: `grupoagis.com.br` → `nome.sobrenome@grupoagis.com.br`)
- `grupo_id` opcional → liga ao `grupo`

### `decisores_obra`
Decisores encontrados via busca pública (LinkedIn, Escavador). Convive com `obras.nivel1_*` (decisor "principal" denormalizado para UI rápida). Suporta múltiplos decisores por obra, classificados por `tipo_cargo` (lista do Anderson).
- Campos: `nome`, `cargo` (livre), `tipo_cargo` (enum), `linkedin_url`, `email`, `telefone`, `fonte`, `registrado_por`, `excluido_em` (soft delete)
- `tipo_cargo` ∈ `{GERENTE_SUPRIMENTOS, GERENTE_COMPRAS, SUPPLY_CHAIN, ENGENHEIRO_MECANICO_CIVIL, GERENTE_ENGENHARIA, PROJETISTA, COORDENADOR_MANUTENCAO, GERENTE_INDUSTRIAL, COORDENADOR_OBRAS, GERENTE_PROJETOS, OUTRO}` (CHECK constraint)
- Constants `TIPO_CARGO_LABEL` e `TIPO_CARGO_ORDEM` em `app/routes/prestadores.py` controlam UI labels e ordenação
- Quando enriquecer o decisor principal: gravar **nas duas** estruturas — `decisores_obra` (registro auditável) e `obras.nivel1_*` (denormalizado, alimenta `is_ouro`)

### `enriquecimento_log`
Auditoria do enriquecimento automatizado via `POST /api/admin/decisores/enriquecer` e `POST /api/admin/decisores/inferir_emails`. Uma linha por campo alterado.
- Campos: `obra_id`, `decisor_id`, `decisor_nome`, `campo`, `valor_anterior`, `valor_novo`, `fonte`, `criado_em`

### `empresa_intel`
Inteligência comercial coletada via `hackertarget.com` (free tier público, sem key). Coletada pelo orchestrator diário.
- Campos: `cnpj`, `empresa`, `dominio`, `subdominios[]`, `tags[]`, `fonte`, `coletado_em`, `erro`
- Constraint: `UNIQUE(cnpj, dominio)` — UPSERT por chave composta
- `tags` derivadas por substring match em `services/recon_intel.py:SUBDOM_TAG_MAP` (ex.: `servicedesk` → `tem_itsm`, `escolavirtual` → `tem_treinamento_corporativo`). Ordem importa: tags específicas antes das genéricas
- Labels + pitch hint em `app/main.py:TAG_LABEL` (frontend só renderiza tags que estão nesse mapa)
- **Rate limit hackertarget é mais agressivo do que documentado** (~20-30 queries/IP/hora antes de retornar `http_429`/`api_limit_excedido`). Patch em `coletar_intel_empresa`: erros NÃO sobrescrevem dados válidos pré-existentes — só log; registro fica sem `coletado_em` e é retentado no próximo ciclo do orchestrator. Para uma corrida de 43 empresas, espalhar em vários dias é o normal.

## Matchmaking on-login (sob demanda)

Trigger no `POST /api/auth/login`: se o último `matches_obra_prestador.gerado_em` para o CNPJ do prestador for `NULL` ou > 7 dias, dispara `services.matchmaking.gerar_matches_para_prestador(prestador_id)` em `threading.Thread(daemon=True)`. A resposta inclui `matches_status: "gerando"|"pronto"`.

- Set guardado por lock em `_matches_em_geracao` evita duplicar disparo concorrente
- `gerar_matches_para_prestador` é diferente do `gerar_matches_para_obra`: faz uma única SQL bulk com CTEs (`prestador → cat_relev → obra_cat → scored → top_n`), score igual à fórmula do obra-side, **cap de `MAX_POR_PRESTADOR_ON_DEMAND = 500` matches** ordenados por score DESC, `ranking = 999` (placeholder; orchestrator semanal recalcula ranking real ao reprocessar a obra), `ON CONFLICT DO NOTHING` (preserva rankings reais já existentes)
- Frontend faz polling em `GET /api/matches/status` (Bearer JWT) a cada 3s. Banner animado `.wnshub-matches-banner` aparece na aba Matches enquanto status=gerando

## Endpoints admin (`ADMIN_TOKEN` em `.env`)

Usados por agentes remotos. Token único bearer comparado com `secrets.compare_digest`.

- `GET /api/admin/ouro_parcial?token=<ADMIN_TOKEN>&limit=50` — lista obras com `is_ouro_parcial=true` e sem email/linkedin. Ordena por `lead_score DESC, urgencia ASC`.
- `POST /api/admin/decisores/enriquecer` (header `Authorization: Bearer <ADMIN_TOKEN>`) — body `{obra_id, decisor_nome?, linkedin?, email?, fonte}`. Idempotente: nunca sobrescreve campo já preenchido. Atualiza `obras.nivel1_*` + `decisores_obra` (linha mais recente do mesmo nome) + insere em `enriquecimento_log`.
- `GET /api/admin/empresas_ouro_gaps?token=<ADMIN_TOKEN>&limit=50` — para cada empresa de obra-ouro, retorna `cargos_ja` (já cadastrados) e `cargos_faltantes` (lista do Anderson menos os já cadastrados). Ordena por `valor_max DESC`.
- `POST /api/admin/decisores/cadastrar` (Bearer) — body `{obra_id, nome, cargo, tipo_cargo, linkedin?, email?, telefone?, fonte, observacoes?}`. INSERT em `decisores_obra` com `tipo_cargo` validado. Idempotente por `(obra_id, tipo_cargo, lower(nome))` — retorna `status: 'criado'|'ja_existe'`.
- `POST /api/admin/fornecedor_meta/padrao_email` (Bearer) — body `{dominio_email, padrao_email, fonte, amostras?}`. Atualiza `padrao_email` em todos os CNPJs com mesmo `dominio_email`. Padrões válidos: `nome.sobrenome`, `nome_sobrenome`, `nomesobrenome`, `inicial.sobrenome`, `inicial_sobrenome`, `inicialsobrenome` (PRIO), `primeironome`, `outro`. Salva amostras em `observacoes` pra rastreabilidade.
- `POST /api/admin/decisores/inferir_emails?dry_run=1&limit=100` (Bearer) — para cada decisor sem email cuja empresa tem `dominio_email + padrao_email` cadastrados, gera email candidato pelo padrão. **`dry_run=1` (default) só retorna o plano**, não persiste. `dry_run=0` aplica e registra em `enriquecimento_log` com `fonte='inferencia_padrao:<padrao>|fonte_padrao:<origem>'`. Idempotente: só atualiza onde `email` ainda é NULL/''.
- `GET /api/admin/em_execucao_sem_decisor?token=<ADMIN_TOKEN>&limit=20` — alimenta a routine de WebSearch 4×/dia. Lista obras `fase='EM_EXECUCAO'` + `nivel1_nome IS NULL` + `fonte_tipo != 'NOTICIA'` + `visivel != false`, **excluindo** as que já têm registro em `enriquecimento_log` com `fonte LIKE 'WEBSEARCH_ROUTINE%'` (já processadas/esgotadas). Ordena por `lead_score DESC, urgencia ASC`.
- `POST /api/admin/obras/marcar_esgotada` (Bearer) — body `{obra_id, fonte}` (default `fonte='WEBSEARCH_ROUTINE_VAZIO'`). Insere linha em `enriquecimento_log` com `decisor_id=NULL, decisor_nome=NULL, campo='websearch_routine', valor_novo='esgotado'`. Idempotente por `(obra_id, fonte)`. Usado pela routine quando os 10 cargos da lista do Anderson esgotam sem resultado, pra evitar retry infinito da mesma obra.
- `GET /api/admin/decisores_anderson_sem_linkedin?token=<ADMIN_TOKEN>&limit=200` — alimenta o PASSO 0 da routine 4×/dia. Lista decisores `fonte LIKE 'anderson_csv%'` com `tipo_cargo` classificado (não NULL e não OUTRO) mas sem `linkedin_url`. Universo elegível ≈ 1.831 (todo decisor importado por CSV do Anderson não tem LinkedIn). Ordena por `lead_score DESC, urgencia ASC` — top da fila é sempre obra-ouro de alto valor.

## Página dedicada `/obra/{id}` (SPA)

- `GET /api/obras/{oid}/detalhe` (Bearer JWT, **STANDARD/PREMIUM** — 402 se GRATUITO) — single call que combina obra + decisores agrupados por `tipo_cargo` + intel comercial + fornecedores compatíveis (top 5 por categoria). Registra `interacoes(VISUALIZACAO)` ao acessar.
- Frontend usa SPA routing puro: catch-all `@app.get("/{path:path}")` serve `index.html` para `/obra/{uuid}`. Alpine `init()` inspeciona `window.location.pathname`, define `rota = 'dashboard'|'obra-detail'`. `popstate` listener trata back/forward; `pushState` em `abrirPaginaObra(id)`. Voltar via `voltarParaDashboard()` (history.back se possível, senão pushState pra `/`).
- Modal antigo virou **preview leve** para GRATUITO: nome, 3 KPIs, necessidades, CTA "Ver detalhes completos →" (gating: GRATUITO → modal de upgrade; STANDARD/PREMIUM → navega para `/obra/{id}`).
- Página tem ordem: header → KPIs → descrição → necessidades → **decisores** → **intel comercial** (depois dos decisores, com subtítulo "Use essas informações para personalizar sua abordagem comercial antes de ligar") → fornecedores compatíveis.

## Responsividade mobile

CSS em `<style id="wnshub-page-obra-style">`:
- `@media (max-width: 768px)`: kpi-grid 2 cols, último centralizado quando ímpar; cards de obra full-width; modal e página em fullscreen (sem padding lateral); fonte base 14px; touch targets 44px mínimo; `.logo-sub` oculto; `.wnshub-actions` em coluna com botões 100% width
- `@media (max-width: 480px)`: kpi-grid 1 col na página dedicada
- **Drawer mobile**: sidebar de filtros (`.wnshub-filtros-sidebar`) vira drawer fixed. `body.wnshub-drawer-open` controla. Alpine state `sidebarFiltrosAberta` (toggled por `.wnshub-drawer-toggle` button + overlay click + Escape + watchers em `tab`/`rota` resetam pra false). **Atenção**: regra legada `@media (max-width: 900px)` em `.wnshub-filtros-sidebar { position: static }` exige `!important` na regra mobile pra vencer cascade.
- `[x-cloak]` regra global pra evitar flash de conteúdo antes do Alpine inicializar.

## Endpoint de intel comercial

- `GET /api/empresas/{cnpj}/intel` (qualquer plano logado) — retorna `subdominios + tags`. GRATUITO vê tags sem `pitch` hint; STANDARD/PREMIUM vê pitch (texto que sugere ângulo de abordagem comercial).

## Routines remotas agendadas

- **WiNS Hub — Enriquecimento Ouro Parcial** (`trig_01P1DkbMRZYcVReLdb7vwycX`)
  - Cron: `0 6 * * 0` UTC = domingo 03:00 BRT (dentro da janela do orchestrator)
  - Agente remoto chama `GET /api/admin/ouro_parcial`, faz WebSearch por `"<decisor>" "<empresa>" site:linkedin.com`, valida critérios (`/in/`, nome bate, cargo/empresa no snippet), faz `POST /api/admin/decisores/enriquecer`. Não inventa emails.
  - Painel: https://claude.ai/code/routines/trig_01P1DkbMRZYcVReLdb7vwycX

- **WiNS Hub — Sweep cargos Anderson (one-time)** (`trig_01PNC6UCDveeh6MYgbGjRCL8`)
  - One-shot que dispara em 2026-05-01T12:25:14Z
  - Para cada empresa de obra-ouro, busca decisores em todos os 10 cargos da lista do Anderson via `GET /api/admin/empresas_ouro_gaps` + WebSearch site:linkedin.com OR site:escavador.com, filtra `/in/` + nome plausível, persiste via `POST /api/admin/decisores/cadastrar` com `tipo_cargo`. Idempotente — re-arm seguro.
  - Painel: https://claude.ai/code/routines/trig_01PNC6UCDveeh6MYgbGjRCL8

- **WiNS Hub — WebSearch decisores 4×/dia** (`trig_015p4QBFEn2AhaQtMgR64QSj`)
  - Cron: `0 3,9,15,21 * * *` UTC = 00, 06, 12, 18 BRT. Roda fora da janela canônica 02-07 BRT do orchestrator porque só escreve em `decisores_obra` + `enriquecimento_log` (não dispara matchmaking).
  - Executa **2 passos em sequência** em cada execução:
    - **PASSO 0 — LinkedIn dos decisores Anderson (até 200/exec):** `GET /api/admin/decisores_anderson_sem_linkedin` → para cada decisor faz WebSearch `"<nome>" "<empresa>" site:linkedin.com`, valida `/in/` + nome bate, e enriquece via `POST /api/admin/decisores/enriquecer` com `fonte='WEBSEARCH_ROUTINE_LINKEDIN'`. Decisor sem match fica pra próxima rodada (não marca esgotado).
    - **PASSO 1 — Obras EM_EXECUCAO sem decisor (até 20/exec):** `GET /api/admin/em_execucao_sem_decisor` → percorre os 10 cargos canônicos do Anderson via WebSearch `"<empresa>" "<uf>" (<cargo>) site:linkedin.com OR site:escavador.com.br`, valida `nome ≥ 2 palavras` + `/in/`, cadastra via `POST /api/admin/decisores/cadastrar` com `fonte='WEBSEARCH_ROUTINE:<query>'`. Quando 10 cargos esgotam sem match, marca obra via `POST /api/admin/obras/marcar_esgotada` (`fonte='WEBSEARCH_ROUTINE_VAZIO'`).
  - Limites totais: ~400 WebSearches/exec (200 + 200), ~25 min. Aborta em 401, retry com backoff em 429.
  - Painel: https://claude.ai/code/routines/trig_015p4QBFEn2AhaQtMgR64QSj

## Orchestrator e janela horária

Cron host: `0 5 * * * /root/wins_hub/scripts/cron_orchestrator.sh` (dispara `docker exec wins_hub-api-1 python /app/scripts/orchestrator.py`).

⚠️ **Servidor está em UTC** (`date` retorna UTC, Postgres `SHOW TIME ZONE` = UTC). Cron usa hora UTC. Para rodar **02:00 BRT** (= UTC-3, sem horário de verão desde 2019), o cron precisa ser `0 5 * * *` UTC. Versões antigas usavam `0 2` UTC = 23:00 BRT do dia anterior, fora da janela do matchmaking — bug histórico documentado em log_captacao com `MATCHMAKING: pulado` por meses.

Janela canônica BRT: **02:00 ↦ 07:00** (`JANELA_FIM_HORA = 7` em `app/scripts/orchestrator.py`). Após 07:00 BRT o matchmaking e o populador de descrição sintética são **pulados** (não os captadores — esses sempre rodam).

Sequência fixa:

1. **Captadores** (1h timeout cada, ordem importa):
   - **OFICIAL** (CSV/JSON/XLSX estruturado, `fonte_tipo='OFICIAL'`): `captar_ibama` → `captar_bndes` → `captar_aneel` → `captar_antaq` → `captar_anm` → `captar_cvm`
   - **NOTICIA** (RSS/WordPress API, `fonte_tipo='NOTICIA'` — excluído de `is_ouro` até validação manual): `captar_cimm` → `captar_agenciainfra`
2. **Matchmaking** — só se hora atual BRT < 07:00. Dispara `services.matchmaking.gerar_matches_para_obra` para o conjunto `obras_modificadas_desde(snapshot_utc)` (novas + atualizadas via `obras_atualizacoes_log`)
3. **DESCRICAO_SINTETICA** — re-popula `obras.descricao` quando `LENGTH(descricao) < 200`, usando `scripts.sintetizador.gerar_descricao`. Mesma janela do matchmaking.
4. **INTEL_COMERCIAL** — `services.recon_intel.coletar_intel_obras_ouro(max_idade_dias=7)` chama hackertarget para cada empresa de obra-ouro com `dominio_email` cadastrado, persiste em `empresa_intel`. Mesma janela.

Cada etapa grava 1 linha em `log_captacao` com `fonte='ORCHESTRATOR'|'captar_xxx'|'MATCHMAKING'|'DESCRICAO_SINTETICA'|'INTEL_COMERCIAL'` e `status='sucesso'|'erro'|'pulado'`.

Lock por PID em `/tmp/wins_hub_orchestrator.lock` (cron wrapper). Logs em `/var/log/wins_hub/orchestrator_*.log`, cleanup automático >30 dias.

Tarefas agendadas pelo agente Claude (`/schedule`) que rodem fora desta janela devem ser explícitas sobre por que estão fora — as 02:00–07:00 são a janela em que o sistema está "quente" e seguro para escrever.

## Padrão de deploy (base64 via SSH)

Edição direta em `/root/wins_hub/app/...` no host (não há repo Git remoto sincronizado na VPS). Para mudanças vindas de fora (notebook → VPS), o padrão é:

```bash
# 1. No notebook: codifica o arquivo
base64 -w0 novo_arquivo.py > novo_arquivo.py.b64

# 2. Envia via SSH e decodifica atomicamente no destino
ssh root@<vps> "base64 -d > /root/wins_hub/app/scripts/novo_arquivo.py.tmp \
  && mv /root/wins_hub/app/scripts/novo_arquivo.py{.tmp,}" < novo_arquivo.py.b64

# 3. Restart da API
ssh root@<vps> "docker compose -f /root/wins_hub/docker-compose.yml restart api"
```

Por quê base64: evita problemas de escaping de aspas, crases e caracteres unicode quando o conteúdo passa por shell intermediário. O `mv` atômico evita arquivo meio-escrito sendo lido pelo uvicorn em hot-reload.

Sempre fazer backup antes de mudar `app/main.py` ou migrations:

```bash
cp /root/wins_hub/app/main.py /root/wins_hub/app/main.py.bak_pre_$(date +%Y%m%d_%H%M%S)
```

Backups SQL pré-deploy ficam em `/root/wins_hub_pre_*.sql.gz` ou `/root/wins_hub_backups/`.

## CONVENÇÃO DE SESSÕES (Nível 4)

### Tipo A — Investigação (read-only)
- Apenas SELECTs, pesquisa web, análise
- Pode ser longa (2h+)
- Declarar no início: "sessão Tipo A"
- Antes de qualquer escrita: encerrar e abrir sessão Tipo B

### Tipo B — Execução (escrita)
- Uma transação por sessão (BEGIN...COMMIT)
- Encerra após COMMIT — nunca misturar com investigação
- Declarar no início: "sessão Tipo B — escopo: <briefing>"
- SEMPRE rodar health check antes do primeiro briefing

## /COMPACT — QUANDO USAR (Nível 5)
- Sessão Tipo A com mais de ~2h de contexto
- Sinal: Code repete erros já corrigidos na mesma sessão
- Regra: rodar /compact ANTES de qualquer briefing de escrita em sessão longa
- /compact não apaga backups nem estado do DB — só comprime contexto do Code
