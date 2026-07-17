\timing on
BEGIN;

-- ANTES
SELECT 'A_scc_telecom_dc_sider_antes' AS m, COUNT(*) FROM setor_cnae_compatibility WHERE setor_obra IN ('TELECOM','DATA_CENTER') OR (setor_obra='SIDERURGIA_METALURGIA' AND cnae_codigo IN ('2422902','2439300'));
SELECT 'B_obras_oleo_antes' AS m, COUNT(*) FROM obras WHERE setor='OLEO_E_GAS';
SELECT 'C_obras_outro_alvo_antes' AS m, COUNT(*) FROM obras WHERE setor='OUTRO';

-- ============================================================
-- Seções A+B+C — matchmaking #2 (11/06/2026)
-- A: mapeia TELECOM/DATA_CENTER + 2 CNAEs siderurgia faltantes
-- B: normaliza OLEO_E_GAS -> PETROLEO_GAS (16 obras)
-- C: reclassifica 43 obras OUTRO -> setor industrial (Haiku, filtrado)
-- ============================================================

-- A) setor_cnae_compatibility (fases amplo; ON CONFLICT idempotente)
INSERT INTO setor_cnae_compatibility (setor_obra, cnae_codigo, peso, fases_aplicaveis, fonte)
SELECT v.setor, v.cnae, v.peso,
       ARRAY['PLANEJAMENTO','PROJETO','LICENCA_PREVIA','LICITACAO_ABERTA','LICENCA_INSTALACAO','EM_EXECUCAO','OPERACAO'],
       'seed_manual_20260611'
FROM (VALUES
        ('TELECOM','4221904',1.00),('TELECOM','4221905',0.85),('TELECOM','2631100',0.70),
('TELECOM','6190699',0.60),('TELECOM','6190601',0.50),('TELECOM','4652400',0.50),
('DATA_CENTER','6311900',1.00),('DATA_CENTER','4322302',0.80),('DATA_CENTER','4321500',0.70),
('DATA_CENTER','2824101',0.60),('DATA_CENTER','3530100',0.50),
('SIDERURGIA_METALURGIA','2422902',0.90),('SIDERURGIA_METALURGIA','2439300',0.80)
     ) AS v(setor, cnae, peso)
ON CONFLICT (setor_obra, cnae_codigo) DO NOTHING;

-- B) normalização OLEO_E_GAS -> PETROLEO_GAS
UPDATE obras SET setor='PETROLEO_GAS'
 WHERE setor='OLEO_E_GAS';

-- C) reclassificação 43 OUTRO -> industrial (marker p/ rollback)
UPDATE obras o SET
   setor = r.novo_setor,
   validacao_metodo = 'auto:haiku_setor_outro_v1',
   observacoes_validacao = COALESCE(observacoes_validacao,'') || ' | auto:haiku_setor_outro_v1'
FROM (VALUES
        ('2064cba7-389b-4723-b2c7-2a4271becc6e','INFRAESTRUTURA'),
        ('54d8fe45-4c75-4452-8bab-9acf4fb78152','INFRAESTRUTURA'),
        ('add185a7-281e-409a-a9a3-743279aab145','INFRAESTRUTURA'),
        ('ec53803f-d2f5-4c7a-b14a-c986a829dcc8','INFRAESTRUTURA'),
        ('65b1c976-0391-4124-8487-bda92c9693a2','INFRAESTRUTURA'),
        ('5c496e48-0c5b-4d6a-9f40-65a3f2fe1063','INFRAESTRUTURA'),
        ('d0470050-b844-475d-9562-047765cad389','INFRAESTRUTURA'),
        ('012b8e20-eaf1-477f-a845-334c39bf0ced','INFRAESTRUTURA'),
        ('0933cfe4-1686-4697-bdec-19df15c32380','INFRAESTRUTURA'),
        ('a8af2b84-c34d-4100-9c18-8a94088b74c7','INFRAESTRUTURA'),
        ('68e70a15-3ee9-4043-aca6-2fdf096eb170','INFRAESTRUTURA'),
        ('a8d0431f-38a9-41ff-ba87-bbe1cb5d7eb0','INFRAESTRUTURA'),
        ('c04677d6-8784-4197-b8e9-2699c57eccb1','INFRAESTRUTURA'),
        ('d16683c5-ccae-4727-b0f4-3374e6d95931','INFRAESTRUTURA'),
        ('e62810db-d854-405c-8b02-70700eeda51e','INFRAESTRUTURA'),
        ('193ec76c-c36f-48b0-afe0-2a3bb78d2da0','INFRAESTRUTURA'),
        ('577834db-2bad-4501-b1fd-7eac7453bf87','INFRAESTRUTURA'),
        ('1c03f64c-93f5-4c35-a879-67745b17b527','INFRAESTRUTURA'),
        ('639e34a2-cb59-4973-bbce-d98787e0c996','INFRAESTRUTURA'),
        ('db69c02b-3678-4478-a85f-398f74ce91ca','INFRAESTRUTURA'),
        ('68e87683-61ac-40fa-90d8-852859798e95','INFRAESTRUTURA'),
        ('5ffffcd2-bcdb-48ae-81d4-b6c6b4086069','INFRAESTRUTURA'),
        ('c90c39b0-3d1c-4630-a344-bd2b3e5a2455','INFRAESTRUTURA'),
        ('9d7a7120-8381-4c70-938a-ec8b588004c7','INFRAESTRUTURA'),
        ('f0f3946c-f2a8-4ebe-8e27-38942df63026','INFRAESTRUTURA'),
        ('b2d958fe-9609-4ee2-bc4f-584752ca8926','INFRAESTRUTURA'),
        ('823dc149-4134-49d8-ad4b-5bd2c3989d0c','INFRAESTRUTURA'),
        ('71194a7b-5601-4786-8923-c6b7b3e4a674','INFRAESTRUTURA'),
        ('1e3a03fa-9d42-4ecb-83ec-e470ffdede7b','INFRAESTRUTURA'),
        ('3c46a36e-632c-4d6e-ac86-7b37d8d9d5db','INFRAESTRUTURA'),
        ('443048ee-793a-4de9-939a-94d16906a5c0','INFRAESTRUTURA'),
        ('5ababae6-ece7-496f-bf42-f351de18471f','INFRAESTRUTURA'),
        ('0ee102d6-edaa-4739-a400-e4fe93d36405','LOGISTICO'),
        ('90dca555-c446-484c-8238-8a56cc72b1df','INFRAESTRUTURA'),
        ('e7acb44f-0121-41eb-ba88-62b828d4aa56','INFRAESTRUTURA'),
        ('64c701d1-fd8a-40c2-a0ca-11d2da67cc31','INFRAESTRUTURA'),
        ('132939ed-8ddf-44c0-af7d-861d20510509','INFRAESTRUTURA'),
        ('3a61536d-1958-4ce6-b9c5-f461f6a339e3','SANEAMENTO'),
        ('7b0173a5-f6ba-4853-ae0b-ea9e0e7e268e','INFRAESTRUTURA'),
        ('392ada71-54f8-4429-add9-a138f326068b','INFRAESTRUTURA'),
        ('540d048c-f98d-4f14-9986-b0167c75d826','INFRAESTRUTURA'),
        ('3e131f0e-8857-47bc-a6c4-0a74b457f2db','INFRAESTRUTURA'),
        ('cbca2df4-7726-4c7d-8daa-c107961ee85a','INFRAESTRUTURA')
     ) AS r(id, novo_setor)
WHERE o.id = r.id::uuid AND o.setor='OUTRO';


-- DEPOIS
SELECT 'A_scc_inseridas' AS m, COUNT(*) FROM setor_cnae_compatibility WHERE fonte='seed_manual_20260611';
SELECT 'B_obras_oleo_depois(esperado 0)' AS m, COUNT(*) FROM obras WHERE setor='OLEO_E_GAS';
SELECT 'B_petroleo_ganhou' AS m, COUNT(*) FROM obras WHERE setor='PETROLEO_GAS' AND validacao_metodo IS DISTINCT FROM 'auto:haiku_setor_outro_v1';
SELECT 'C_reclassificadas' AS m, COUNT(*) FROM obras WHERE validacao_metodo='auto:haiku_setor_outro_v1';
SELECT 'C_distribuicao' AS m, setor, COUNT(*) FROM obras WHERE validacao_metodo='auto:haiku_setor_outro_v1' GROUP BY setor;

-- PROVA: quantas das obras afetadas agora teriam >=1 fornecedor candidato
SELECT 'PROVA_telecom_dc_com_fornec' AS m, COUNT(DISTINCT o.id)
FROM obras o
JOIN setor_cnae_compatibility scc ON scc.setor_obra=o.setor
JOIN uf_proximidade up ON up.uf_obra=o.uf
JOIN fornecedores f ON (f.cnae_principal=scc.cnae_codigo OR scc.cnae_codigo=ANY(f.cnae_secundarios)) AND up.uf_fornec=f.uf AND f.porte_inferido<>'MICRO'
WHERE o.setor IN ('TELECOM','DATA_CENTER','PETROLEO_GAS') AND o.visivel=true
  AND (o.validacao_metodo='auto:haiku_setor_outro_v1' OR o.setor IN ('TELECOM','DATA_CENTER','PETROLEO_GAS'));

ROLLBACK;
