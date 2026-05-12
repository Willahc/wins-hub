# ISSUE-001 — Inconsistência de escala em `obras.urgencia`

**Status:** ✅ **RESOLVIDO em 2026-04-30**
**Identificado em:** 2026-04-30
**Contexto onde foi descoberto:** revisão de UPSERTs dos captadores (PR de `urgencia/necessidades` no SET).

## Resolução

Aplicado em 2026-04-30 num único PR. A escala foi padronizada para **1 = mais urgente** em todo o sistema.

### Mudanças nos captadores

| Captador | Antes | Depois |
|---|---|---|
| `captar_bndes.py:201` | `4 if valor>=500Mi else 3` | `1 if valor>=500Mi else 2` |
| `captar_ibama.py:354` | `4 if is_pac else 3` | `1 if is_pac else 2` |
| `captar_aneel.py:235` | constante `3` | constante `2` |
| `captar_antaq.py:232` | constante `3` | constante `2` |
| `captar_anm.py:306` | constante `3` | constante `2` |
| `captar_cvm.py:332` | constante `3` | constante `2` |
| `app/main.py:153` (`calc_urgencia`) | já estava em `{1,2,3}` | sem mudança — já correto |

### Backfill aplicado (uma transação, COMMIT confirmado)

```sql
UPDATE obras SET urgencia = 1 WHERE urgencia = 4;
-- 149 linhas (76 BNDES ≥500Mi + 73 IBAMA PAC)

UPDATE obras SET urgencia = 2
WHERE urgencia = 3
  AND fonte IN ('bndes_financiamento','ibama_sislic','aneel_siga',
                'antaq_tup','anm_cfem','cvm_ipe');
-- 9.960 linhas
```

### Distribuição final

| urgencia | count | semântica |
|---:|---:|---|
| 1 | 149 | top — BNDES grandes + IBAMA PAC |
| 2 | 9.960 | default das 6 fontes migradas |
| 3 | 1.814 | fontes não tocadas (PLANILHA, antt_*, anac_concessao, anp_ep, debentures_infra, abiove_processadoras, unica_usinas, mapa_sif, aneel_transmissao, manual) |

### Verificação

`ORDER BY urgencia ASC, lead_score DESC` em `main.py:301` agora retorna como top 5:

```
urg=1, lead=90, ibama_sislic         | Ferrovia Transnordestina - Trecho Salgueiro - Suap
urg=1, lead=85, bndes_financiamento  | IMPLANTACAO DOS PLANOS DE INVESTIMENTOS DA COELBA
urg=1, lead=85, bndes_financiamento  | IMPLANTACAO DE OITO USINAS FOTOVOLTAICAS
urg=1, lead=85, bndes_financiamento  | IMPLANTACAO DOS PLANOS DE INVESTIMENTOS DAS CONCES
urg=1, lead=85, bndes_financiamento  | IMPLANTACAO DE DEZOITO USINAS FOTOVOLTAICAS
```

— exatamente o oposto do comportamento anterior (onde essas obras ficavam no fim).

### Pendência consciente

As 1.814 obras em `urgencia=3` (fontes não migradas) ficam em estado coerente sob a nova escala
(`3 = menos urgente`), mas representam um risco residual: se algum dia descobrirmos que algum
desses captadores também usava a escala invertida, será preciso uma segunda passagem. Hoje, sem
o código fonte de cada um, é mais seguro deixar como está.

---



## Problema

A coluna `obras.urgencia` é populada em três lugares do sistema, com convenções de escala
**incompatíveis entre si**, e a query de listagem ordena no sentido errado para metade das fontes.

### Convenções atuais

| Origem | Código | Mais urgente = | Faixa observada |
|---|---|---|---|
| `app/main.py:153` (`calc_urgencia(fase)`) — usado em criação manual via `POST /api/obras` | `{LICITACAO_ABERTA: 1, CONTRATACAO: 1, LICENCA_INSTALACAO: 2, FINANCIAMENTO_BNDES: 2, LICENCA_PREVIA: 3, EM_EXECUCAO: 3}` | **valor BAIXO** (`1`) | 1, 2, 3 |
| `app/scripts/captar_bndes.py:201` | `4 if valor_estimado >= 500_000_000 else 3` | **valor ALTO** (`4`) | 3, 4 |
| `app/scripts/captar_ibama.py:354` | `4 if pac == "Sim" else 3` | **valor ALTO** (`4`) | 3, 4 |
| Demais captadores (anm, aneel, antaq, cvm) | `3` constante | — | 3 |

### Query que sofre

`app/main.py:301` — listagem de obras:

```sql
ORDER BY urgencia ASC, lead_score DESC
```

`ASC` faz com que **valores menores venham primeiro**. Isso é coerente com a escala do `calc_urgencia` (1=mais urgente), mas é **o oposto** da escala dos captadores BNDES/IBAMA.

### Consequência prática

- 1 obra em `LICITACAO_ABERTA` com `urgencia=1` (criada via POST manual): aparece **no topo** ✓
- 149 obras com `urgencia=4` (BNDES grandes ≥ 500Mi e IBAMA do programa PAC): aparecem **no fim** ✗
- 11.774 obras com `urgencia=3`: bloco do meio, ordenadas por `lead_score`

Ou seja: as obras que os captadores marcaram como **mais urgentes** ficam **menos visíveis** na UI
porque a ordenação as joga para o fim. É exatamente o oposto da intenção de produto.

## Correção proposta

Padronizar **`1 = mais urgente`** em todo o sistema (mantém `ORDER BY ASC` da query inalterado).

Migração nos captadores:

| Antes | Depois |
|---|---|
| `urgencia = 4` (BNDES valor ≥ 500Mi, IBAMA PAC) | `urgencia = 1` |
| `urgencia = 3` (default) | `urgencia = 2` |

E backfill nos dados existentes:

```sql
UPDATE obras SET urgencia = 1 WHERE urgencia = 4;
UPDATE obras SET urgencia = 2 WHERE urgencia = 3 AND fonte IN
    ('bndes_financiamento','ibama_sislic','aneel_siga','antaq_tup',
     'anm_cfem','cvm_ipe','aneel_transmissao','antt_rod','antt_ferro_pic',
     'anac_concessao','anp_ep','debentures_infra','abiove_processadoras',
     'unica_usinas','mapa_sif','PLANILHA','manual');
-- Cuidado: obras criadas via POST /api/obras já usam escala correta (1/2/3),
-- então o UPDATE acima precisa filtrar por fontes ou por urgência != 1.
```

(O filtro exato precisa ser revisto — uma forma defensiva é atualizar
**apenas as 149 obras com `urgencia = 4`** primeiro, e tratar a migração
do default `3 → 2` como fase 2 separada.)

Plus: deduplicar a lógica — hoje `calc_urgencia` em `main.py` e os literais nos
captadores são definições paralelas. Idealmente, mover para um util único
(`app/services/urgencia.py` ou similar) que toma `(fase, valor, is_pac)` e
retorna 1/2/3.

## Por que não foi corrigido neste PR

O PR atual (`urgencia/necessidades` no SET) só **propaga** os valores que os captadores
já calculam — não altera a escala. Corrigir a escala muda o comportamento de UI, requer
backfill de dados, e merece ser tratado isoladamente para facilitar revisão e rollback.

## Esforço estimado

- Backfill SQL: 1 transação curta, ~5min para escrever + revisar
- Atualização dos 6 captadores (e do helper `calc_urgencia`): ~30min
- Smoke test (criar obra com cada fase e verificar ordenação): ~15min
- Total: ~1h
