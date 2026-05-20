-- Função adaptada para schema real (decisores_obra é autônoma, sem FK p/ empresa_decisores_cache)
-- Critérios:
--  A. cargo_sufixo_match (+30 / -30 / 0): extrai empresa do cargo ("na X", "at X", "| X", "@ X")
--     +30 se sufixo == primeira palavra significativa de obras.empresa
--     -30 se sufixo cita outra empresa diferente de obras.empresa (captura o bug Eduardo Ayres)
--      0 sem sufixo (sem informação)
--  B. dominio_email_match (+25): email do decisor tem domínio relacionado ao radical do nome empresa
--  C. cargo_cita_empresa (+20): cargo (sem extração) contém substring do nome da empresa
--  D. cargo_compativel_setor (+15): cargo bate regex genérico de função (compras/eng/etc)
--  E. citado_em_descricao (+10): nome do decisor aparece em obras.descricao
-- Score range: -30 a 100; clamped to 0 on storage minimum
CREATE OR REPLACE FUNCTION calcular_confianca_match(p_dob_id UUID)
RETURNS JSONB AS $$
DECLARE
  v_score INT := 0;
  v_componentes JSONB := '{}'::JSONB;
  v_dob_nome TEXT;
  v_dob_cargo TEXT;
  v_dob_email TEXT;
  v_obra_id UUID;
  v_obra_empresa TEXT;
  v_obra_cnpj TEXT;
  v_obra_descricao TEXT;
  v_radical_empresa TEXT;
  v_radical_empresa_unaccent TEXT;
  v_cargo_sufixo TEXT;
  v_dominio_email TEXT;
  v_resultado JSONB;
BEGIN
  -- Carregar contexto
  SELECT dob.nome, dob.cargo, dob.email, dob.obra_id,
         o.empresa, o.cnpj, o.descricao
  INTO v_dob_nome, v_dob_cargo, v_dob_email, v_obra_id,
       v_obra_empresa, v_obra_cnpj, v_obra_descricao
  FROM decisores_obra dob
  INNER JOIN obras o ON o.id = dob.obra_id
  WHERE dob.id = p_dob_id;

  IF v_dob_nome IS NULL THEN
    RETURN jsonb_build_object('dob_id', p_dob_id, 'erro', 'dob não encontrado');
  END IF;

  -- Radical da empresa: primeira palavra significativa (>=3 chars, ignora "A", "DA", "DE", "DO")
  v_radical_empresa := COALESCE(
    (SELECT word FROM unnest(string_to_array(TRIM(COALESCE(v_obra_empresa,'')), ' ')) AS word
     WHERE LENGTH(word) >= 3 AND UPPER(word) NOT IN ('LTDA','S/A','S.A','S.A.','SA','EIRELI','SAS','SPE','DA','DE','DO','DAS','DOS','OU','E','A','O','-','EMPRESA','GRUPO') LIMIT 1),
    SPLIT_PART(COALESCE(v_obra_empresa,''), ' ', 1)
  );
  v_radical_empresa_unaccent := LOWER(unaccent(COALESCE(v_radical_empresa,'')));

  -- =========================================================================
  -- Critério A: cargo_sufixo_match (-30 / 0 / +30)
  -- Extrai trecho após "na ", "at ", "| ", "@ ", "—" no cargo
  -- =========================================================================
  IF v_dob_cargo IS NOT NULL AND v_obra_empresa IS NOT NULL THEN
    v_cargo_sufixo := LOWER(unaccent(TRIM(
      regexp_replace(
        v_dob_cargo,
        '^.*?(?:\sna\s|\sat\s|\s\|\s|\s@\s|\s—\s|\s-\s+)',
        '',
        'i'
      )
    )));
    -- Se o regex não casou, sufixo == cargo todo. Detectamos isso:
    IF v_cargo_sufixo = LOWER(unaccent(TRIM(v_dob_cargo))) THEN
      -- Sem padrão de sufixo → 0
      v_score := v_score + 0;
      v_componentes := v_componentes || jsonb_build_object('cargo_sufixo_match', 0);
    ELSIF v_radical_empresa_unaccent <> '' AND v_cargo_sufixo LIKE '%' || v_radical_empresa_unaccent || '%' THEN
      v_score := v_score + 30;
      v_componentes := v_componentes || jsonb_build_object('cargo_sufixo_match', 30);
    ELSE
      -- Sufixo cita OUTRA empresa → penalidade
      v_score := v_score - 30;
      v_componentes := v_componentes || jsonb_build_object('cargo_sufixo_match', -30);
    END IF;
  ELSE
    v_componentes := v_componentes || jsonb_build_object('cargo_sufixo_match', 0);
  END IF;

  -- =========================================================================
  -- Critério B: dominio_email_match (+25 / 0)
  -- =========================================================================
  IF v_dob_email IS NOT NULL AND v_dob_email ~ '@' AND v_radical_empresa_unaccent <> '' THEN
    v_dominio_email := LOWER(SPLIT_PART(v_dob_email, '@', 2));
    -- Match se o radical aparece no domínio (ex: "vale@vale.com.br" matches "VALE S.A")
    IF v_dominio_email LIKE '%' || v_radical_empresa_unaccent || '%' THEN
      v_score := v_score + 25;
      v_componentes := v_componentes || jsonb_build_object('dominio_email_match', 25);
    ELSE
      v_componentes := v_componentes || jsonb_build_object('dominio_email_match', 0);
    END IF;
  ELSE
    v_componentes := v_componentes || jsonb_build_object('dominio_email_match', 0);
  END IF;

  -- =========================================================================
  -- Critério C: cargo_cita_empresa (+20 / 0)
  -- Match relaxado: cargo todo contém radical da empresa
  -- =========================================================================
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

  -- =========================================================================
  -- Critério D: cargo_compativel_setor (+15 / 0)
  -- =========================================================================
  IF v_dob_cargo IS NOT NULL AND v_dob_cargo ~* '(suprim|compras|comprador|procurement|sourcing|buyer|engenh|engineer|industri|capex|projetos|projects|operations|operacoe|manuten|maintenance|diretor|director|presidente|chief|gerente|coordenad|head|supply\s*chain)' THEN
    v_score := v_score + 15;
    v_componentes := v_componentes || jsonb_build_object('cargo_compativel_setor', 15);
  ELSE
    v_componentes := v_componentes || jsonb_build_object('cargo_compativel_setor', 0);
  END IF;

  -- =========================================================================
  -- Critério E: citado_em_descricao (+10 / 0)
  -- =========================================================================
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

  -- Floor at 0 (não armazenar score negativo, mas componente fica negativo no breakdown)
  IF v_score < 0 THEN
    v_score := 0;
  END IF;

  -- Persistir
  UPDATE decisores_obra
  SET
    confianca_match = v_score,
    confianca_match_componentes = v_componentes,
    confianca_match_calculada_em = NOW()
  WHERE id = p_dob_id;

  v_resultado := jsonb_build_object(
    'dob_id', p_dob_id,
    'score', v_score,
    'componentes', v_componentes,
    'radical_empresa', v_radical_empresa,
    'cargo_sufixo_extraido', v_cargo_sufixo
  );

  RETURN v_resultado;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION calcular_confianca_match(UUID) IS
  'Calcula score 0-100 de confiança do match decisor↔obra. Adaptado p/ decisores_obra autônoma. Componente A pode ser negativo (-30) se cargo cita OUTRA empresa, mas score persistido tem floor=0. Atualiza decisores_obra in-place.';
