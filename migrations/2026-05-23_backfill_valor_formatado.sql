-- 2026-05-23 — Backfill valor_formatado a partir de valor_estimado
--
-- Contexto: 657 obras visíveis (motivo_invisivel IS NULL) têm valor_estimado
-- populado mas valor_formatado vazio/NULL. Captadores novos esquecem de popular.
--
-- Formato canônico (segue padrão existente, capitalização lowercase):
--   < 1.000        → "R$ N"
--   < 1.000.000    → "R$ N k"
--   < 1.000.000.000 → "R$ N mi"
--   >= 1.000.000.000 → "R$ N bi"
-- N é arredondado para inteiro (matching padrão dominante das amostras).
--
-- Idempotente: WHERE (valor_formatado IS NULL OR valor_formatado = '').

UPDATE obras
SET valor_formatado = CASE
  WHEN valor_estimado >= 1000000000 THEN
    'R$ ' || ROUND(valor_estimado / 1000000000.0)::text || ' bi'
  WHEN valor_estimado >= 1000000 THEN
    'R$ ' || ROUND(valor_estimado / 1000000.0)::text || ' mi'
  WHEN valor_estimado >= 1000 THEN
    'R$ ' || ROUND(valor_estimado / 1000.0)::text || ' k'
  ELSE
    'R$ ' || ROUND(valor_estimado)::text
END
WHERE valor_estimado IS NOT NULL
  AND (valor_formatado IS NULL OR valor_formatado = '')
  AND motivo_invisivel IS NULL;
