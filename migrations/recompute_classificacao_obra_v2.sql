-- Sprint 2: recompute_classificacao_obra v2 (snapshot do banco após fix do ELSE)
 CREATE OR REPLACE FUNCTION public.recompute_classificacao_obra(p_obra_id uuid)                                                   +
  RETURNS void                                                                                                                    +
  LANGUAGE plpgsql                                                                                                                +
 AS $function$                                                                                                                    +
 DECLARE                                                                                                                          +
   v_max_score INT;                                                                                                               +
   v_has_email_max_score BOOLEAN;                                                                                                 +
   v_valor NUMERIC;                                                                                                               +
   v_validada BOOLEAN;                                                                                                            +
   v_fonte_tipo TEXT;                                                                                                             +
   v_classificacao_atual TEXT;                                                                                                    +
   v_nova TEXT;                                                                                                                   +
 BEGIN                                                                                                                            +
   SELECT                                                                                                                         +
     o.valor_estimado, (o.validacao_obra_at IS NOT NULL),                                                                         +
     COALESCE(o.fonte_tipo, 'OFICIAL'), o.classificacao_computed                                                                  +
   INTO v_valor, v_validada, v_fonte_tipo, v_classificacao_atual                                                                  +
   FROM obras o WHERE o.id = p_obra_id;                                                                                           +
                                                                                                                                  +
   IF v_fonte_tipo = 'NOTICIA' THEN                                                                                               +
     RETURN;                                                                                                                      +
   END IF;                                                                                                                        +
                                                                                                                                  +
   SELECT COALESCE(MAX(confianca_match), 0)                                                                                       +
   INTO v_max_score                                                                                                               +
   FROM decisores_obra dob                                                                                                        +
   WHERE dob.obra_id = p_obra_id AND dob.excluido_em IS NULL;                                                                     +
                                                                                                                                  +
   SELECT EXISTS (                                                                                                                +
     SELECT 1 FROM decisores_obra dob                                                                                             +
     LEFT JOIN obras o2 ON o2.id = dob.obra_id                                                                                    +
     WHERE dob.obra_id = p_obra_id AND dob.excluido_em IS NULL                                                                    +
       AND dob.confianca_match >= 70                                                                                              +
       AND (NULLIF(dob.email,'') IS NOT NULL OR NULLIF(dob.linkedin_url,'') IS NOT NULL OR NULLIF(o2.nivel1_email,'') IS NOT NULL)+
   ) INTO v_has_email_max_score;                                                                                                  +
                                                                                                                                  +
   v_nova := CASE                                                                                                                 +
     WHEN v_max_score >= 70 AND v_has_email_max_score THEN 'OURO'                                                                 +
     WHEN v_max_score >= 50 THEN 'PRATA'                                                                                          +
     WHEN v_validada AND v_valor >= 50e6 AND v_fonte_tipo IN ('OFICIAL','MANUAL') THEN 'BRONZE'                                   +
     WHEN v_validada AND v_valor >= 10e6 AND v_fonte_tipo IN ('OFICIAL','MANUAL') THEN 'PIPELINE'                                 +
     -- Downgrade OURO/PRATA/BRONZE/PIPELINE antigos que não qualificam → PIPELINE (mantém visíveis)                              +
     WHEN v_classificacao_atual IN ('OURO','PRATA','BRONZE','PIPELINE') THEN 'PIPELINE'                                           +
     -- Preserva REJEITADO, LICITACAO_ABERTA, NULL                                                                                +
     ELSE v_classificacao_atual                                                                                                   +
   END;                                                                                                                           +
                                                                                                                                  +
   UPDATE obras SET classificacao_computed = v_nova WHERE id = p_obra_id;                                                         +
 END;                                                                                                                             +
 $function$                                                                                                                       +
 

