-- Migration: 2026-05-22 — Ampliar setor_cnae_compatibility PETROLEO_GAS
--
-- Antes: scc(PETROLEO_GAS) tinha 5 CNAEs (0600001/0910600/1921700/4292801/7112000),
-- destes só 4 passavam peso_min=0.5 do time-ideal. Resultado: obras petróleo
-- (Tupi/Lula, Búzios, Marlim, Roncador) tinham todos os matches concentrados em
-- 1 categoria só (Petróleo, Gás e E&P).
--
-- Realidade: Petrobras/Shell/Equinor contratam pesadamente Caldeiraria,
-- Tubulação Industrial, Instrumentação, Manutenção Industrial, Engenharia
-- Offshore, Dutos, HSE — todas categorias de fornecimento legítimo pra E&P.
--
-- Fix: para cada categoria abaixo, INSERT (PETROLEO_GAS, cnae, peso) com
-- ON CONFLICT GREATEST pra não rebaixar pesos preexistentes (ex.: 7112000
-- já estava como peso=0.3 — GREATEST mantém ou eleva, nunca abaixa).
--
-- 12 categorias incluídas. "Inspeção e Ensaios NDT" do brief não existe em
-- categorias_servico (skipado). Pesos conforme brief; "Consultoria e Engenharia"
-- (0.45) fica abaixo do peso_min default (0.5) — não aparece no time-ideal
-- mas pode ser puxado pelo engine v2 (pre-rank candidatos).

BEGIN;

WITH cat_pesos(codigo, peso) AS (VALUES
    ('CALD_SOL', 0.70::numeric),  -- Caldeiraria e Soldagem
    ('TUBU_IND', 0.70),  -- Tubulação Industrial
    ('INST_AUT', 0.65),  -- Instrumentação e Automação
    ('MANU_IND', 0.65),  -- Manutenção Industrial
    ('OFFS_ENG', 0.80),  -- Engenharia Offshore
    ('DUTO_GAS', 0.75),  -- Dutos e Gasodutos
    ('PROC_OGS', 0.80),  -- Sistemas de Processo Oil Gas
    ('ISOL_TER', 0.60),  -- Isolamento Térmico
    ('ICAT_PES', 0.55),  -- Içamento e Transporte Pesado
    ('SEG_TRA',  0.55),  -- Segurança do Trabalho (HSE)
    ('MEIO_AMB', 0.50),  -- Meio Ambiente e Licenciamento
    ('CONS_ENG', 0.45)   -- Consultoria e Engenharia (abaixo do default peso_min)
),
expandido AS (
  -- 1 row por (cnae, codigo); CNAE pode aparecer em N categorias → vai gerar N rows
  SELECT unnest(cs.cnaes) AS cnae_codigo, cp.peso
  FROM cat_pesos cp
  JOIN categorias_servico cs ON cs.codigo = cp.codigo
),
dedup AS (
  -- Dedup por cnae: pega o maior peso quando o mesmo CNAE estiver em
  -- multiplas categorias (ex.: 7112000 em CALD_SOL/CONS_ENG/OFFS_ENG/etc.).
  -- ON CONFLICT cobre só conflict com row preexistente, não com row da mesma
  -- batch — por isso fazemos o GROUP BY antes.
  SELECT cnae_codigo, MAX(peso) AS peso
  FROM expandido
  GROUP BY cnae_codigo
)
INSERT INTO setor_cnae_compatibility (setor_obra, cnae_codigo, peso, fases_aplicaveis)
SELECT
  'PETROLEO_GAS',
  cnae_codigo,
  peso,
  ARRAY['PLANEJAMENTO','LICITACAO_ABER','EM_EXECUCAO','OPERACAO']
FROM dedup
ON CONFLICT (setor_obra, cnae_codigo) DO UPDATE
  SET peso = GREATEST(setor_cnae_compatibility.peso, EXCLUDED.peso);

REFRESH MATERIALIZED VIEW cnaes_interesse;

COMMIT;

-- Verify
SELECT scc.cnae_codigo, scc.peso, ARRAY_AGG(DISTINCT cs.nome ORDER BY cs.nome) AS categorias
FROM setor_cnae_compatibility scc
LEFT JOIN categorias_servico cs ON scc.cnae_codigo = ANY(cs.cnaes)
WHERE scc.setor_obra = 'PETROLEO_GAS'
GROUP BY scc.cnae_codigo, scc.peso
ORDER BY scc.peso DESC, scc.cnae_codigo;
