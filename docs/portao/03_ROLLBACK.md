# Rollback

1. Desligar flags: `psql -f sql/02_ROLLBACK_PORTAO.sql`
2. Restart API
3. Site volta a usar regras de `visivel` + `status_portao IS NULL OR APROVADA` (histórico intacto)
4. Auditoria `wins_v2.portao_decisoes` é preservada (append-only)
