BEGIN;
UPDATE public.obras o SET
  classificacao_computed = r.classificacao_computed,
  empresa = r.empresa,
  cnpj = r.cnpj,
  nivel1_nome = r.nivel1_nome,
  nivel1_cargo = r.nivel1_cargo,
  nivel1_email = r.nivel1_email,
  nivel1_telefone = r.nivel1_telefone,
  nivel1_linkedin = r.nivel1_linkedin,
  nivel1_email_status = r.nivel1_email_status,
  nivel1_telefone_status = r.nivel1_telefone_status,
  nivel1_origem_enrichment = r.nivel1_origem_enrichment,
  status_enriquecimento = r.status_enriquecimento
FROM wins_v2.bronze_enrich_rollback r
WHERE o.id = r.obra_id;
COMMIT;
