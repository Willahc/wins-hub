# 2026-06-11 — Matchmaking #2: mapas setor + reclassificação OUTRO

## Autorização
"COMMIT" explícito do William, 11/06/2026.

## Aplicado (COMMIT.sql)
- 13 linhas em setor_cnae_compatibility: TELECOM(6), DATA_CENTER(5), SIDERURGIA(2422902,2439300). CNAEs validados vs cnae_oficial. fonte='seed_manual_20260611'.
- OLEO_E_GAS→PETROLEO_GAS: 16 obras.
- 43 obras OUTRO→industrial (41 INFRAESTRUTURA+1 LOGISTICO+1 SANEAMENTO). marker validacao_metodo='auto:haiku_setor_outro_v1'.
- Deploy código main.py: SETORES_VALIDOS += {SIDERURGIA_METALURGIA,TELECOM,DATA_CENTER}; aliases siderurgia/metalurgia→SIDERURGIA_METALURGIA, oleo→PETROLEO_GAS, telecom→TELECOM, data center→DATA_CENTER.

## Rollback
- SCC: DELETE FROM setor_cnae_compatibility WHERE fonte='seed_manual_20260611';
- reclass: UPDATE obras SET setor='OUTRO', validacao_metodo=NULL WHERE validacao_metodo='auto:haiku_setor_outro_v1';
- OLEO: sem reverse trivial (16 obras eram OLEO_E_GAS; log_obras_changes registrou). 
- código: main.py.bak_pre_setores_validos_siderurgia_*

## Fora de escopo
438 obras OUTRO de serviço (corretamente sem match); 32 obras UF composta (BA/MG etc.).
