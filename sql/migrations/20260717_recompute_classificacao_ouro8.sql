CREATE OR REPLACE FUNCTION public.recompute_classificacao_obra(p_obra_id uuid)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_max_score INT;
  v_has_email_max_score BOOLEAN;
  v_valor NUMERIC;
  v_validada BOOLEAN;
  v_fonte_tipo TEXT;
  v_classificacao_atual TEXT;
  v_smtp_verified BOOLEAN;
  v_has_real_decisor BOOLEAN;
  v_nova TEXT;
BEGIN
  SELECT
    o.valor_estimado, (o.validacao_obra_at IS NOT NULL),
    COALESCE(o.fonte_tipo, 'OFICIAL'), o.classificacao_computed,
    COALESCE(o.nivel1_email_smtp_verified, false)
  INTO v_valor, v_validada, v_fonte_tipo, v_classificacao_atual, v_smtp_verified
  FROM obras o WHERE o.id = p_obra_id;

  IF v_fonte_tipo = 'NOTICIA' THEN RETURN; END IF;

  -- v2.1: ignora decisores REPLICADO no MAX
  SELECT COALESCE(MAX(confianca_match), 0)
  INTO v_max_score
  FROM decisores_obra dob
  WHERE dob.obra_id = p_obra_id
    AND dob.excluido_em IS NULL
    AND COALESCE(dob.hipotese_replicacao,'') <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO';

  SELECT EXISTS (
    SELECT 1 FROM decisores_obra dob
    LEFT JOIN obras o2 ON o2.id = dob.obra_id
    WHERE dob.obra_id = p_obra_id AND dob.excluido_em IS NULL
      AND COALESCE(dob.hipotese_replicacao,'') <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO'
      AND dob.confianca_match >= 70
      AND (NULLIF(dob.email,'') IS NOT NULL OR NULLIF(dob.linkedin_url,'') IS NOT NULL OR NULLIF(o2.nivel1_email,'') IS NOT NULL)
  ) INTO v_has_email_max_score;

  -- v2.2 (PRATA condicional): existe ao menos 1 decisor real nao-replicado?
  SELECT EXISTS (
    SELECT 1 FROM decisores_obra dob
    WHERE dob.obra_id = p_obra_id
      AND dob.excluido_em IS NULL
      AND COALESCE(dob.hipotese_replicacao,'') <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO'
  ) INTO v_has_real_decisor;

  v_nova := CASE
    WHEN v_max_score >= 70 AND v_has_email_max_score THEN 'OURO'
    WHEN v_max_score >= 50 THEN 'PRATA'
    -- PRATA condicional: confiança baixa mas email SMTP verificado + decisor real não-replicado.
    -- Threshold 30 (não 0) evita promover lixo de baixíssima confiança.
    WHEN v_max_score >= 30 AND v_smtp_verified AND v_has_real_decisor THEN 'PRATA'
    WHEN v_validada AND v_valor >= 50e6 AND v_fonte_tipo IN ('OFICIAL','MANUAL') THEN 'BRONZE'
    WHEN v_validada AND v_valor >= 10e6 AND v_fonte_tipo IN ('OFICIAL','MANUAL') THEN 'PIPELINE'
    WHEN v_classificacao_atual IN ('OURO','PRATA','BRONZE','PIPELINE') THEN 'PIPELINE'
    ELSE v_classificacao_atual
  END;

  UPDATE obras SET classificacao_computed = v_nova WHERE id = p_obra_id;
END;
$function$

