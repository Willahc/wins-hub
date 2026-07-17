# Rollback operacional

## Código
- Bundle pré-consolidação: /home/william/wins_hub_pre_consolidacao_20260717.bundle
- Tarball app: /home/william/backups_consolidacao_20260717/wins_hub_app_pre_consolidacao_20260717.tgz

## Portão histórico
- sql/rollback/04_ROLLBACK_HISTORICO_COMPLETO.sql
- Snapshot: wins_v2.portao_snapshot_pre_historico

## Bronze
- sql/rollback/02_ROLLBACK_BRONZE.sql
- Snapshot: wins_v2.bronze_enrich_snapshot

## Portão schema
- sql/rollback/02_ROLLBACK_PORTAO.sql

## CNPJ master / pipeline V2
- Ver docs/cnpj_master/11_PLANO_ROLLBACK.md e docs/pipeline_v2/11_PLANO_ROLLBACK.md
