#!/usr/bin/env python3
"""enforce_sample.py — Fase 3 ENFORCE com permitir_externo=True (Serper real).
Pega uma AMOSTRA de obras bndes (30d) que passam o portão mas NÃO resolveram domínio
interno, e roda avaliar(permitir_externo=True, web_search_fn) p/ medir o ganho externo.
Controla custo Serper via LIMIT (default 12). Não altera o banco.

Uso: docker exec wins_hub-api-1 python /app/scripts/portao/regression/enforce_sample.py [LIMIT]
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import portao
from web_search_serper import web_search_fn
import psycopg2.extras

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 12


def main():
    conn = portao.get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, nome, empresa, cnpj, setor, valor_estimado, uf, municipio, capex_fonte,
               COALESCE(fonte,'') fonte
        FROM obras
        WHERE criado_em >= now() - interval '30 days' AND fonte ILIKE 'bndes%%'
          AND cnpj ~ '^[0-9]{14}$'
        ORDER BY criado_em DESC
    """)
    rows = cur.fetchall()

    # seleciona os que passam mas com domínio externo_pendente (sem domínio interno)
    sample = []
    for r in rows:
        obra = {"nome": r["nome"], "empresa": r["empresa"], "cnpj": r["cnpj"], "setor": r["setor"],
                "valor_estimado": float(r["valor_estimado"]) if r["valor_estimado"] is not None else None,
                "uf": r["uf"], "municipio": r["municipio"], "capex_fonte": r["capex_fonte"],
                "_self_id": str(r["id"])}
        v = portao.avaliar(obra, {"fonte": r["fonte"], "fonte_tipo": "OFICIAL"}, conn, permitir_externo=False)
        if v["passou"] and v["origem_resolucao"].get("dominio") == "externo_pendente":
            sample.append((obra, r))
        if len(sample) >= LIMIT:
            break

    print(f"=== ENFORCE Fase 3 (permitir_externo=True, Serper) — amostra {len(sample)} obras bndes sem domínio interno ===\n")
    res = {"dom_serper": 0, "razao_brasilapi": 0}
    for obra, r in sample:
        v = portao.avaliar(obra, {"fonte": r["fonte"], "fonte_tipo": "OFICIAL"},
                           conn, permitir_externo=True, web_search_fn=web_search_fn)
        o = v["origem_resolucao"]
        dom = v["enriquecimento"]["dominio"]
        if o.get("dominio") == "web_search":
            res["dom_serper"] += 1
        if o.get("cnpj") == "brasilapi":
            res["razao_brasilapi"] += 1
        print(f"  {(r['empresa'] or '')[:38]:<38} dom={str(dom)[:28]:<28} origem_dom={o.get('dominio')}")
    n = max(len(sample), 1)
    print(f"\nResolvido por Serper (domínio): {res['dom_serper']}/{len(sample)} ({100*res['dom_serper']/n:.0f}%)")
    print(f"Razão preenchida via BrasilAPI: {res['razao_brasilapi']}/{len(sample)}")
    print("Hunter: 0 inline (sempre batch noturno).")


if __name__ == "__main__":
    main()
