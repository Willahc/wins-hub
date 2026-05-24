---
name: nova-obra-enrichment
description: Pipeline canônico de enriquecimento de decisor pra obra (subsidiária/SPE/PPP). Aciona quando obra entra sem decisor real em decisores_obra, ou via botão ⚡ Enriquecer da tab admin "Sem decisor" (v1.4.5). Cobre dedup, validação de domínio, LinkedIn (Serper PT+EN), Hunter email-finder, fallback holding via BrasilAPI QSA (v1.4.7), telefone, persistência em decisores_obra (fonte única v1.4.3) e recompute. Validado end-to-end em ENGIE Plano 2026-2030 (BRONZE→OURO 20min) e ECO RIOMINAS S.A. (BRONZE→OURO via fallback holding ecorodovias).
---

# Skill: Enriquecimento de Obra Nova — Pipeline Canônico

Pipeline implementado em `app/scripts/enrichment_auto_job.py` (commit `6ede3fc`, tag `v1.4.7-holding-fallback`). Acionável via:
- **Admin UI** — tab "Sem decisor" → botão ⚡ Enriquecer por linha (POST `/api/admin/obras/{id}/enriquecer`, gateado JWT admin)
- **CLI in-container** — `docker exec wins_hub-api-1 python /app/scripts/enrichment_auto_job.py --obra-id <UUID> --commit`
- **Cron** — runtime job 02:00 BRT, obras criadas nas últimas 24h sem decisor

## Trigger
- Obra com `fonte_tipo IN ('MANUAL', 'PESQUISA_MANUAL', 'NOTICIA', 'OFICIAL')` sem decisor canônico
- `NOT EXISTS (SELECT 1 FROM decisores_obra d WHERE d.obra_id = obras.id AND d.excluido_em IS NULL AND d.hipotese_replicacao IS DISTINCT FROM 'REPLICADO_PROVAVEL_FALSO_POSITIVO')`
- Capex mínimo `CAPEX_MIN = R$ 50 Mi` (configurável via `--min-capex`)

## Caps configurados (constantes no script)
| Constante | Valor | Significado |
|-----------|-------|-------------|
| `MAX_OBRAS_PER_RUN` | 10 | obras processadas por batch (cron) |
| `HUNTER_MAX_CALLS_PER_OBRA` | 4 | teto Hunter por obra (subsidiária); fallback usa o mesmo teto separado |
| `CAPEX_MIN` | 50_000_000 | filtro capex mínimo |
| `MARKER` | `enrichment_auto:v1:{YYYYMMDD}` | preenchido em `decisores_obra.registrado_por` pra rollback |

---

## Pipeline obrigatório — 8 passos (+ 5b fallback)

### Passo 1 — Deduplicação
```sql
SELECT id, nome, classificacao_computed, fase, valor_estimado
FROM obras
WHERE empresa ILIKE '%{empresa}%' OR nome ILIKE '%{nome}%'
ORDER BY valor_estimado DESC;
```
Se existir obra similar → checar se é a mesma ou distinta. Documentar decisão antes de prosseguir.

### Passo 2 — Validação de domínio
- Checar `empresa_dominios WHERE cnpj = {cnpj}` via `get_dominio_validado()`.
- Rejeita: `confianca < 4`, `validacao_metodo` contém `'agressivo'/'descoberta_automatica'/'domain_search'`.
- Se inválido + modo admin (`--obra-id`): tenta `discover_domain_via_serper()` (3 guards: tokens distintivos da razão social + blocklist + Hunter confirmation de pelo menos 1 email no domínio).
- Se inválido + cron: **skip** (não dispara Hunter).

### Passo 3 — LinkedIn search padrão (2 queries Serper)
Implementação em `linkedin_search(serper_key, empresa)` (enrichment_auto_job.py:156):

```
Q1: site:linkedin.com/in "{empresa}" "diretor" OR "gerente"
    ("capex" OR "investimentos" OR "projetos" OR "implantação")

Q2: site:linkedin.com/in "{empresa}" "diretor" OR "gerente"
    ("suprimentos" OR "compras" OR "procurement" OR "engenharia")
```

Cada query: `num=10` hits. Filtra snippets com `"ex-"` ou `"former"`. Total bruto típico: 10-20 hits.

### Passo 4 — Busca Mari (fallback quando Passo 3 < 4 candidatos)
Usar quando LinkedIn bloqueado, sem resultado, ou empresa grande com perfis genéricos demais.

**30 variações** cobrindo PT + EN como Google externo (`webwsearch` sem `site:linkedin.com`, depois cross-reference com LinkedIn pelo nome):

**Cargos PT (7):** diretor · gerente · coordenador · superintendente · head · chefe · VP
**Cargos EN (7):** director · manager · head · chief · officer · VP · superintendent

**Áreas PT (9):** suprimentos · compras · projetos · obras · investimentos · capex · implantação · engenharia · contratos
**Áreas EN (7):** supply chain · procurement · capex · projects · engineering · contracts · investments

Execução: 2 Serper PT + 2 Serper EN = **4 calls máximo**.

Filtros obrigatórios:
- Cargo relevante + empresa correta + ativo (não "ex-")
- Priorizar: nome composto + cargo específico + empresa no título
- SPE/SPV → buscar decisor na holding/operadora real (ver Passo 5b)

### Passo 5 — Hunter Email Finder
Implementação em loop em `_processar_obra_inner()`. Threshold rejeita:
```python
if not email or score < 70 or v_status not in ("valid", "accept_all", None):
    log.info(f"  ✗ Hunter {nome}: email={email} score={score} status={v_status}")
    continue
```

**Hierarquia de cargos** (`rank_candidatos()` linha 219) — pontuação cumulativa, top 4 vão pro Hunter:

| Termo no cargo | Pontos |
|----------------|--------|
| `diretor` / `director` | +30 |
| `gerente` / `manager` | +20 |
| `coordenador` | +10 |
| Área: `implantação`, `capex`, `investimento`, `transmissão` | +15 |
| Área: `suprimento`, `procurement`, `compras` | +10 |

Dedup por nome após ordenação. Ex.: "Diretor de Implantação" (30+15=45) supera "Gerente de Suprimentos" (20+10=30).

### Passo 5b — Fallback Holding (v1.4.7)
**Acionado automaticamente** quando Hunter retorna 0 emails no domínio da subsidiária.

Fluxo `get_holding_dominio(cur, conn, cnpj, ...)`:
1. **Cache hit**: `empresa_dominios.holding_dominio` (0 API calls).
2. **BrasilAPI QSA**: `GET https://brasilapi.com.br/api/cnpj/v1/{cnpj}` (free, sem chave). Extrair primeiro sócio PJ via `_socio_pj_controlador()` — CNPJ tem 14 dígitos sem máscara `*` (BrasilAPI mascara CPFs).
3. **Discover**: `discover_domain_via_serper(nome_holding)` reusa o validador 3-guards.
4. **Persist**: UPDATE `empresa_dominios SET holding_cnpj=..., holding_nome=..., holding_dominio=...`.
5. **Re-roda Hunter** com os MESMOS candidates rankeados, agora contra `holding_dominio`.
6. **Persiste aceitos** com `extra_componentes={fallback_holding: true, holding_dominio, holding_motivo, dominio_subsidiaria}` em `decisores_obra.confianca_match_componentes`.

**Quando usar**:
- SPEs/PPPs/concessões (ANTT, ANEEL, ANTAQ, ANAC) com domínio próprio sem cobertura Hunter
- QSA com sócio PJ → resolve automático via BrasilAPI
- QSA com só sócios PF → pré-popular `empresa_dominios.holding_*` manualmente uma vez (cache resolve depois)

**Custo extra**: 0 Serper extra (reusa candidates) + até 4 Hunter (cap separado) + 1 BrasilAPI (gratuito).

### Passo 6 — Busca de telefone
Implementação em `telefone_corporativo(serper_key, empresa)`:
```
Q: "{empresa}" "fale conosco" OR "contato" telefone
```
Regex `PHONE_RE` extrai DDD+número de snippets/title. Persiste em `obras.nivel1_telefone` + `nivel1_telefone_e164` + `nivel1_telefone_status='ok'`.

> Nota: telefone do decisor direto (ramal) vai em `decisores_obra.telefone` (raramente disponível via Serper). Telefone corporativo da empresa fica em `obras.nivel1_*` (cache).

### Passo 7 — Persistência (schema v1.4.3 fonte única)

`decisores_obra` é a **fonte autoritativa**. `obras.nivel1_*` é cache interno (classificação OURO/PRATA, backfill Hunter) e **nunca exposto via API**.

```sql
-- Insert via persistir_decisor(): gate decisor_inserivel() pré-INSERT
INSERT INTO decisores_obra (
  obra_id, nome, cargo, linkedin_url, email, telefone,
  fonte, registrado_por, tipo_cargo, confianca_match,
  hipotese_replicacao, confianca_match_componentes,
  confianca_match_calculada_em
) VALUES (
  '{obra_id}', '{nome}', '{cargo}', '{linkedin_url}', '{email}', '{telefone}',
  'serper_linkedin+hunter_email_finder',
  'enrichment_auto:v1:{YYYYMMDD}',
  '{tipo_cargo}',  -- ver mapping abaixo
  {confianca_match},  -- max(70, min(hunter_score, 97))
  NULL,
  '{...componentes_jsonb...}'::jsonb,
  now()
)
ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO NOTHING
RETURNING id;
```

**Mapping `tipo_cargo`** (enrichment_auto_job.py:287):
- `GERENTE_PROJETOS` — `gerente` + (`projeto` OR `implantação`)
- `GERENTE_ENGENHARIA` — `gerente` + `engenharia`
- `GERENTE_SUPRIMENTOS` — `gerente` + (`suprimento` OR `compra`)
- `COORDENADOR_OBRAS` — `coordenador` + `obra`
- `OUTRO` — default

**`confianca_match_componentes` (JSONB audit)**:
```json
{
  "fonte_pipeline": "enrichment_auto_job_v1",
  "linkedin_url_serper": "...",
  "hunter_score": 98,
  "hunter_email": "julio.amorim@ecorodovias.com.br",
  "obra_capex": 1969000000.0,
  "obra_fonte": "antt_rod",
  "decisor_gate_motivo": "cargo_cita_empresa",

  // só se passou pelo fallback holding (v1.4.7)
  "fallback_holding": true,
  "holding_dominio": "ecorodovias.com.br",
  "holding_motivo": "cache_hit:Grupo EcoRodovias",
  "dominio_subsidiaria": "ecoriominas.com.br"
}
```

### decisor_gate (pré-INSERT, `sales_intelligence/decisor_gate.py:118`)
4 gates em ordem:
- **Gate 0** — cargo em `CARGOS_NAO_DECISOR` (Partner/Investor/Founder/...) → rejeita
- **Gate 0.5** — cargo contém razão social (parse error tipo Trident) → rejeita
- **Gate 1** — cargo cita token distintivo da empresa → **aceita** (match defensável)
- **Gate 2** — (nome, cargo) aparece em ≤ `GATE2_THRESHOLD_RAIZES` CNPJ-raízes → aceita (decisor único)
- **Default** — rejeita como "cargo_generico_multi_grupo" (replicação suspeita)

Motivo do gate vai em `confianca_match_componentes.decisor_gate_motivo` pra auditoria.

### Passo 8 — Recompute
```sql
-- SEMPRE este, NUNCA recompute_classificacao_full() (regra imutável)
SELECT recompute_classificacao_obra('{obra_id}');
```
Promoção esperada:
- **BRONZE → OURO** se inserir ≥1 decisor com email Hunter score ≥70 e cargo decisor válido
- **BRONZE → PRATA** se inserir decisor sem email (LinkedIn-only)

---

## Custo estimado por obra

| Recurso | Calls (caso happy) | Calls (com fallback holding) |
|---------|---|---|
| Serper LinkedIn | 2 | 2 |
| Serper telefone | 1 | 1 |
| Serper discover (cache miss holding) | 0 | 0-2 |
| Hunter email-finder | até 4 | até 8 (4 subsidiária + 4 holding) |
| BrasilAPI QSA | 0 | 1 (free) |

Hunter quota: 2000/mês (reset 11/06 03:20 UTC) — ver memory `project_winshub_hunter_state.md`.

---

## Regras anti-alucinação
- **Nunca usar Hunter em domínio não validado** (`validacao_metodo` 'agressivo'/'descoberta_automatica' → revalidar).
- Confirmar que decisor é funcionário **atual** (filtro `ex-`/`former` no snippet).
- Confirmar empresa correta (não homônimo) — `parse_candidato()` exige primeira palavra da razão social no title+snippet.
- `decisor_gate` bloqueia decisores genéricos replicados em ≥2 CNPJ-raízes (anti-FP tipo Francisco Antonio Rueda).
- Hunter threshold rígido: score ≥ 70 + status ∈ {valid, accept_all, None}.

---

## Rollback
```sql
-- Reverter decisores inseridos pelo pipeline em data X (marker em registrado_por)
DELETE FROM decisores_obra
WHERE registrado_por = 'enrichment_auto:v1:20260524';

-- Re-classificar a obra após delete
SELECT recompute_classificacao_obra('{obra_id}');
```

Pra rollback do fallback holding (caso decisor da holding seja inválido):
```sql
UPDATE decisores_obra SET excluido_em = now()
WHERE obra_id = '{obra_id}'
  AND confianca_match_componentes->>'fallback_holding' = 'true';
```

---

## Auditoria

```sql
-- Listar todos os decisores inseridos via fallback holding (audit v1.4.7)
SELECT o.empresa, d.nome, d.email, d.confianca_match,
       d.confianca_match_componentes->>'dominio_subsidiaria' AS dom_sub,
       d.confianca_match_componentes->>'holding_dominio'     AS dom_hold,
       d.confianca_match_componentes->>'holding_motivo'      AS motivo
FROM decisores_obra d
JOIN obras o ON o.id = d.obra_id
WHERE d.confianca_match_componentes->>'fallback_holding' = 'true'
  AND d.excluido_em IS NULL
ORDER BY d.registrado_em DESC;
```

---

## Exemplos validados

### ENGIE Brasil — Plano Investimentos 2026-2030 (R$6bi)
- **Antes**: BRONZE, 0 decisores em `decisores_obra`
- **Pipeline**: 2 Serper LinkedIn → 6 candidatos → 4 Hunter direto em `engie.com.br`
- **Inseridos**: 3 decisores (Diretor Transmissão + Diretor Renováveis + Diretor Implantação Asa Branca)
- **Telefone**: corporativo (48) 3221-7000 + ramal Guilherme (48) 3221-7072
- **Depois**: **BRONZE → OURO** em ~20 min
- **Custo**: 6 Serper + 4 Hunter

### ECO RIOMINAS S.A. — Concessão Rodoviária (R$1.97bi)
- **Antes**: BRONZE, 0 decisores; `obras.nivel1_nome` tinha FP "Francisco Antonio Rueda" (filtrado por v1.4.3)
- **Pipeline (subsidiária `ecoriominas.com.br`)**: 2 Serper → 20 hits → 4 candidatos top → **4/4 Hunter falhou** (domínio sem cobertura Hunter, `total: 0 pessoas`)
- **Fallback holding (v1.4.7)**:
  - Cache hit em `empresa_dominios.holding_dominio` = `ecorodovias.com.br` (pré-populado manual; QSA da ECO RIOMINAS só tem sócios PF)
  - Re-roda Hunter com mesmos 4 candidates em `ecorodovias.com.br`
  - **1/4 aceito**: `julio.amorim@ecorodovias.com.br` score **98** (mesmo Julio Amorim — Diretor Superintendente listado no LinkedIn da subsidiária)
- **Depois**: **BRONZE → OURO**
- **Custo total**: 2 Serper + 8 Hunter (4 sub + 4 hold) + 1 BrasilAPI (free)
- **Audit**: `confianca_match_componentes->>'fallback_holding' = 'true'`, `holding_dominio = 'ecorodovias.com.br'`
