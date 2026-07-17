#!/usr/bin/env python3
import os, sys, json
sys.path.insert(0, "/app/services")
sys.path.insert(0, "/app/scripts/portao")
import psycopg2
from psycopg2.extras import RealDictCursor
from portao_gate_v5 import decidir_portao, VALOR_MINIMO

conn = psycopg2.connect(
    host=os.getenv("DB_HOST", "db"),
    dbname=os.getenv("DB_NAME", "wins_hub"),
    user=os.getenv("DB_USER", "wins_app"),
    password=os.getenv("DB_PASSWORD", ""),
)
cur = conn.cursor(cursor_factory=RealDictCursor)

def sample(status, n=200):
    cur.execute(
        """
      SELECT id::text, nome, descricao, descricao_publica, setor, fonte, fonte_tipo,
             valor_estimado, capex_fonte, url_fonte, cnpj, empresa, id_externo,
             fase, status_licenca, status_portao, classificacao_computed, visivel
      FROM public.obras WHERE status_portao=%s
      ORDER BY random() LIMIT %s
    """,
        (status, n),
    )
    return [dict(r) for r in cur.fetchall()]

def agree(row, expected_group):
    d = decidir_portao(row, conn=None)
    if expected_group == "APROVADA":
        if d.status_portao == "REJEITADA" and d.confianca >= 0.9:
            return False, d
        v = row.get("valor_estimado")
        if v is not None and float(v) < VALOR_MINIMO:
            return False, d
        return True, d
    if expected_group == "REJEITADA":
        ok = d.status_portao == "REJEITADA" or (
            row.get("valor_estimado") is not None and float(row["valor_estimado"]) < VALOR_MINIMO
        )
        return ok, d
    return d.status_portao in ("EM_ANALISE", "EM_ANALISE_MANUAL") or d.confianca < 0.85, d

results = {}
for st, grp, n in [
    ("APROVADA", "APROVADA", 200),
    ("REJEITADA", "REJEITADA", 200),
    ("EM_ANALISE_MANUAL", "ANALISE", 200),
]:
    rows = sample(st, n)
    ok = 0
    fails = []
    for r in rows:
        good, d = agree(r, grp)
        if good:
            ok += 1
        else:
            fails.append(
                {
                    "id": r["id"],
                    "nome": (r["nome"] or "")[:80],
                    "dec": d.status_portao,
                    "motivo": d.motivo,
                }
            )
    results[st] = {
        "n": len(rows),
        "ok": ok,
        "prec": round(100 * ok / max(1, len(rows)), 2),
        "fails_sample": fails[:8],
    }

for tier in ("OURO", "PRATA", "BRONZE", "PIPELINE"):
    cur.execute(
        """
      SELECT COUNT(*) AS n FROM public.obras
      WHERE classificacao_computed=%s AND status_portao='APROVADA'
        AND (visivel IS NULL OR visivel) AND empresa IS NOT NULL AND empresa<>''
    """,
        (tier,),
    )
    results[f"tier_{tier}_vitrine"] = cur.fetchone()["n"]
    # all OURO/PRATA vitrine must be APROVADA - check non-aprovada with that tier still visible
    cur.execute(
        """
      SELECT COUNT(*) AS n FROM public.obras
      WHERE classificacao_computed=%s AND status_portao IS DISTINCT FROM 'APROVADA'
        AND visivel IS TRUE
    """,
        (tier,),
    )
    results[f"tier_{tier}_nao_aprovada_visivel"] = cur.fetchone()["n"]

cur.execute(
    """
  SELECT COUNT(*) AS n FROM public.obras
  WHERE status_portao='APROVADA' AND (visivel IS NULL OR visivel)
    AND empresa IS NOT NULL AND empresa<>''
    AND (
      lower(coalesce(nome,'')||' '||coalesce(descricao,'')) ~ 'software|consultoria|licen[cç]a de software'
      OR (valor_estimado IS NOT NULL AND valor_estimado < 100000)
    )
"""
)
results["suspeitos_visiveis"] = cur.fetchone()["n"]

cur.execute(
    "SELECT COUNT(*) AS n FROM public.obras WHERE status_portao IS DISTINCT FROM 'APROVADA' AND visivel IS TRUE"
)
results["nao_aprovada_visivel_true"] = cur.fetchone()["n"]

cur.execute("SELECT COUNT(*) AS n FROM public.obras WHERE status_portao IS NULL")
results["null_restantes"] = cur.fetchone()["n"]

cur.execute(
    """
  SELECT COUNT(*) AS n FROM public.obras
  WHERE status_portao='APROVADA' AND (visivel IS NULL OR visivel)
    AND empresa IS NOT NULL AND empresa<>''
"""
)
results["vitrine_final"] = cur.fetchone()["n"]

print(json.dumps(results, ensure_ascii=False, indent=2))
open("/tmp/amostragem_precisao.json", "w").write(json.dumps(results, ensure_ascii=False, indent=2))
conn.close()
