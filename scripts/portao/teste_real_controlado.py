#!/usr/bin/env python3
import os, sys, json, uuid
sys.path.insert(0, "/app/services")
sys.path.insert(0, "/app/scripts/portao")
import psycopg2
from portao_gate_v5 import processar_fila

conn = psycopg2.connect(
    host=os.getenv("DB_HOST", "db"),
    dbname=os.getenv("DB_NAME", "wins_hub"),
    user=os.getenv("DB_USER", "wins_app"),
    password=os.getenv("DB_PASSWORD", ""),
)
cur = conn.cursor()
suffix = uuid.uuid4().hex[:8]
tests = [
    {
        "id_externo": f"PORTAO-TEST-OK-{suffix}",
        "nome": "Construção de unidade industrial e galpão em Goiás",
        "descricao": "Implantação de fábrica com infraestrutura e montagem industrial",
        "setor": "INDUSTRIAL",
        "fonte": "teste_portao_v5",
        "valor_estimado": 3500000,
        "url_fonte": f"https://example.test/ok/{suffix}",
        "empresa": "EMPRESA TESTE PORTAO LTDA",
        "cnpj": "11222333000181",
        "expect": "APROVADA",
    },
    {
        "id_externo": f"PORTAO-TEST-SOFT-{suffix}",
        "nome": "Aquisição de licença de software ERP",
        "descricao": "Compra de software SaaS sem instalação física",
        "setor": "INDUSTRIAL",
        "fonte": "teste_portao_v5",
        "valor_estimado": 900000,
        "url_fonte": f"https://example.test/soft/{suffix}",
        "empresa": "EMPRESA TESTE PORTAO LTDA",
        "cnpj": None,
        "expect": "REJEITADA",
    },
    {
        "id_externo": f"PORTAO-TEST-ENG-{suffix}",
        "nome": "Serviços de engenharia diversos",
        "descricao": "Prestação de serviços de engenharia sem escopo físico",
        "setor": "INFRAESTRUTURA",
        "fonte": "teste_portao_v5",
        "valor_estimado": 400000,
        "url_fonte": f"https://example.test/eng/{suffix}",
        "empresa": "EMPRESA TESTE PORTAO LTDA",
        "cnpj": None,
        "expect": "EM_ANALISE",
    },
    {
        "id_externo": f"PORTAO-TEST-LOW-{suffix}",
        "nome": "Construção de depósito auxiliar",
        "descricao": "Construção de pequeno galpão",
        "setor": "INDUSTRIAL",
        "fonte": "teste_portao_v5",
        "valor_estimado": 80000,
        "url_fonte": f"https://example.test/low/{suffix}",
        "empresa": "EMPRESA TESTE PORTAO LTDA",
        "cnpj": None,
        "expect": "REJEITADA",
    },
]
ids = []
for t in tests:
    cur.execute(
        """
        INSERT INTO obras (
            id_externo, nome, descricao, setor, fonte, valor_estimado, url_fonte,
            empresa, cnpj, uf, municipio, fase, fonte_tipo
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'GO','Goiânia','PLANEJAMENTO','OFICIAL')
        RETURNING id::text, status_portao, visivel, motivo_invisivel
        """,
        (
            t["id_externo"], t["nome"], t["descricao"], t["setor"], t["fonte"],
            t["valor_estimado"], t["url_fonte"], t["empresa"], t.get("cnpj"),
        ),
    )
    row = cur.fetchone()
    ids.append((row[0], t, row[1], row[2], row[3]))
conn.commit()
print("AFTER_INSERT")
for oid, t, sp, vis, mot in ids:
    print(f"{t['id_externo']}|{sp}|{vis}|{mot}")

stats = processar_fila(limit=20)
print("FILA", json.dumps(stats))

results = []
print("AFTER_PROCESS")
for oid, t, *_ in ids:
    cur.execute(
        "SELECT status_portao, visivel, portao_motivo, status_enriquecimento FROM obras WHERE id=%s",
        (oid,),
    )
    sp, vis, pm, se = cur.fetchone()
    ok = sp == t["expect"]
    print(f"{t['id_externo']}|got={sp}|exp={t['expect']}|ok={ok}|vis={vis}|enr={se}|{pm}")
    results.append({"id_externo": t["id_externo"], "ok": ok, "status": sp, "visivel": vis})

cur.execute(
    """
    SELECT COUNT(*) FROM obras
     WHERE id = ANY(%s::uuid[])
       AND (visivel IS NULL OR visivel=true)
       AND empresa IS NOT NULL AND empresa <> ''
       AND (status_portao IS NULL OR status_portao='APROVADA')
    """,
    ([r[0] for r in ids],),
)
print("visiveis_filtro_site", cur.fetchone()[0])

# audit rows
cur.execute("SELECT COUNT(*) FROM wins_v2.portao_decisoes WHERE obra_id = ANY(%s::uuid[])", ([r[0] for r in ids],))
print("auditoria_rows", cur.fetchone()[0])

# cleanup
cur.execute("DELETE FROM wins_v2.portao_decisoes WHERE obra_id = ANY(%s::uuid[])", ([r[0] for r in ids],))
cur.execute("DELETE FROM wins_v2.portao_fila WHERE obra_id = ANY(%s::uuid[])", ([r[0] for r in ids],))
try:
    cur.execute("DELETE FROM enrichment_queue WHERE obra_id = ANY(%s::uuid[])", ([r[0] for r in ids],))
except Exception:
    conn.rollback()
cur.execute("DELETE FROM obras WHERE id = ANY(%s::uuid[])", ([r[0] for r in ids],))
conn.commit()
cur.execute("SELECT COUNT(*) FROM obras WHERE fonte='teste_portao_v5'")
print("restantes_teste", cur.fetchone()[0])
cur.close(); conn.close()
failed = [r for r in results if not r["ok"]]
print("FAILED", len(failed))
print(json.dumps(results, ensure_ascii=False))
raise SystemExit(1 if failed else 0)
