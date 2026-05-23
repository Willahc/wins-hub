-- 2026-05-23 — Flag honesta [capex_indisponivel_fonte]
--
-- Contexto: 642 obras OURO/PRATA/BRONZE/PIPELINE com valor_estimado IS NULL
-- distribuídas em fontes que NÃO declaram valor BRL (antaq_tup outorgas, antt_ferro_pic,
-- dnit notícias operacionais, cvm_ipe comunicados de evento, agenciainfra_wp, dou,
-- noticia_*, revistaoe, unica_usinas).
--
-- Decisão: Caminho B (anti-alucinação). Flag honesta em observacoes_validacao em vez
-- de enrich web custoso. Pipeline CVM_IPE PDF+Sonnet fica como tech debt pós-sprint.
--
-- Idempotente: WHERE NOT LIKE '%capex_indisponivel_fonte%'.
--
-- Resultado: 618 (antaq_tup 442 + antt_ferro_pic 160 + dnit 16) + 24 (cvm_ipe 15 + dou 3 +
-- agenciainfra_wp 2 + outras 4) = 642 obras flagadas.

-- Etapa 1: set onde observações IS NULL
UPDATE obras
SET observacoes_validacao = '[capex_indisponivel_fonte]'
WHERE valor_estimado IS NULL
  AND observacoes_validacao IS NULL
  AND (
    fonte IN ('antt_ferro_pic','antaq_tup','dnit')
    OR (classificacao_computed IN ('OURO','PRATA')
        AND fonte IN ('cvm_ipe','dou','agenciainfra_wp','unica_usinas',
                      'noticia_agenciainfra','noticia_click_petroleo','revistaoe.com.br'))
  );

-- Etapa 2: append onde observações já existem (idempotente via NOT LIKE)
UPDATE obras
SET observacoes_validacao = observacoes_validacao || ' [capex_indisponivel_fonte]'
WHERE valor_estimado IS NULL
  AND observacoes_validacao IS NOT NULL
  AND observacoes_validacao NOT LIKE '%capex_indisponivel_fonte%'
  AND (
    fonte IN ('antt_ferro_pic','antaq_tup','dnit')
    OR (classificacao_computed IN ('OURO','PRATA')
        AND fonte IN ('cvm_ipe','dou','agenciainfra_wp','unica_usinas',
                      'noticia_agenciainfra','noticia_click_petroleo','revistaoe.com.br'))
  );
