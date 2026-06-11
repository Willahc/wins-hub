# 2026-06-11 — UF composta/lixo (matchmaking #2 follow-up)

## Autorização: "COMMIT A+B" explícito do William, 11/06/2026. Aplicado: UPDATE 26 (A) + UPDATE 8 (B). B5 OK: 0 uf invalida.

## Aplica (COMMIT.sql)
- A) 26 obras UF composta (BA/MG, CE/PB/PE/AL/BA/PI...) → primeira UF válida.
     Original preservado em observacoes_validacao (' | uf_orig:<comp> auto:uf_split_v1').
- B) 8 obras UF lixo (6 vazias + 'MÚ' + 'NE'=região) → uf=NULL (saem do alvo do
     matchmaker, param de reprocessar). Original em observacoes_validacao (auto:uf_null_v1).
- Pós: 0 obras com UF inválida. Trigger log_obras_changes audita uf.

## Rollback
- A: UPDATE obras SET uf=split_part(substring(observacoes_validacao from 'uf_orig:([^ ]+) auto:uf_split_v1'),...) -- ou via observacoes; original está gravado.
- B: idem via 'uf_orig:[<val>] auto:uf_null_v1'.
- Simplificado: backup pré-mudança recomendado se quiser revert trivial.
