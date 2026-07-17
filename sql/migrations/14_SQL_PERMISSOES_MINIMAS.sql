-- Executar como owner/superuser depois dos DDLs 04 e 05.
-- Escopo intencional: somente a role wins_app e o schema wins_v2.
-- EXECUTE implicito de PUBLIC em funcoes deste schema tambem e removido.
BEGIN;

REVOKE ALL PRIVILEGES ON SCHEMA wins_v2 FROM wins_app;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA wins_v2 FROM wins_app;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA wins_v2 FROM wins_app;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA wins_v2 FROM wins_app;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA wins_v2 FROM PUBLIC;

-- Impede que objetos futuros do mesmo owner readquiram privilegios amplos.
ALTER DEFAULT PRIVILEGES IN SCHEMA wins_v2
    REVOKE ALL PRIVILEGES ON TABLES FROM wins_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA wins_v2
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM wins_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA wins_v2
    REVOKE ALL PRIVILEGES ON FUNCTIONS FROM wins_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA wins_v2
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA wins_v2
    REVOKE ALL PRIVILEGES ON TYPES FROM wins_app;

GRANT USAGE ON SCHEMA wins_v2 TO wins_app;
GRANT SELECT ON TABLE wins_v2.entidades_lookup TO wins_app;
GRANT INSERT ON TABLE wins_v2.enrichment_lookup_log TO wins_app;
GRANT USAGE ON SEQUENCE wins_v2.enrichment_lookup_log_id_seq TO wins_app;

DO $audit$
DECLARE
    wins_app_oid OID := 'wins_app'::regrole::OID;
    owned_object TEXT;
BEGIN
    IF (
        SELECT nspowner = wins_app_oid
        FROM pg_namespace
        WHERE nspname = 'wins_v2'
    ) THEN
        RAISE EXCEPTION
            'wins_app ainda e owner do schema wins_v2; transfira ownership antes do COMMIT';
    END IF;

    SELECT format('%I.%I', namespace.nspname, class.relname)
      INTO owned_object
      FROM pg_class AS class
      JOIN pg_namespace AS namespace ON namespace.oid = class.relnamespace
     WHERE namespace.nspname = 'wins_v2'
       AND class.relowner = wins_app_oid
     ORDER BY class.relname
     LIMIT 1;
    IF owned_object IS NOT NULL THEN
        RAISE EXCEPTION
            'wins_app ainda e owner de %; transfira ownership antes do COMMIT',
            owned_object;
    END IF;

    IF has_schema_privilege('wins_app', 'wins_v2', 'CREATE')
       OR NOT has_schema_privilege('wins_app', 'wins_v2', 'USAGE') THEN
        RAISE EXCEPTION 'privilegios do schema wins_v2 nao sao minimos';
    END IF;

    IF NOT has_table_privilege(
        'wins_app', 'wins_v2.entidades_lookup', 'SELECT'
    ) OR has_table_privilege(
        'wins_app',
        'wins_v2.entidades_lookup',
        'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
    ) THEN
        RAISE EXCEPTION 'privilegios de entidades_lookup nao sao minimos';
    END IF;

    IF NOT has_table_privilege(
        'wins_app', 'wins_v2.enrichment_lookup_log', 'INSERT'
    ) OR has_table_privilege(
        'wins_app',
        'wins_v2.enrichment_lookup_log',
        'SELECT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
    ) THEN
        RAISE EXCEPTION 'privilegios de enrichment_lookup_log nao sao minimos';
    END IF;

    IF NOT has_sequence_privilege(
        'wins_app', 'wins_v2.enrichment_lookup_log_id_seq', 'USAGE'
    ) OR has_sequence_privilege(
        'wins_app', 'wins_v2.enrichment_lookup_log_id_seq', 'SELECT,UPDATE'
    ) THEN
        RAISE EXCEPTION 'privilegios da sequence do log nao sao minimos';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_proc AS procedure
          JOIN pg_namespace AS namespace
            ON namespace.oid = procedure.pronamespace
         WHERE namespace.nspname = 'wins_v2'
           AND has_function_privilege(
               'wins_app', procedure.oid, 'EXECUTE'
           )
    ) THEN
        RAISE EXCEPTION 'wins_app ainda possui EXECUTE efetivo em wins_v2';
    END IF;
END;
$audit$;

COMMIT;
