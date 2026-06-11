# 2026-06-11 — Validação lote OFICIAL + dedup Cetras-PA

## Autorização: "COMMIT" do William (dedup + 30 validações; 2 borderline excluídos).
## Aplicado
- DEDUP: 690b40f5 (Cetras-PA, sem url_fonte) → visivel=false, motivo_invisivel=duplicata_pncp_serpro_20260611. Mantida bd849445 (mais antiga + url completa).
- VALIDAÇÃO: 30 obras OFICIAL setor≠OUTRO valor>=10mi classif NULL → validacao_obra_at=NOW() + recompute → 6 BRONZE + 24 PIPELINE. marker auto:validacao_lote_oficial_20260611.
- Excluídos do lote: 83cb13a9 (serviço transporte), 07f597d9 ("Obras comuns" genérico).
## Rollback
- validação: UPDATE obras SET validacao_obra_at=NULL, classificacao_computed=NULL WHERE observacoes_validacao LIKE '%auto:validacao_lote_oficial_20260611%';
- dedup: UPDATE obras SET visivel=true, motivo_invisivel=NULL WHERE id::text LIKE '690b40f5%';
