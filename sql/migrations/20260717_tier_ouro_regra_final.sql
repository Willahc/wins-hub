-- Snapshot e auditoria da regra definitiva OURO (e-mail nominal validado obrigatório)
BEGIN;

CREATE TABLE IF NOT EXISTS wins_v2.tier_ouro_regra_final_snapshot (
    obra_id uuid PRIMARY KEY,
    classificacao_anterior text,
    status_portao text,
    valor_estimado numeric,
    cnpj text,
    snapshot_em timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS wins_v2.tier_ouro_regra_final_audit (
    id bigserial PRIMARY KEY,
    obra_id uuid NOT NULL,
    tier_anterior text,
    tier_novo text,
    criterios_atendidos jsonb,
    criterios_ausentes jsonb,
    cnpj text,
    dominio text,
    capex numeric,
    decisor text,
    cargo text,
    linkedin text,
    email text,
    email_status text,
    telefone text,
    tipo_telefone text,
    motivo text,
    regra_aplicada text NOT NULL DEFAULT 'OURO_8_CRITERIOS_V1',
    evidencias jsonb,
    criado_em timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tier_ouro_regra_final_audit_obra
  ON wins_v2.tier_ouro_regra_final_audit (obra_id);

CREATE INDEX IF NOT EXISTS idx_tier_ouro_regra_final_audit_ts
  ON wins_v2.tier_ouro_regra_final_audit (criado_em DESC);

COMMIT;
