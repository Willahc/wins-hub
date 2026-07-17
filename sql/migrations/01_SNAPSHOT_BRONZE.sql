BEGIN;
CREATE TABLE IF NOT EXISTS wins_v2.bronze_enrich_snapshot (
  obra_id uuid PRIMARY KEY,
  classificacao_computed text,
  empresa text,
  cnpj text,
  empresa_executora text,
  cnpj_executora text,
  nivel1_nome text,
  nivel1_cargo text,
  nivel1_email text,
  nivel1_telefone text,
  nivel1_linkedin text,
  nivel1_email_status text,
  nivel1_telefone_status text,
  status_enriquecimento text,
  visivel boolean,
  capturado_em timestamptz DEFAULT now()
);
TRUNCATE wins_v2.bronze_enrich_snapshot;
INSERT INTO wins_v2.bronze_enrich_snapshot (
  obra_id, classificacao_computed, empresa, cnpj, empresa_executora, cnpj_executora,
  nivel1_nome, nivel1_cargo, nivel1_email, nivel1_telefone, nivel1_linkedin,
  nivel1_email_status, nivel1_telefone_status, status_enriquecimento, visivel
)
SELECT id, classificacao_computed, empresa, cnpj, empresa_executora, cnpj_executora,
  nivel1_nome, nivel1_cargo, nivel1_email, nivel1_telefone, nivel1_linkedin,
  nivel1_email_status, nivel1_telefone_status, status_enriquecimento, visivel
FROM public.obras
WHERE status_portao='APROVADA' AND classificacao_computed='BRONZE';

CREATE TABLE IF NOT EXISTS wins_v2.bronze_enrich_audit (
  id bigserial PRIMARY KEY,
  obra_id uuid NOT NULL,
  classificacao_anterior text,
  classificacao_nova text,
  campos_enriquecidos jsonb,
  valores_anteriores jsonb,
  valores_novos jsonb,
  fontes jsonb,
  confianca numeric,
  regra_aplicada text,
  motivo_promocao text,
  agente text DEFAULT 'bronze_enrich_v1',
  lote text,
  criado_em timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bronze_enrich_audit_obra ON wins_v2.bronze_enrich_audit(obra_id);

CREATE TABLE IF NOT EXISTS wins_v2.bronze_enrich_rollback (
  obra_id uuid PRIMARY KEY,
  classificacao_computed text,
  empresa text,
  cnpj text,
  nivel1_nome text,
  nivel1_cargo text,
  nivel1_email text,
  nivel1_telefone text,
  nivel1_linkedin text,
  nivel1_email_status text,
  nivel1_telefone_status text,
  nivel1_origem_enrichment text,
  status_enriquecimento text,
  snapshot_em timestamptz DEFAULT now()
);
TRUNCATE wins_v2.bronze_enrich_rollback;
INSERT INTO wins_v2.bronze_enrich_rollback (
  obra_id, classificacao_computed, empresa, cnpj, nivel1_nome, nivel1_cargo,
  nivel1_email, nivel1_telefone, nivel1_linkedin, nivel1_email_status,
  nivel1_telefone_status, nivel1_origem_enrichment, status_enriquecimento
)
SELECT id, classificacao_computed, empresa, cnpj, nivel1_nome, nivel1_cargo,
  nivel1_email, nivel1_telefone, nivel1_linkedin, nivel1_email_status,
  nivel1_telefone_status, nivel1_origem_enrichment, status_enriquecimento
FROM public.obras
WHERE status_portao='APROVADA' AND classificacao_computed='BRONZE';

GRANT SELECT, INSERT, UPDATE, DELETE ON wins_v2.bronze_enrich_snapshot TO wins_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON wins_v2.bronze_enrich_audit TO wins_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON wins_v2.bronze_enrich_rollback TO wins_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA wins_v2 TO wins_app;
COMMIT;
