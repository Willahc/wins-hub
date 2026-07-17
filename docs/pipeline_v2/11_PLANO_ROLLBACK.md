# Plano de Rollback — Pipeline Mestre V2

## Desativacao imediata (sem perda de dados V1)

1. Em `/root/wins_hub/.env` definir:
   ```
   MASTER_PIPELINE_V2_ENABLED=false
   ```
2. Recriar somente a API:
   ```bash
   sudo docker compose -f /root/wins_hub/docker-compose.yml --project-directory /root/wins_hub \
     up -d --no-deps --force-recreate api
   ```
3. Confirmar:
   ```bash
   docker exec wins_hub-api-1 printenv MASTER_PIPELINE_V2_ENABLED
   # false
   ```

Com a flag em `false`, os captadores continuam gravando apenas na V1.
O hook retorna `DISABLED` e o inbox pode continuar recebendo linhas leves
(trigger), mas **nenhum** registro novo e processado no pipeline mestre.

## Remocao do trigger de inbox (opcional)

```sql
DROP TRIGGER IF EXISTS trg_obras_pipeline_inbox ON public.obras;
```

## Remocao de codigo (opcional)

- `/app/services/master_pipeline_v2.py`
- `/app/scripts/_master_hook.py`
- `/app/scripts/exportar_planilha_mestre_v2.py`
- `/app/scripts/reprocessar_falhas_pipeline_v2.py`
- chamadas `notificar_master_v2` nos captadores (fail-safe: import falho e no-op)

## Dados

Dados ja gravados em `wins_v2.capturas_brutas` / evidencias **nao** afetam a V1.
Nao e necessario truncar para restaurar o comportamento legado.

## Nao fazer no rollback

- Nao reiniciar PostgreSQL
- Nao reiniciar Nginx
- Nao apagar `public.obras`
- Nao reverter `entidades_lookup` (independente)
