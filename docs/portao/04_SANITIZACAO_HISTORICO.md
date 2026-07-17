# Sanitização histórica do Portão

## Etapas executadas
1. Snapshot + rollback table
2. Decisões determinísticas em lotes (500→1000→2000)
3. EM_ANALISE residual → EM_ANALISE_MANUAL (invisível)
4. Restauração de tiers comerciais do snapshot nas APROVADAS
5. Remoção da graça `status_portao IS NULL` nas APIs públicas

## Reversão
```bash
sudo docker exec -e PGPASSWORD=$PW wins_hub-db-1 \
  psql -U postgres -d wins_hub -f /tmp/04_ROLLBACK_HISTORICO_COMPLETO.sql
```
