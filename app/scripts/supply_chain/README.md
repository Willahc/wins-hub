# Supply Chain — Ciclo Completo da Obra

Mapeia o ciclo industrial ponta a ponta num grafo único: **OBRA (dono+decisor) → EXECUTOR → INSUMOS → FORNECEDOR-DE-INSUMO**, com decisor em cada nó. Único no mercado BR (concorrentes mapeiam obras OU empresas, ninguém fecha o ciclo).

## Camadas e tabelas
| Camada | Tabela | Como é gerada |
|---|---|---|
| 1. Obra + decisor-dono | `obras` + `decisores_obra` | captadores + enrichment |
| 2. Executor (quem faz) | `matches_obra_prestador` | matchmaking |
| Impacto econômico | `obras_impacto_economico` | Leontief IBGE (`impacto_economico.py`) |
| 3a. Demanda de insumo | `matches_cadeia_obra` | `materializar_cadeia_obra.py` (Leontief × CAPEX por divisão CNAE) |
| 3b. Fornecedor de insumo | `matches_cadeia_fornecedor` | `materializar_matches_fornecedor.sql` (top por div+UF, score) |
| Decisor do fornecedor | `empresa_decisores_cache` | `serper_haiku_decisor_sc.py` (Serper→Haiku, cron 06:00) |

## Scripts (nesta pasta)
- **etl_rfb_insumos_vps.py** — ETL disk-safe (mirror Casa dos Dados, baixa-filtra-apaga 1 partição/vez) → CSV de fornecedores CNAE 22/23/24. Rodado 22/06: 117k fornecedores em ~7min.
- **etl_rfb_insumos_PC.py** — versão p/ rodar no PC do usuário (lê pasta de zips já baixados).
- **etl_rfb_insumos_DRAFT.py** — esboço inicial (URL antiga, mantido p/ referência).
- **importar_insumos.sh** — importa o CSV em `fornecedores` (staging + WHERE NOT EXISTS, `status='importado_sc'`, reversível).
- **pos_import_insumos.sh** — re-materializa cadeia + antes/depois.
- **materializar_matches_fornecedor.sql** — gera os leads reais obra↔fornecedor (Camada 3b).
- **enriquecimento_web_obras_20260622.sql** — inteligência da obra via web (escopo/material/cronograma reais) nas 6 maiores OURO.

## Estado 22/06/2026
- +117.004 fornecedores de aço/cimento/plástico importados (validados vs BrasilAPI).
- 60.551 matches obra↔fornecedor (1.165 obras, 82% mesmo UF).
- 6 obras top enriquecidas com demanda real (não média de setor).
- ⚠️ Pago bloqueado: Anthropic sem crédito (Haiku/decisor); Hunter reseta 11/07. Crons armados retomam sozinhos.

## Regra de trabalho
**Free-first**: sempre o que dá pra fazer grátis primeiro (SQL no banco, IBGE, RFB+BrasilAPI, PNCP); pago é upgrade consciente. Ver memória `feedback_free_first_depois_pago`.
