# Enriquecimento orientado por lacunas

## Princípio
1. Diagnosticar lacunas de todas as APROVADAS
2. Reaproveitar dados internos (lookup CNPJ, empresa_dominios, decisores irmãos, preservados)
3. Consulta externa só em FULL_MISS (fase controlada)
4. OURO só com 8 critérios; telefone é bônus

## Tabelas
- `wins_v2.enrichment_gap_matrix` — completude e próxima ação
- `wins_v2.enrichment_gap_snapshot` — baseline pré-execução
- `wins_v2.enrichment_gap_audit` — append-only

## Script
`scripts/enrichment/enriquecer_por_lacunas.py --apply`

## Reclassificação
`scripts/auditoria/aplicar_regra_ouro_final.py --apply`

## Rollback
`sql/rollback/20260717_enrichment_gap_rollback.sql`
