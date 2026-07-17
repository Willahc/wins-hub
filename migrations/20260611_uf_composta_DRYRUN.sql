\timing on
BEGIN;

-- ANTES
SELECT 'A_compostas_antes' m, COUNT(*) n FROM obras
 WHERE visivel=true AND uf ~ '^[A-Z]{2}(/[A-Z]{2})+$';
SELECT 'B_lixo_antes' m, COUNT(*) n FROM obras
 WHERE visivel=true AND uf IS NOT NULL
   AND uf !~ '^[A-Z]{2}(/[A-Z]{2})+$'
   AND NOT EXISTS (SELECT 1 FROM uf_proximidade u WHERE u.uf_obra=obras.uf);

-- A) COMPOSTAS -> primeira UF (válida); registra original em observacoes_validacao
UPDATE obras o SET
   uf = upper(split_part(o.uf,'/',1)),
   observacoes_validacao = COALESCE(o.observacoes_validacao,'') || ' | uf_orig:' || o.uf || ' auto:uf_split_v1'
 WHERE o.visivel=true
   AND o.uf ~ '^[A-Z]{2}(/[A-Z]{2})+$'
   AND upper(split_part(o.uf,'/',1)) IN (SELECT DISTINCT uf_obra FROM uf_proximidade);

-- B) LIXO (vazio/MÚ/NE) -> NULL (sai do alvo do matchmaker; para de reprocessar pra sempre)
UPDATE obras o SET
   uf = NULL,
   observacoes_validacao = COALESCE(o.observacoes_validacao,'') || ' | uf_orig:[' || COALESCE(o.uf,'') || '] auto:uf_null_v1'
 WHERE o.visivel=true AND o.uf IS NOT NULL
   AND o.uf !~ '^[A-Z]{2}(/[A-Z]{2})+$'
   AND NOT EXISTS (SELECT 1 FROM uf_proximidade u WHERE u.uf_obra=o.uf);

-- DEPOIS
SELECT 'A_split_aplicado' m, COUNT(*) n FROM obras WHERE observacoes_validacao LIKE '%auto:uf_split_v1%';
SELECT 'A_distribuicao_nova_uf' m, uf, COUNT(*) FROM obras WHERE observacoes_validacao LIKE '%auto:uf_split_v1%' GROUP BY uf ORDER BY 3 DESC;
SELECT 'B_null_aplicado' m, COUNT(*) n FROM obras WHERE observacoes_validacao LIKE '%auto:uf_null_v1%';
SELECT 'restam_uf_invalida (esperado 0)' m, COUNT(*) n FROM obras
 WHERE visivel=true AND uf IS NOT NULL AND NOT EXISTS (SELECT 1 FROM uf_proximidade u WHERE u.uf_obra=obras.uf);

ROLLBACK;
