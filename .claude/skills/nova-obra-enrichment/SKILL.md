---
name: nova-obra-enrichment
description: Pipeline canônico de 8 passos para enriquecer obra recém-cadastrada (manual/notícia/pesquisa). Acionar quando inserir obra nova sem decisor, ou quando rodar enriquecimento de obras com fonte_tipo IN ('MANUAL','PESQUISA_MANUAL','NOTICIA'). Garante deduplicação, validação de domínio antes de Hunter, busca LinkedIn padrão + Mari, captura de telefone, persistência consistente e recompute. Baseado no enriquecimento validado da obra ENGIE Plano 2026-2030 (R$6bi) que promoveu BRONZE→OURO em ~20 min.
---

# Skill: Enriquecimento de Obra Nova — Pipeline Canônico

## Trigger
- Obra inserida com fonte_tipo IN ('MANUAL', 'PESQUISA_MANUAL', 'NOTICIA')
- Obra sem decisor (nivel1_nome IS NULL)
- Cron 02:00 BRT: obras criadas nas últimas 24h sem decisor

---

## Pipeline obrigatório — 8 passos

### Passo 1 — Deduplicação
```sql
SELECT id, nome, classificacao_computed, fase, valor_estimado 
FROM obras 
WHERE empresa ILIKE '%{empresa}%' OR nome ILIKE '%{nome}%'
ORDER BY valor_estimado DESC;
```
Se existir obra similar → checar se é a mesma ou distinta. Documentar decisão antes de prosseguir.

### Passo 2 — Validação de domínio
- Checar `empresa_dominios WHERE cnpj = {cnpj}`
- Se NULL ou confiança < 50 ou método IN ('E2_agressivo', 'E3_domain_search', 'descoberta_automatica_V2'):
  → **web_search ANTES de usar Hunter** — nunca confiar em domínio não validado
  → Atualizar `empresa_dominios` com domínio real descoberto

### Passo 3 — LinkedIn search padrão (2 queries obrigatórias via Serper)
Q1: "{empresa}" site:linkedin.com/in + "suprimentos" OR "supply chain" OR "capex" OR "compras" OR "projetos" OR "engenharia"
Q2: "{empresa}" site:linkedin.com/in + "diretor" OR "gerente" OR "coordenador" OR "procurement" OR "obras" OR "investimentos"
- Multinationals → adicionar "Brasil"
- SPEs → usar operador real (não a SPE)
- Genéricas → filtrar por CNPJ

### Passo 4 — Busca Mari (Google externo, sem LinkedIn direto)
Usar quando LinkedIn bloqueado, sem resultado, ou empresa grande com perfis genéricos demais.

30 variações cobrindo PT + EN:

**Cargos PT:** diretor, gerente, coordenador, superintendente, head, chefe, VP
**Cargos EN:** director, manager, head, chief, officer, VP, superintendent

**Áreas PT:** suprimentos, compras, projetos, obras, investimentos, capex, implantação, engenharia, contratos
**Áreas EN:** supply chain, procurement, capex, projects, engineering, contracts, investments

Execução: 2 calls Serper PT + 2 calls Serper EN = 4 calls máximo
Filtros obrigatórios:

Cargo relevante + empresa correta + ativo (não "ex-")
Priorizar: nome composto + cargo específico + empresa no título
SPE → buscar decisor na holding/operadora real


### Passo 5 — Hunter Email Finder
- **Só após domínio validado** (Passo 2)
- Máximo **3-4 calls por obra**
- Prioridade de cargo:
  1. Diretor de Implantação / Diretor Capex
  2. Diretor de Suprimentos / Procurement
  3. Gerente de Projetos / Engenharia
  4. CFO (último recurso)

### Passo 6 — Busca de telefone
web_search "{nome decisor}" + "{empresa}" + "telefone" OR "contato"
web_search "{empresa}" + "fale conosco" OR "contato corporativo"
- Persistir telefone corporativo em `obras.nivel1_telefone`
- Persistir ramal/direto do decisor em `decisores_obra.telefone`
- Registrar negative-cache quando não encontrado (evita re-busca)

### Passo 7 — Persistência
```sql
-- Inserir decisores (schema real: decisores_obra usa nome/cargo/email — NÃO nivel1_*)
INSERT INTO decisores_obra (
  obra_id, nome, cargo, linkedin_url, email, telefone, fonte, registrado_por,
  tipo_cargo, confianca_match, hipotese_replicacao,
  confianca_match_componentes, confianca_match_calculada_em
)
VALUES (
  '{obra_id}', '{nome}', '{cargo}', '{linkedin_url}', '{email}', '{telefone}',
  'serper_linkedin+hunter_email_finder',
  'enrichment_pipeline_v1:{data}',
  '{tipo_cargo}',  -- GERENTE_PROJETOS|GERENTE_ENGENHARIA|GERENTE_SUPRIMENTOS|...|OUTRO
  {confianca_match},  -- 70+ pra OURO se email; 50+ pra PRATA
  NULL,  -- ou 'REPLICADO_PROVAVEL_FALSO_POSITIVO' se SPE+decisor genérico
  '{...componentes_jsonb...}'::jsonb,
  now()
);

-- Atualizar domínio se descoberta nova/diferente
UPDATE empresa_dominios
SET dominio='{dominio}',
    dominios_alternativos='{alt1,alt2}'::text[],
    validacao_metodo='hunter+websearch_{data}',
    validacao_data=CURRENT_DATE
WHERE cnpj='{cnpj}';

-- Telefone corporativo da obra (fallback quando decisor não tem ramal direto)
UPDATE obras
SET nivel1_telefone='{tel}',
    nivel1_telefone_e164='{tel_e164}',
    nivel1_telefone_status='ok',
    nivel1_origem_enrichment='site_oficial_{empresa}_{data}'
WHERE id='{obra_id}';
```

> Notas de schema:
> - `decisores_obra` tem colunas `nome, cargo, email` (NÃO `nivel1_*`); as `nivel1_*` ficam em `obras` como cache do decisor primário e são populadas via trigger `sync_classificacao_after_decisor`.
> - SMTP verification de email mora em `obras.nivel1_email_smtp_verified` (não em `decisores_obra`); pra promover via PRATA condicional o gate olha `obras.nivel1_email_smtp_verified=true`.
> - Marker do pipeline vai em `registrado_por` (convenção: `enrichment_pipeline_v1:{YYYYMMDD}`); rollback grep por isso.

### Passo 8 — Recompute
```sql
-- SEMPRE este, NUNCA recompute_classificacao_full()
SELECT recompute_classificacao_obra('{obra_id}');
```
Resultado esperado:
- BRONZE → PRATA: email verificado + decisor real (conf ≥ 30)
- PRATA → OURO: conf ≥ 70 + email SMTP verified + LinkedIn

---

## Custo estimado por obra
| Recurso | Calls | Obs |
|---------|-------|-----|
| Serper (LinkedIn) | 2 | Passo 3 |
| Serper (Mari/Google) | 0-4 | Passo 4, se necessário |
| Serper (telefone) | 1-2 | Passo 6 |
| Hunter Email Finder | 2-4 | Passo 5 |
| Anthropic Sonnet | ~$0.05 | Se enriquecimento de descrição |
| **Total Serper** | **5-8** | |
| **Total Hunter** | **2-4** | Reset 11/06, 2000/mês |

---

## Regras anti-alucinação
- **Nunca usar Hunter em domínio não validado**
- Domínios de fonte E2/E3/descoberta_automatica → sempre revalidar via web
- Confirmar que decisor é funcionário **atual** (não ex-)
- Confirmar empresa correta (não homônimo)
- Registrar negative-cache: `confianca_match_componentes.telefone_busca = 'negativo_{data}'`

---

## Rollback
```sql
-- Reverter decisores inseridos por este pipeline
DELETE FROM decisores_obra 
WHERE registrado_por = 'enrichment_pipeline_v1:{data}';

-- Reverter recompute
SELECT recompute_classificacao_obra('{obra_id}');
```

---

## Exemplo real validado
**Obra:** ENGIE Brasil — Plano Investimentos 2026-2030 (R$6bi)
**Resultado:** BRONZE → OURO em ~20 min
**Decisores:** 3 (Transmissão + Renováveis + Implantação Asa Branca)
**Custo:** 4 Hunter + 6 Serper
**Telefone:** corporativo (48) 3221-7000 + ramal Guilherme (48) 3221-7072
