# Baselines do Portão — medidos em 2026-06-18 (janela: últimos 30 dias)

Valores de referência. O `run_harness.sh` sinaliza DRIFT quando algo se afasta muito.

## Funil (01_funil.sql) — total 30d: **2.577**
| métrica | valor |
|---|---|
| pass_cnpj | 2.002 |
| pass_setor | 2.560 |
| pass_capex_real | 1.621 |
| pass_capex_inc_estim | 1.971 |
| pass_local | 1.550 |
| **pass_TODOS_literal** (AND rígido) | **762** |
| pass_TODOS_inc_estim | 910 |

## Sanidade (02_sanidade.sql) — por que município/estimativa são SOFT
- **Descartado pelo portão literal mas hoje útil: 1.122** (596 OURO + 265 PRATA + 261 BRONZE).
- Portão literal mantém só 318 úteis (+442 PIPELINE). ⇒ AND rígido jogaria fora **78% do útil**.

## Motivos (03_motivos.sql)
| critério | falham (30d) |
|---|---|
| falha_cnpj | 0 |
| falha_setor | 17 |
| falha_capex_real | 671 |
| falha_local | 1.027 (dos quais **639 OURO/PRATA**) |
| só_estimativa_senão_passava | 350 |

## Resolução interna (04_resolucao_interna.sql) — Fase 0, custo zero
Base: 2.002 obras 30d com CNPJ válido.
| fonte interna | resolve | % |
|---|---|---|
| CNPJ full em fornecedores | 921 | 46% |
| **CNPJ raiz em fornecedores** | **1.051** | **52,5%** |
| **domínio interno** (empresa_dominios) | **1.106** | **55%** |
| **decisor interno c/ email** (decisores_preservados) | **782** | **39%** |

## Índice criado (Fase 0)
- `idx_fornecedores_cnpj_raiz` ON fornecedores (substring(cnpj,1,8)) — **116 MB**, CONCURRENTLY, válido.
- Lookup por raiz: 40s seq-scan → Bitmap Index Scan (~ms).
