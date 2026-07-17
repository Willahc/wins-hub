# Plano de Rollback

O rollback operacional nao reinicia banco ou Nginx e nao reverte a credencial
ja rotacionada.

1. Alterar atomicamente `MASTER_CNPJ_LOOKUP_ENABLED=false` em
   `/root/wins_hub/.env`.
2. Recriar somente a API com `docker compose up -d --no-deps --force-recreate api`.
3. Confirmar flag `false`, site HTTP 200 e `/healthz` HTTP 200.
4. Se for necessario restaurar codigo, usar as copias em
   `rollback/before_state_reconciliation_20260716/` e manter a flag desativada.
5. Reaplicar `14_SQL_PERMISSOES_MINIMAS.sql` depois de qualquer manutencao no
   schema `wins_v2`.

O schema e os dados V2 podem permanecer para auditoria com a feature desligada.
Os dumps `wins_v2_schema_before.sql` e `wins_v2_lookup_before.sql` sao uma
ultima alternativa e nao sao necessarios para o rollback normal. Uma
restauracao destrutiva exige novo backup e janela de manutencao.
