# CLAUDE.md — WiNS Hub Comercial
# Instruções canônicas para Claude Code
# Atualizado: 31/05/2026 | Founder: William Nunes da Silva

---

## REGRAS DE WORKFLOW (SEMPRE SEGUIR)

### Ciclo obrigatório
1. **Chat investiga + decide** → 2. **Code executa** → 3. **Chat analisa resultado** → 4. **Próxima execução**
- Chat é grátis. Code consome quota. NUNCA mandar pro Code sem decisão tomada no chat.
- **Pesquisar antes de rejeitar** — obra que parece duplicata/lixo pode ser a mais valiosa (Arauco R$1bi→R$25bi).
- **Investigar antes de gravar** — nunca INSERT/UPDATE sem SELECT confirmando o estado atual.

### Checkpoints obrigatórios
- SEMPRE fazer backup antes de qualquer escrita: `pg_dump ... > /home/william/backups/<contexto>_<YYYYMMDD>/pre_<acao>.sql`
- PARAR após cada SELECT e mostrar resultado antes de qualquer escrita
- PARAR se resultado inesperado — nunca continuar no escuro
- Transação única por briefing: `BEGIN; ... COMMIT;` com rollback se qualquer etapa falhar

### Health check (rodar no início de cada sessão)
```sql
SELECT classificacao_computed, COUNT(*), ROUND(SUM(valor_estimado)/1e9,1) cap_bi
FROM obras WHERE motivo_invisivel IS NULL GROUP BY classificacao_computed ORDER BY 2 DESC;

-- Cron noturno (vitrine principal):
SELECT COUNT(*) matches, MAX(gerado_em) ultimo FROM matches_obra_prestador;
-- Standalone/manual (intel rica / score_breakdown):
SELECT COUNT(*) matches_v2, MAX(gerado_em) ultimo_v2 FROM matches_v2;

-- Hunter saldo (via API, não DB):
-- curl -s "https://api.hunter.io/v2/account" -H "Authorization: Bearer $HUNTER_API_KEY" \
--   | python3 -c "import sys,json; d=json.load(sys.stdin)['data']; print(d['credits'], d['searches'], d['verifications'])"
-- schema atual: data.credits | data.searches | data.verifications
-- NUNCA usar data.calls nem data.requests.email_finder (schemas depreciados)
```

---

## INFRA E ACESSO

```
VPS: srv1617037 (Hostinger)
Containers: wins_hub_v2-api-1 (PRODUÇÃO), wins_hub-db-1 (postgres)
DB: postgres user=wins_app db=wins_hub (PG16)
Working dir: /root/wins_hub
Admin: williamvnvn@gmail.com / WiNS2026!
Site: winshubcomercial.com.br
Repo: Willahc/wins-hub (backup cron ativo)
```

### Comandos padrão
```bash
# Entrar no DB
docker exec -it wins_hub-db-1 psql -U wins_app -d wins_hub

# Restart API
docker restart wins_hub_v2-api-1

# Logs
docker logs wins_hub_v2-api-1 --tail=50

# Backup padrão
pg_dump -U wins_app -d wins_hub -F c -f /home/william/backups/<contexto>/pre_<acao>.sql
```

---

## REGRAS CRÍTICAS DE API E DADOS

### Hunter API
- **BEARER OBRIGATÓRIO**: `Authorization: Bearer $HUNTER_API_KEY` (NÃO `?api_key=` — depreciado, retorna 502)
- Saldo: `data.credits` (total) + `data.searches` (email finder) + `data.verifications` (verify) — schema novo 2026
  - **NUNCA usar** `data.calls` (depreciado) nem `data.requests.email_finder` (schema antigo)
- Reset: dia 11 de cada mês às 03:20 UTC
- Plano Starter: 2.000 Email Finder + 4.000 verifications = 6.000 ops/mês
- **Domínio OBRIGATÓRIO antes de Hunter**: validar via web_search antes de gastar crédito
  - Se `empresa_dominios` é NULL → descobrir domínio primeiro
  - Se domínio veio de descoberta automática (E2_agressivo, E3_domain_search) → validar antes de Domain Search

### Matchmaker — arquitetura real (CRÍTICO)
- **V1 (cron noturno):** `services/matchmaking.py` → `matches_legacy` (lido via VIEW `matches_obra_prestador`) — é o que alimenta a vitrine principal
- **V2 (standalone/manual):** `matchmaker_worker.py` → `matches_v2` — usado on-demand; tem `score_breakdown` JSON (intel rica); não é usado pelo cron
- **Vitrine principal** lê `matches_obra_prestador` (26+ hits em routes/ e main.py) ✅
- **Features secundárias** (score_breakdown, explicação de match) leem `matches_v2` diretamente — stale quando V2 não roda
- **Fix de performance** (porte adaptativo, regressão 234k) → vai em `services/matchmaking.py`, NÃO em `matchmaker_worker.py`
- **NÃO rodar --full** no matchmaker_worker.py até diagnosticar regressão 34×

### Enrichment cascade
Ordem: CNPJ (BrasilAPI) → Domain → LinkedIn (Serper, 2 queries mínimo) → Email (Hunter) → Phone
- `enrichment_auto_job.py` tem o fix de Bearer (4 callsites corrigidos em 31/05/2026)

---

## SCHEMA — TABELAS CRÍTICAS

### obras
```sql
-- Campos chave:
id uuid PK
empresa text          -- nome da empresa proponente (NÃO empresa_nome)
nome text             -- título/nome da obra (NÃO titulo)
setor text            -- ENUM canônico (ver SETORES VÁLIDOS)
uf char(2)
municipio text
fase text             -- ENUM (ver FASES VÁLIDAS)
valor_estimado numeric
classificacao_computed text  -- OURO/PRATA/BRONZE/PIPELINE/NULL (NÃO tier_classificacao)
motivo_invisivel text        -- NULL = visível; populado = invisível
fonte text
fonte_tipo text       -- CONSTRAINT: só 'OFICIAL', 'NOTICIA', 'MANUAL' (PESQUISA_MANUAL não existe!)
status_licenca text
validacao_obra_at timestamptz
observacoes_validacao text
```

### Tiers (classificacao_computed)
- **OURO**: dados completos + ≥1 decisor com Nome + LinkedIn + email verificado
- **PRATA**: dados + CAPEX, decisor ausente ou parcial
- **BRONZE**: CAPEX validado, sem decisor
- **PIPELINE**: programa guarda-chuva ou intenção sem obra física confirmada

### Fases válidas (obras.fase)
`PLANEJAMENTO | EM_EXECUCAO | LICITACAO_ABERTA | LICENCA_INSTALACAO | LICENCA_PREVIA | OPERACAO | PROJETO | PIPELINE | CONCLUIDA | NULL`

### Setores canônicos (setor_cnae_compatibility)
`ENERGIA | INFRAESTRUTURA | PETROLEO_GAS | MINERACAO | SANEAMENTO | LOGISTICO | INDUSTRIAL | PORTUARIO | SUCROENERGETICO | PAPEL_E_CELULOSE | ALIMENTOS_E_BEBIDAS | LATICINIOS | AGROINDUSTRIAL | AUTOMOTIVO_E_AUTOPECAS | QUIMICA | TECNOLOGIA | AGRO`
- **NUNCA usar** `AUTOMOTIVO` (sem _E_AUTOPECAS) — zero matches
- **NUNCA usar** `PAPEL_CELULOSE` (sem _E_) — zero matches
- **NUNCA usar** `ALIMENTOS_BEBIDAS` (sem _E_) — zero matches
- `SIDERURGIA_METALURGIA` — no prompt Haiku mas SEM CNAEs no SCC → rejeitar até mapear

### setor_cnae_compatibility
```sql
-- 133 rows, todas populadas (0 vazias em 31/05/2026)
-- Truncamento em fases_aplicaveis → zero matches silencioso (bug histórico)
-- SEMPRE usar array_replace pra corrigir, nunca UPDATE direto que corta
-- Regra de generosidade: cedo é barato (aparecer cedo = ok), tarde é fatal (perder negócio)
```

### matches_v2
```sql
-- Filtros silenciosos do worker:
-- WHERE classificacao_computed IN ('OURO','PRATA','BRONZE','PIPELINE')  → exclui NULL tier
-- AND (COALESCE(fonte_tipo,'OFICIAL') != 'NOTICIA' OR validacao_obra_at IS NOT NULL)
-- Fix aplicado 31/05/2026: obras NOTICIA validadas por humano agora passam
```

---

## REGRAS DE NEGÓCIO — CAPEX

### Erro nº1: capex inflado (NUNCA aceitar sem validar)
| Caso | Capex errado | Capex correto |
|---|---|---|
| Alibaba | R$275bi (global) | NULL (BR não divulgado) |
| Toyota Sorocaba | R$11bi (programa Brasil 2030) | R$5bi (fábrica Sorocaba) |
| Ascenty | R$30bi (imprensa local) | R$6bi (obra física) |
| Arauco | R$1bi (card) | R$25bi (projeto Sucuriú real) |
| Guofuhee | R$1bi (notícia) | R$202mi (real) |
| Petrobras SE | R$72,5bi (programa estadual) | PIPELINE |
| Petrobras SP | R$37bi (programa 2026-2030) | PIPELINE (Replan R$6bi à parte) |

### Regras de capex
- Imprensa local agrega/infla — sempre buscar fonte primária (governo/CVM/empresa)
- "Investimentos" no plural + anúncio político + sem ativo nomeado = guarda-chuva → PIPELINE
- Ativo nomeado + capacidade definida + gestor técnico citado = obra real → cadastrar
- Capex NULL é melhor que capex errado — usar `observacoes_validacao` pra explicar

---

## REGRAS DE NEGÓCIO — DEDUP

### Cross-dedup OBRIGATÓRIO antes de qualquer INSERT
```sql
-- Checar empresa + UF em obras existentes
SELECT id, empresa, nome, uf, valor_estimado, fase, motivo_invisivel
FROM obras
WHERE immutable_unaccent_lower(empresa) ILIKE immutable_unaccent_lower('%<empresa>%')
  AND uf = '<UF>';

-- Checar candidatos_industrial
SELECT id, empresa, uf, status FROM candidatos_industrial
WHERE immutable_unaccent_lower(empresa) ILIKE '%<empresa>%';
```

### Padrões de duplicata conhecidos
- Mesmo contrato visto por dois lados (contratante vs vencedor da licitação) → 2 cards, 1 obra
- Notícia local agrega anúncios → capex inflado + mesmo ativo
- Google Alerts repete a mesma URL em janela qdr:w → dedup por fonte_nome UNIQUE protege
- Programa guarda-chuva capturado como obra → PIPELINE + marker

### Qual canônica manter
Prioridade: OFICIAL > MANUAL > NOTICIA; dados completos > incompletos; fase correta > NULL

---

## REGRAS DE NEGÓCIO — CAPTADORES

### google_alerts (`/app/scripts/captar_google_alerts.py`)
- Janela: `qdr:w` (semana), cron diário 05:00 UTC
- Grava em: `noticias_backlog_manual` (fila de aprovação humana)
- Endpoint admin promover: `POST /api/admin/noticias/promover`
  - **Normaliza setor** automaticamente via `_SETOR_MAP_PROMPT_TO_DB` (fix 31/05/2026)
  - **Infere fase** via heurística de keyword (fix 31/05/2026)
  - **Cross-dedup** vs obras: 409 se exato, warning se parcial (fix 31/05/2026)
  - `status_licenca='NOTICIA'` → gravado como NULL (fix 31/05/2026)

### industrial_priv (`/app/scripts/captar_industrial_priv.py`)
- Estágio 1: operacional (descoberta → `candidatos_industrial`)
- Estágio 2: pendente (processar → INSERT em `obras`)
- Modos: `--descoberta` (busca) / `--dry-run` (simula) / `--processar` (Estágio 2, não implementado)
- SETOR_MAP canonicaliza Haiku → enum DB
- Cross-dedup vs obras por `immutable_unaccent_lower(empresa)+uf`
- Anti-inflação: `capex_suspeito` → valor_estimado=NULL + flag

### Validação de domínio ANTES de Hunter (regra canônica 08/05)
1. Testar "Grupo X"/"Holding X" antes de descartar SPV
2. CAPEX >R$500M = operação real existe, buscar até achar
3. SPV → decisores estão na holding
4. CNPJ-FIRST sempre: extrair CNPJ literal do DB antes de pesquisar (siglas são ambíguas)
5. Imprensa CAPEX ≠ matchmaking CAPEX — usar só o escopo que a empresa contrata

---

## REGRAS DE NEGÓCIO — MATCH POR FASE

### Regra de ouro
- **Cedo é barato** (fornecedor ignora): aparecer no PLANEJAMENTO de uma obra é ok
- **Tarde é fatal** (negócio perdido): não aparecer em EM_EXECUCAO é catastrófico
- Janela generosa > filtro rígido — ampliar arrays, não restringir

### Camada de timing (implementada 31/05/2026)
- `obra_janela_score()` já calcula score 0-100 por fase+data+status_licenca+capex
- `timing` no payload (top-level, GRATUITO): `{bucket, janela_score, mensagem, status_licenca_raw}`
- Buckets: HOT≥80, WARM≥50, STEADY≥30, COLD<30
- Fallback timing=null: status_licenca IS NULL, '', ou 'NOTICIA'
- **Sinal (timing) é gratuito** → cria urgência; **contato (decisor) é pago** → entrega

### Gaps de fase conhecidos (pós-fix 31/05/2026)
- Todos os 133 arrays populados, generosidade ampliada (Regras A/B/C/D aplicadas)
- Zoomlion: AUTOMOTIVO_E_AUTOPECAS×PLANEJAMENTO ainda sem match (baixo peso, 0 candidatos)
- SIDERURGIA_METALURGIA: sem CNAEs no SCC → obras desse setor geram 0 matches

---

## PADRÕES SQL OBRIGATÓRIOS

```sql
-- SEMPRE usar immutable_unaccent_lower para texto (não ILIKE simples):
WHERE immutable_unaccent_lower(empresa) ILIKE immutable_unaccent_lower('%toyota%')
-- obras: campo é "empresa" (proponente) + "nome" (título da obra)

-- Validar CNPJ antes de qualquer uso:
SELECT cnpj_valido('12345678000195');  -- função SQL nativa wins_hub

-- Invisibilizar obra (nunca DELETE):
UPDATE obras SET visivel=false, motivo_invisivel='<motivo>_<YYYYMMDD>' WHERE id='<uuid>';

-- Verificar impacto antes de invisibilizar:
SELECT obra_id, COUNT(*) matches FROM matches_v2 WHERE obra_id='<uuid>' GROUP BY obra_id;

-- Ampliar fases_aplicaveis (nunca sobrescrever):
UPDATE setor_cnae_compatibility
SET fases_aplicaveis = (SELECT array_agg(DISTINCT f) FROM unnest(fases_aplicaveis || ARRAY['NOVA_FASE']) f)
WHERE setor_obra='X' AND cnae_codigo='Y';

-- Migrar matches de duplicata para canônica:
UPDATE matches_v2 SET obra_id='<canonica>'
WHERE obra_id='<duplicata>'
  AND cnpj NOT IN (SELECT cnpj FROM matches_v2 WHERE obra_id='<canonica>');
```

---

## PENDÊNCIAS ATIVAS (31/05/2026)

### P0 — Crítico
- [x] **Fix porte adaptativo em `matchmaker_worker.py` (V2/standalone)** — pool real AXIA SP: 77k (!=MICRO) → 10k (GRANDE+MEDIA). Fix binário por capex: >R$100mi→`IN ('GRANDE','MEDIA')` (~10k); ≤R$100mi→`!=MICRO` (~77k atual). Bracket 3-tier era ilusório (PEQUENA≡default). **V1 (cron/services/matchmaking.py) NÃO tem regressão** — 7s/obra, saudável.
- [ ] **matches_v2 stale** — features secundárias (score_breakdown, intel) dependem de V2; investigar por que parou de receber inserts em 30/05. Vitrine principal (matches_obra_prestador) OK.
- [ ] **Cron ANEEL offline** — captar_aneel.py falha HTTP desde 20/05/2026. Exit=1 no orchestrator mas resto do cron roda OK.

### P1 — Alta prioridade
- [ ] Frontend: renderizar badge de timing (bucket+mensagem) — backend pronto
- [ ] CNAEs siderúrgicos no SCC → habilitar setor SIDERURGIA_METALURGIA no captador
- [ ] Estágio 2 captador industrial_priv (--processar: BrasilAPI→Sonnet→INSERT obras)

### P2 — Média prioridade
- [ ] Validar 3 obras suspeitas no match: Motiva "CEO prepara leilão", Atlas Eletro, Aurora Coop (são obra civil?)
- [ ] Full matchmaker (obras antigas com pool pré-ampliação de fase) — após diagnosticar regressão
- [ ] Cron google_alerts: ativar após Estágio 2 industrial_priv maduro
- [ ] `.canal` null race condition (P2 antigo)
- [ ] Silenciar 401/403 console noise nos handlers de KPI do home
- [ ] Remover linha duplicada `pg_dump` no cron

### P3 — Baixa prioridade
- [ ] Pre-validar 50-100 Ouro-tier obras via Hunter+Claude pra popular decisor cache
- [ ] Sub-agente de health/backup automático

---

## APRENDIZADOS CANÔNICOS (NÃO REPETIR)

1. **Hunter: Authorization: Bearer** (não ?api_key= → 502)
2. **Capex inflado = erro nº1**: imprensa local agrega; programa global ≠ obra BR. Validar SEMPRE.
3. **Pesquisar antes de rejeitar**: Arauco ia ser descartada como "R$1bi já em DB" → era R$25bi, a maior do mundo
4. **Empresa errada**: Echoenergia≠Neoenergia; Cosan≠Raízen; CORSAN≠Cagepa. CNPJ-first BrasilAPI sempre
5. **Truncamento em setor_cnae_compatibility** → zero matches silencioso (bug histórico, fix via array_replace)
6. **immutable_unaccent_lower obrigatório** em qualquer comparação de texto no DB
7. **fonte_tipo**: constraint aceita só 'OFICIAL', 'NOTICIA', 'MANUAL' — PESQUISA_MANUAL não existe
8. **Match por fase**: regra de ouro = aparecer cedo é barato, tarde é fatal; janela generosa > filtro rígido
9. **Dedup cross-empresa**: mesmo contrato visto como contratante (Petrobras) e vencedor (DOF/Navship) = 2 cards, 1 obra
10. **Concorrentes (BVMI/InduXdata)**: anonimizam projetos no feed público → não extrair; usar como radar de setor quente
11. **ANTAQ bug**: `antaq_tup` mapeia outorga → OPERACAO mecanicamente (551 obras ~R$76,9bi afetadas)
12. **Petrobras anúncio político**: "R$Xbi em investimentos no estado Y" = guarda-chuva → PIPELINE, nunca obra
13. **status_licenca='NOTICIA'**: bug do captador antigo; gravar NULL quando ausente, nunca 'NOTICIA'
14. **Filtro NOTICIA worker**: obras NOTICIA com validacao_obra_at IS NOT NULL agora passam (fix 31/05/2026)
15. **Sessão Tipo A/B**: Tipo A = investigação read-only (pode ser longa); Tipo B = execução (uma transação, encerra após COMMIT). /compact antes de briefing de escrita em sessão >2h.
16. **matchmaker_jobs schema real**: `iniciado_por` / `iniciado_em` / `finalizado_em` (NÃO job_name/started_at/finished_at)
17. **fornecedores.cnae**: `cnae_principal text` + `cnae_secundarios text[]` (NÃO cnae_codigo único)
18. **V1 vs V2 matchmaker**: V1 (cron, `services/matchmaking.py` → `matches_legacy/matches_obra_prestador`) alimenta vitrine, 7s/obra. V2 (`matchmaker_worker.py` → `matches_v2`) é standalone. Fix de perf (porte adaptativo) vai em V2.
19. **INSERT direto em obras não dispara trigger de classificação** — `classificacao_computed` fica NULL. Sempre chamar `SELECT recompute_classificacao_obra(uuid)` após INSERT direto. Endpoint `/promover` já faz isso automaticamente.

---

## ESTADO DA PLATAFORMA (31/05/2026)

```
OURO: ~938 obras | PRATA: ~67 | BRONZE: ~3.242 | PIPELINE: ~672 | NULL: ~697
matches_obra_prestador (cron): ~115k | matches_v2 (standalone): ~630k+
Obras visíveis: ~5.617 | Data: 30/05/2026
Hunter: ~333/2.000 restantes | Reset: 11/06/2026 03:20 UTC
Serper: 2.500 créditos gratuitos (ativos)
Disk VPS: ~82%, 8.8GB free
Backup rclone → GDrive: ativo
```

### Obras canônicas de referência (não modificar sem cautela)
| Obra | UUID | Tier | Capex |
|---|---|---|---|
| Toyota Sorocaba | a3ffc496 | NULL (sem CNPJ) | R$5bi |
| XBRI Pneus PR | 866f09c1 | BRONZE | R$6,2bi |
| Guofuhee Uberaba | 893bf764 | PIPELINE | R$202mi |
| Arauco Sucuriú MS | 4b420bb6 | OURO | R$25bi |
| Petrobras R$37bi SP | c0aeed8e | PIPELINE | R$37bi |
| Alibaba DC SP | 1801a4f4 | NULL | NULL |

---

## CONTATOS E REPRESENTANTES

- **Mari Silveira Vilela**: representante comercial, co-admin (`prestadores.eh_co_admin`), painel `/vendas`, comissão 50% inicial / 25% recorrente 12 meses
- **Eric Secco**: country manager Alibaba Cloud Brasil (ex-AWS) — decisor pra enrichment
