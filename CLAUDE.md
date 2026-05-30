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
- **Fix de performance em V2** → ranking por porte no ORDER BY (NÃO cortar via WHERE — binário corta 90%+ em 14/17 setores SP); refatorar `_PORTE_FILTER` compartilhado entre 3 callsites (matchmaker_worker L160, main.py L3302, L3326)
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

## PENDÊNCIAS ATIVAS (30/05/2026)

### P0 — Crítico
- [x] **Fix porte V2 — solução correta pendente.** Ranking por porte no ORDER BY (sem cortar WHERE) + refactor `_PORTE_FILTER` único entre 3 callsites (matchmaker_worker L160, main.py L3302, L3326). Fix binário revertido (2dfa37e).
- [x] **matches_v2 throughput** — resolvido 30/05: UF backfill (11 obras +1.380 matches), exclusão PIPELINE/CONCLUIDA (9f981a6), SCC fases ampliadas (PAPEL/LOGISTICO/AGRO), setor OUTRO invisibilizado. Fila stuck: 1 obra (Stellantis PE — F2 estrutural, aceitável).
- [ ] **Cron ANEEL offline** — captat_aneel.py falha HTTP desde 20/05/2026. Exit=1 no orchestrator mas resto roda OK.

### P1 — Alta prioridade
- [x] Frontend badge timing — já existe (janela_score escalar ≥85🔥/≥70⚡/≥50⏳/<50🕐). Refatorar pra timing.bucket é cosmético.
- [x] CNAEs siderúrgicos no SCC — 7 rows inseridas (commit e241dea). SETOR_MAP google_alerts habilitado.
- [x] Estágio 2 captador industrial_priv (--processar: BrasilAPI→Sonnet→INSERT obras) — done 30/05 (commits `b98c7ca` Estágio 2 + `601a252` setor armazenagem→LOGISTICO + aceitar obras públicas; 1ª obra cadastrada: Conab Ponta Grossa LOGISTICO/PR `ad671638`)

### P2 — Média prioridade
- [x] Aurora Coop: obra civil legítima (novo frigorífico São Miguel do Oeste SC R$600mi, 2027) — mantida no match
- [x] Motiva, Atlas Eletro: gerando matches (77 e 193) — validados indiretamente
- [x] `.canal` race condition — não existe em main.py (só em alerta_semanal_cnae.py L52, legítimo)
- [x] 401/403 console noise — são HTTPException raises, fix é frontend (fora de escopo)
- [x] pg_dump duplicado — não existe (backup_diario.sh tem 1 pg_dump)
- [ ] Full matchmaker (obras com pool pré-ampliação de fase) — após resolver regressão P0
- [ ] Cron google_alerts: ativar após Estágio 2 industrial_priv maduro
- [ ] `chmod +x captat_google_alerts.py` — perdeu executable bit no commit e241dea (não quebra cron pois usa `python script.py`)
- [ ] **Bug captador `cimm_rss`**: extrai empresa pro Sonnet validar (obs comprova "Empresa identificada, fornecedores B2B demandados") mas NÃO persiste em `obras.empresa` — toda BRONZE futura via cimm_rss cai como `empresa=NULL`. Descoberto 30/05 na auditoria dedup (Positivo Tecnologia R$300mi + Tropical Biogás R$275,8mi backfillados manualmente). Fix: patchar parsing pra extrair empresa do título antes do INSERT.
- [x] **Eldorado Brasil MS ferrovia: dup BNDES×DOU?** — investigado 30/05 ultra2: **DUP CONFIRMADA** (mesmo ramal Três Lagoas↔Aparecida do Taboado MS, 86,66 km Eldorado). BNDES R$1bi = financiamento parcial debêntures; DOU R$2,4bi = capex TOTAL via REIDI Portaria 345/2026. **Decisão de canônica em aberto** — sugere manter DOU (capex total mais real), invisibilizar BNDES (parcial). Pendência: invisibilizar `b1ffcd9f` com motivo `dup_cross_captador_eldorado_ferrovia_30052026` quando user decidir.
- [x] **Padronização empresa por CNPJ** (Q3 30/05 ultra2): **77/84 obras normalizadas** via BrasilAPI razão social. Petrobras 34, Motiva/CCR 14, ISA/CTEEP 11, Rumo Sul 7, MRS 6, Shell BR 5. **PRIO 7 obras pendentes** — BrasilAPI HTTP 400 no CNPJ 33069197000199 (possível CNPJ holding/subsidiária com erro de base). Retry manual ou fetch direto Receita.
- [x] **Petrobras cvm_ipe UF backfill** (30/05 ultra2): 5 obras movidas RJ→UF correta (1 PE RNEST, 1 SE Sergipe, 1 AP Amapá, 2 ES Jubarte/Espírito Santo). 4 ficam RJ (Bacia de Campos off-shore legítimo). Bug estrutural cvm_ipe (atribui UF=HQ por default) permanece — fix do captador pendente. Aprendizado #20 reforçado.
- [ ] **52 obras com CNPJ matematicamente inválido residuais** (audit 30/05 ultra2). Top uniformizados via nome holding: PRIO S.A. 7, Enauta 4, Rialma 3, Equinor 2, Mineração Rio do Norte 2, Ambar 2 = 20 obras com nome canônico mas CNPJ ainda inválido. Restam 32 com 1 obra cada. **Fix estrutural** (aprendizado #25): captadores invocarem `cnpj_valido()` antes de INSERT. Backlog imediato: investigar CNPJs corretos de cada holding (Receita Federal manual ou CNPJa API) e atualizar campo `cnpj` + retry BrasilAPI normalize.

### P3 — Baixa prioridade
- [ ] Pre-validar 50-100 OURO via Hunter+Claude pra popular decisor cache
- [x] Sub-agente health/backup — session_health.sh ativo, crontab 08:55

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
18. **V1 vs V2 matchmaker**: V1 (cron) scoring RF + LIMIT 50, 7s/obra. V2 (standalone) usa `porte_inferido`. Fix de perf em V2 = **ranking por porte no ORDER BY, NÃO filtro no WHERE** (binário corta 90%+ do pool em 14/17 setores). Drift entre 3 callsites — refatorar `_PORTE_FILTER` único (worker L160 + main.py L3302 + L3326).
19. **INSERT direto em obras não dispara trigger de classificação** — `classificacao_computed` fica NULL. Sempre chamar `SELECT recompute_classificacao_obra(uuid)` após INSERT direto. Endpoint `/promover` já faz isso automaticamente.
20. **UF=NULL silencia obras no V2** — worker filtra `o.uf IS NOT NULL`; obras sem UF nunca entram na fila incremental. Backfill via pesquisa geográfica da OBRA (não da matriz/CNPJ — DOF é RJ mas obra é SC/Navship).
21. **Fases fora do SCC zeram matches silenciosamente** — PIPELINE/CONCLUIDA não estão em nenhum `fases_aplicaveis`; worker agora exclui `AND o.fase NOT IN ('PIPELINE','CONCLUIDA')` (commit 9f981a6). Setor OUTRO sem SCC mapping → invisibilizar obra.
22. **Stellantis PE AUTOMOTIVO** = caso estrutural (F2 intransponível): zero fornecedores GRANDE/MEDIA em AUTOMOTIVO/PE. Não é bug — mercado real contrata de SP/MG. Aceitar como limitação.
23. **SPEs ANEEL/ANTT fragmentadas = 1 oportunidade B2B**: SIGA registra cada UFV como linha (Serena CE = 120 "Kuara 1 X" mesmo CNPJ, mesmo capex R$117,6mi); ANTT/PIC idem (FCA MG = 26 trechos). Dedup safe SÓ quando 0 decisor email no grupo (preserva REPLICADO Sprint 1 — ex: Citlux 34×34 emails). Canônica via `DISTINCT ON (emp_norm,uf) ORDER BY emp_norm, uf, criado_em ASC, id ASC` (tiebreaker determinístico pra ties de batch ingestion ANEEL); `SUM(valor_estimado)` agrega cap no canônico; motivo `ufv_spe_fragmentada_aneel_DDMMAAAA`. **Sempre fazer pg_dump -t obras ANTES** (custom format, rollback rápido). **Filtro refinado pra qtd 2-5 (long tail)**: adicionar `HAVING ... AND (COUNT(DISTINCT valor_estimado)=1 OR COUNT(DISTINCT nome)=1)` — sem isso colapsa falsos positivos tipo "Vamos Locação SP: 2 financiamentos BNDES distintos com mesmo CNPJ" (capex e nomes distintos = obras reais, NÃO fragmentação). Track B FK migration: pra grupos COM decisor email, padrão 1:1 (1 obra = 1 decisor replicado) permite `DELETE FROM decisores_obra WHERE obra_id IN (invisibilizadas)` sem perda de contato (canônica mantém 1 row do mesmo email).
24. **Cross-captador real vs cobertura complementar**: na audit Q2 30/05, hipótese inicial "anp_ep × cvm_ipe Petrobras = mesma obra dup" estava ERRADA. anp_ep registra ASSETS DE PRODUÇÃO (campos com PSA); cvm_ipe registra EVENTOS CORPORATIVOS (fatos relevantes CVM). Similarity textual = 0 entre fontes; são lentes diferentes da mesma empresa. **Antes de invisibilizar cross-captador**: confirmar overlap via similarity(nome) > 0.5 + capex + descrição. Dups reais existem (Habitat PA bndes×bndes mesma obra; ThyssenKrupp SP exame×google_alerts mesma notícia; Eldorado MS bndes×dou mesmo ramal ferroviário 86,66km — DOU tem capex TOTAL R$2,4bi via REIDI, BNDES só financiamento parcial debênture R$1bi → canônica é a MAIS COMPLETA, não a com menos capex).
25. **CNPJ inválido no DB = bug captador silencioso** — 52 obras (descoberto 30/05) com DV matematicamente errado, padrão "raiz + sufixo default 0001-99" inventado pelo captador (PRIO 7×, Enauta 4×, Rialma 3×, Equinor 2×, Mineração RN 2×, Ambar 2×, +42 trailing). BrasilAPI rejeita com HTTP 400 "CNPJ inválido". **Fix estrutural pendente**: captadores devem chamar `cnpj_valido(text) → boolean` (função SQL já existe no DB) ANTES de INSERT em obras, e logar/skipar se inválido. Workaround manual: `UPDATE obras SET empresa='Razão Holding' WHERE cnpj='XXX' AND empresa <> 'Razão Holding'` (mantém CNPJ inválido mas uniformiza nome). Audit query: `SELECT cnpj, COUNT(*), array_agg(DISTINCT empresa) FROM obras WHERE motivo_invisivel IS NULL AND cnpj IS NOT NULL AND NOT cnpj_valido(cnpj) GROUP BY 1 ORDER BY 2 DESC`.

---

## ESTADO DA PLATAFORMA (31/05/2026)

```
OURO: 929 obras | PRATA: 65 | BRONZE: 1.807 | PIPELINE: 548 | NULL: 561
matches_obra_prestador (cron): ~115k | matches_v2 (standalone): ~632k+
Obras visíveis: 3.910 | Data: 30/05/2026 (−1.707 cleanup dia inteiro, −30,4%)
Hunter: ~333/2.000 restantes | Reset: 11/06/2026 03:20 UTC
Serper: 2.500 créditos gratuitos (ativos)
Disk VPS: ~82%, 8.8GB free
Backup rclone → GDrive: ativo
Commits hoje: f5ef431→dea934b (16 commits PUSHED origin/main) · 6 backups custom format em /home/william/backups/ultra_brief_20260530/
```

**Cleanup ultra2 30/05 noite tarde** (visíveis intacto, refactor cosmético+geográfico): **77 obras normalizadas empresa via BrasilAPI** (6 CNPJs: Petrobras 34, Motiva/CCR 14, ISA/CTEEP 11, Rumo Sul 7, MRS 6, Shell BR 5; PRIO 7 obras CNPJ inválido). **5 Petrobras cvm_ipe UF backfilladas** (RJ→PE/SE/AP/ES baseado no nome) — aprendizado #20 manifestação. Backup: `pre_ultra2.dump`.

**Cleanup ultra2 wrap 30/05 noite** (visíveis −1: Eldorado BNDES invisibilizada, DOU R$2,4bi mantida canônica). **20 obras uniformizadas por CNPJ inválido** (PRIO 7, Enauta 4, Rialma 3, Equinor 2, Mineração RN 2, Ambar 2) — nome holding aplicado via fallback manual já que BrasilAPI rejeitou CNPJ. **Audit descobriu 52 obras visíveis com CNPJ matematicamente inválido** → aprendizado #25 + pendência P2 (captadores precisam invocar `cnpj_valido()` antes do INSERT). **17 commits PUSHED origin/main** (`f5ef431..067980a`).

**Cleanup dedup 30/05 tarde** (−139): −137 obras seguras (`empresa_nao_extraida_30052026` 114 agenciainfra_wp NULL + `servico_nao_obra_pncp_30052026` 23 Rio Negrinho câmara) + −2 PRATAs institucionais (`decisor_institucional_sem_pessoa_30052026`: pontes DNIT + PPP Bahia/FDIRS); +4 BRONZE backfill empresa preservou R$863,8mi visíveis (Positivo Tecnologia/Tropical Biogás/CEM Bioenergia/Eldorado Brasil Celulose).

**Cleanup Track A 30/05 noite** (−1.269): 105 SPEs/UFVs ANEEL+ANTT fragmentadas colapsadas em 1 canônica (motivo `ufv_spe_fragmentada_aneel_30052026`). 101 canônicas com `SUM(capex)` agregado + sufixo `(complexo N unidades)`. Top: Kuara 3 VI (Serena CE 120 UFVs) R$14,11bi, Aurora 85 (MG 56) R$9,24bi, Apia 16 (BA 18) R$6,80bi.

**Cleanup Track B 30/05 noite** (−96): 5 SPEs holding JLC+Lightsource colapsadas em 5 canônicas (Citlux MG/Vento Pampeiro RS/Empresa Desenvolvedora MG/Rio Alto PB/Lightsource Rio Branco BA). DELETE 96 decisor rows replicados (2 emails distintos: `lrocha@jlc.com`×92 + `fabio.pimentel@mdiasbranco.com.br`×9 → 5 rows finais, zero perda de contato). Motivo: `ufv_spe_fragmentada_aneel_track_b_30052026`.

**Cleanup long tail + Suzano UF 30/05 noite** (−195): 136 canônicas qtd 2-5 com filtro refinado capex/nome único (138 grupos qualificados; 144 grupos qtd 2-5 rejeitados por terem capex+nome ambos distintos = obras reais tipo Vamos Locação BNDES). −190 obras invisibilizadas no motivo Track A. Suzano: 5 backfilladas UF=ES (Aracruz/tissue/aterro match) + 5 invisibilizadas `programa_nacional_multi_uf_30052026` (programas florestais nacionais + P&D). Backups: `pre_track_a.dump`, `pre_track_b.dump`, `pre_longtail.dump` (3-4MB cada).

**Cleanup Q2 cross-captador 30/05 noite** (−2, motivo `dup_cross_captador_30052026`): Habitat PA (BNDES saneamento × BNDES financiamento, mesma obra R$20mi) + ThyssenKrupp SP (notícia Exame × Google Alerts, mesma R$50mi). Investigação inicial sugeria 8 dups mas só 2 confirmadas; outras 6 eram lentes complementares (Vale MA porto+ferrovia, Petrobras RJ anp_ep+cvm_ipe assets vs eventos corporativos, etc). Backup: `pre_q2.dump`.

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
