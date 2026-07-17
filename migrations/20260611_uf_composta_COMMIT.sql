\timing on
BEGIN;
-- A) COMPOSTAS -> primeira UF válida (original em observacoes_validacao)
UPDATE obras o SET
   uf = upper(split_part(o.uf,'/',1)),
   observacoes_validacao = COALESCE(o.observacoes_validacao,'') || ' | uf_orig:' || o.uf || ' auto:uf_split_v1'
 WHERE o.visivel=true
   AND o.uf ~ '^[A-Z]{2}(/[A-Z]{2})+$'
   AND upper(split_part(o.uf,'/',1)) IN (SELECT DISTINCT uf_obra FROM uf_proximidade);
-- B) LIXO -> NULL
UPDATE obras o SET
   uf = NULL,
   observacoes_validacao = COALESCE(o.observacoes_validacao,'') || ' | uf_orig:[' || COALESCE(o.uf,'') || '] auto:uf_null_v1'
 WHERE o.visivel=true AND o.uf IS NOT NULL
   AND o.uf !~ '^[A-Z]{2}(/[A-Z]{2})+$'
   AND NOT EXISTS (SELECT 1 FROM uf_proximidade u WHERE u.uf_obra=o.uf);
COMMIT;
