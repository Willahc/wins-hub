-- Função v2 — refinamento Sprint 2 (20/05/2026)
-- Adaptada para schema real (dob autônoma; sem FK p/ empresa_decisores_cache)
-- 8 critérios:
--   A. cargo_sufixo_match (+30 / -30 / 0)        [v1, mantido]
--   B. dominio_email_match (+25 / 0)             [v1, mantido]
--   C. cargo_cita_empresa (+20 / 0)              [v1, mantido]
--   D. cargo_compativel_setor (+15 / 0)          [v1, mantido]
--   E. citado_em_descricao (+10 / 0)             [v1, mantido]
--   G. consistencia_intragrupo (+15 / +5 / -15)  [NOVO v2 — agrupado por nome]
--   H. email_corporativo (+10 / 0)               [NOVO v2]
--   (critério F do briefing é redundante c/ A, mantido em A)
-- Score floor=0, ceiling=100
CREATE OR REPLACE FUNCTION calcular_confianca_match_v2(p_dob_id UUID)
RETURNS JSONB AS $$
DECLARE
  v_score INT := 0;
  v_componentes JSONB := '{}'::JSONB;
  v_dob_nome TEXT;
  v_dob_cargo TEXT;
  v_dob_email TEXT;
  v_obra_empresa TEXT;
  v_obra_descricao TEXT;
  v_radical_empresa TEXT;
  v_radical_empresa_unaccent TEXT;
  v_cargo_sufixo TEXT;
  v_dominio_email TEXT;
  v_qtd_cnpj_raiz INT;
BEGIN
  -- Carregar contexto
  SELECT dob.nome, dob.cargo, dob.email, o.empresa, o.descricao
  INTO v_dob_nome, v_dob_cargo, v_dob_email, v_obra_empresa, v_obra_descricao
  FROM decisores_obra dob
  INNER JOIN obras o ON o.id = dob.obra_id
  WHERE dob.id = p_dob_id;

  IF v_dob_nome IS NULL THEN
    RETURN jsonb_build_object('dob_id', p_dob_id, 'erro', 'dob não encontrado');
  END IF;

  -- Radical da empresa
  v_radical_empresa := COALESCE(
    (SELECT word FROM unnest(string_to_array(TRIM(COALESCE(v_obra_empresa,'')), ' ')) AS word
     WHERE LENGTH(word) >= 3
       AND UPPER(word) NOT IN ('LTDA','S/A','S.A','S.A.','SA','EIRELI','SAS','SPE','DA','DE','DO','DAS','DOS','OU','E','A','O','-','EMPRESA','GRUPO')
     LIMIT 1),
    SPLIT_PART(COALESCE(v_obra_empresa,''), ' ', 1)
  );
  v_radical_empresa_unaccent := LOWER(unaccent(COALESCE(v_radical_empresa,'')));

  -- A: cargo_sufixo_match (já implementa o critério F do briefing)
  IF v_dob_cargo IS NOT NULL AND v_obra_empresa IS NOT NULL THEN
    v_cargo_sufixo := LOWER(unaccent(TRIM(
      regexp_replace(v_dob_cargo, '^.*?(?:\sna\s|\sat\s|\s\|\s|\s@\s|\s—\s|\s–\s|\s-\s+)', '', 'i')
    )));
    IF v_cargo_sufixo = LOWER(unaccent(TRIM(v_dob_cargo))) THEN
      v_componentes := v_componentes || jsonb_build_object('cargo_sufixo_match', 0);
    ELSIF v_radical_empresa_unaccent <> '' AND v_cargo_sufixo LIKE '%' || v_radical_empresa_unaccent || '%' THEN
      v_score := v_score + 30;
      v_componentes := v_componentes || jsonb_build_object('cargo_sufixo_match', 30);
    ELSE
      v_score := v_score - 30;
      v_componentes := v_componentes || jsonb_build_object('cargo_sufixo_match', -30);
    END IF;
  ELSE
    v_componentes := v_componentes || jsonb_build_object('cargo_sufixo_match', 0);
  END IF;

  -- B: dominio_email_match
  IF v_dob_email IS NOT NULL AND v_dob_email ~ '@' AND v_radical_empresa_unaccent <> '' THEN
    v_dominio_email := LOWER(SPLIT_PART(v_dob_email, '@', 2));
    IF v_dominio_email LIKE '%' || v_radical_empresa_unaccent || '%' THEN
      v_score := v_score + 25;
      v_componentes := v_componentes || jsonb_build_object('dominio_email_match', 25);
    ELSE
      v_componentes := v_componentes || jsonb_build_object('dominio_email_match', 0);
    END IF;
  ELSE
    v_componentes := v_componentes || jsonb_build_object('dominio_email_match', 0);
  END IF;

  -- C: cargo_cita_empresa
  IF v_dob_cargo IS NOT NULL AND v_radical_empresa_unaccent <> '' THEN
    IF LOWER(unaccent(v_dob_cargo)) LIKE '%' || v_radical_empresa_unaccent || '%' THEN
      v_score := v_score + 20;
      v_componentes := v_componentes || jsonb_build_object('cargo_cita_empresa', 20);
    ELSE
      v_componentes := v_componentes || jsonb_build_object('cargo_cita_empresa', 0);
    END IF;
  ELSE
    v_componentes := v_componentes || jsonb_build_object('cargo_cita_empresa', 0);
  END IF;

  -- D: cargo_compativel_setor
  IF v_dob_cargo IS NOT NULL AND v_dob_cargo ~* '(suprim|compras|comprador|procurement|sourcing|buyer|engenh|engineer|industri|capex|projetos|projects|operations|operacoe|manuten|maintenance|diretor|director|presidente|chief|gerente|coordenad|head|supply\s*chain)' THEN
    v_score := v_score + 15;
    v_componentes := v_componentes || jsonb_build_object('cargo_compativel_setor', 15);
  ELSE
    v_componentes := v_componentes || jsonb_build_object('cargo_compativel_setor', 0);
  END IF;

  -- E: citado_em_descricao
  IF v_obra_descricao IS NOT NULL AND v_dob_nome IS NOT NULL
     AND LENGTH(v_obra_descricao) BETWEEN 10 AND 20000 THEN
    IF LOWER(unaccent(v_obra_descricao)) LIKE '%' || LOWER(unaccent(v_dob_nome)) || '%' THEN
      v_score := v_score + 10;
      v_componentes := v_componentes || jsonb_build_object('citado_em_descricao', 10);
    ELSE
      v_componentes := v_componentes || jsonb_build_object('citado_em_descricao', 0);
    END IF;
  ELSE
    v_componentes := v_componentes || jsonb_build_object('citado_em_descricao', 0);
  END IF;

  -- G: consistencia_intragrupo (NOVO v2)
  -- Conta quantos cnpj-raízes distintos esse mesmo nome aparece (entre obras ativas)
  -- 1 raiz = grupo coeso (+15), 2-3 = holding plausível (+5), >5 = wire-in falso (-15)
  SELECT COUNT(DISTINCT SUBSTRING(o2.cnpj FROM 1 FOR 8))
  INTO v_qtd_cnpj_raiz
  FROM decisores_obra dob2
  INNER JOIN obras o2 ON o2.id = dob2.obra_id
  WHERE dob2.nome = v_dob_nome
    AND dob2.excluido_em IS NULL
    AND o2.cnpj IS NOT NULL
    AND LENGTH(o2.cnpj) >= 8;

  IF v_qtd_cnpj_raiz = 1 THEN
    v_score := v_score + 15;
    v_componentes := v_componentes || jsonb_build_object('consistencia_intragrupo', 15);
  ELSIF v_qtd_cnpj_raiz BETWEEN 2 AND 3 THEN
    v_score := v_score + 5;
    v_componentes := v_componentes || jsonb_build_object('consistencia_intragrupo', 5);
  ELSIF v_qtd_cnpj_raiz > 5 THEN
    v_score := v_score - 15;
    v_componentes := v_componentes || jsonb_build_object('consistencia_intragrupo', -15);
  ELSE
    v_componentes := v_componentes || jsonb_build_object('consistencia_intragrupo', 0);
  END IF;

  -- H: email_corporativo (NOVO v2)
  -- Email não-pessoal (não-gmail/hotmail/etc) sinaliza credibilidade
  IF v_dob_email IS NOT NULL
     AND v_dob_email !~* '@(gmail|hotmail|yahoo|outlook|uol|terra|bol|live|icloud|me|mail)\.'
     AND v_dob_email ~ '@' THEN
    v_score := v_score + 10;
    v_componentes := v_componentes || jsonb_build_object('email_corporativo', 10);
  ELSE
    v_componentes := v_componentes || jsonb_build_object('email_corporativo', 0);
  END IF;

  -- Clamp [0, 100]
  v_score := GREATEST(0, LEAST(100, v_score));

  UPDATE decisores_obra
  SET
    confianca_match = v_score,
    confianca_match_componentes = v_componentes,
    confianca_match_calculada_em = NOW()
  WHERE id = p_dob_id;

  RETURN jsonb_build_object(
    'dob_id', p_dob_id,
    'score', v_score,
    'componentes', v_componentes,
    'qtd_cnpj_raiz', v_qtd_cnpj_raiz
  );
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION calcular_confianca_match_v2(UUID) IS
  'Score v2 (Sprint 2): 5 critérios v1 + G consistencia_intragrupo (resolve Eliclea/Felipe pattern) + H email_corporativo. Score 0-100.';
