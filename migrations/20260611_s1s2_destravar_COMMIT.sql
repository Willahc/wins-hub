\timing on
BEGIN;
-- ===== S1: 18 NOTICIA industrial >=10mi -> MANUAL + valida + recompute =====
UPDATE obras o SET fonte_tipo='MANUAL', validacao_obra_at=NOW(),
   observacoes_validacao = COALESCE(o.observacoes_validacao,'') || ' | auto:s1_noticia_manual_20260611'
 WHERE o.visivel=true AND o.uf IS NOT NULL AND o.setor IS NOT NULL AND o.setor<>'OUTRO'
   AND COALESCE(o.fonte_tipo,'OFICIAL')='NOTICIA' AND o.valor_estimado>=10e6
   AND NOT EXISTS (SELECT 1 FROM matches_v2 m WHERE m.obra_id=o.id);

-- ===== S2: 16 OUTRO industrial (Haiku) -> setor correto + valida (se faltar) =====
UPDATE obras o SET setor=r.novo, validacao_obra_at=COALESCE(o.validacao_obra_at,NOW()),
   observacoes_validacao = COALESCE(o.observacoes_validacao,'') || ' | auto:s2_reclass_setor_20260611 (de OUTRO)'
FROM (VALUES
        ('a26c1b90-4a4a-4899-8e6c-6292914e8a49','INFRAESTRUTURA'),
        ('414aa9dc-4991-4f71-884d-c946cd5adfc1','ENERGIA'),
        ('9b92c4db-e402-4402-beee-202e7a74d6c2','INFRAESTRUTURA'),
        ('8bf60077-25c3-4e7b-a278-d1a28c5b799b','INFRAESTRUTURA'),
        ('dd7a593b-7c76-49a8-8906-ad29423485a1','INFRAESTRUTURA'),
        ('4342547c-e163-4285-90f6-8a633936d796','INFRAESTRUTURA'),
        ('b9290655-b06f-4e65-8aea-2fb9638a0c76','INFRAESTRUTURA'),
        ('63dc9132-518b-4d07-a993-b4c1307e6fd1','INFRAESTRUTURA'),
        ('edd382d0-6fc3-4500-b5d7-19fae1803fd9','INFRAESTRUTURA'),
        ('98655ac4-12ec-424e-b9f5-13a52d72f00e','INFRAESTRUTURA'),
        ('cf0f3f20-5d79-4c38-b130-e4f56eb7010a','INFRAESTRUTURA'),
        ('08578898-fe7c-440c-8311-c5e30b029371','INFRAESTRUTURA'),
        ('2291636e-3a38-44dd-9d10-b2a1f956e48c','INFRAESTRUTURA'),
        ('16837df0-4020-4e71-8648-02da342d89ab','INFRAESTRUTURA'),
        ('4aab99a9-1bbf-4336-99f9-ddae2b5abf85','INFRAESTRUTURA'),
        ('ea440f54-631b-40c6-83c8-305155b0eec3','INFRAESTRUTURA')
     ) AS r(id, novo)
WHERE o.id = r.id::uuid AND o.setor='OUTRO';

-- ===== recompute individual em todas as obras tocadas por S1+S2 =====
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT id FROM obras
    WHERE observacoes_validacao LIKE '%auto:s1_noticia_manual_20260611%'
       OR observacoes_validacao LIKE '%auto:s2_reclass_setor_20260611%'
  LOOP PERFORM recompute_classificacao_obra(r.id); END LOOP;
END $$;
COMMIT;
