#!/usr/bin/env python3
"""shadow_replay.py — SHADOW PURO. Reprocessa as obras que os captadores piloto JÁ
produziram (últimos N dias) através de portao.avaliar(), sem alterar nada.
Mostra: % PASSOU vs DESCARTOU por fonte, origem da resolução (interno/externo),
e motivos de descarte. ANEEL está offline desde 20/05 e notícias custam Haiku ao vivo,
então o replay sobre as linhas reais é a forma fiel e grátis de medir o gate.

Uso: docker exec wins_hub-api-1 python /app/scripts/portao/regression/shadow_replay.py [dias]
"""
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import portao
import psycopg2.extras

DIAS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
# pilotos: oficiais estruturados (aneel/bndes/antt) + pipeline de notícia
FILTRO = ("(fonte ILIKE 'aneel%%' OR fonte ILIKE 'bndes%%' OR fonte ILIKE 'antt%%' "
          "OR fonte_tipo='NOTICIA')")

SQL = f"""
SELECT id, nome, empresa, cnpj, setor, valor_estimado, uf, municipio, capex_fonte,
       COALESCE(fonte,'') fonte, COALESCE(fonte_tipo,'OFICIAL') fonte_tipo, descricao
FROM obras
WHERE criado_em >= now() - interval '{DIAS} days' AND {FILTRO}
"""


def grupo(fonte, fonte_tipo):
    if fonte_tipo == "NOTICIA":
        return "NOTICIA:" + (fonte or "?")
    for pref in ("aneel", "bndes", "antt"):
        if fonte.startswith(pref):
            return "OFICIAL:" + pref
    return "OFICIAL:" + fonte


def main():
    conn = portao.get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(SQL)
    rows = cur.fetchall()
    agg = collections.defaultdict(lambda: {"total": 0, "passou": 0, "desc": 0})
    motivos = collections.Counter()
    origem = {"cnpj_interno": 0, "dominio_interno": 0, "decisor_interno": 0, "passou_total": 0}
    tiers = collections.Counter()

    for r in rows:
        obra = {
            "nome": r["nome"], "empresa": r["empresa"], "cnpj": r["cnpj"], "setor": r["setor"],
            "valor_estimado": float(r["valor_estimado"]) if r["valor_estimado"] is not None else None,
            "uf": r["uf"], "municipio": r["municipio"], "capex_fonte": r["capex_fonte"],
            "descricao": r["descricao"], "_self_id": str(r["id"]),
        }
        # p/ notícia, a linha já está parseada -> usa os próprios campos como "haiku extraiu"
        if r["fonte_tipo"] == "NOTICIA":
            obra["haiku_extraiu"] = {"cnpj": r["cnpj"], "valor": obra["valor_estimado"],
                                     "setor": r["setor"], "empresa": r["empresa"]}
        v = portao.avaliar(obra, {"fonte": r["fonte"], "fonte_tipo": r["fonte_tipo"]}, conn)
        g = grupo(r["fonte"], r["fonte_tipo"])
        agg[g]["total"] += 1
        if v["passou"]:
            agg[g]["passou"] += 1
            origem["passou_total"] += 1
            tiers[v["tier"]] += 1
            o = v.get("origem_resolucao", {})
            if (o.get("cnpj") or "").startswith("interno"): origem["cnpj_interno"] += 1
            if o.get("dominio") == "interno": origem["dominio_interno"] += 1
            if o.get("decisor") == "interno": origem["decisor_interno"] += 1
        else:
            agg[g]["desc"] += 1
            motivos[v["motivo"]] += 1

    print(f"=== SHADOW REPLAY — últimos {DIAS} dias | {len(rows)} linhas (pilotos: aneel+bndes+antt + notícias) ===\n")
    print(f"{'fonte/grupo':<34} {'total':>6} {'PASSOU':>7} {'DESCART':>8} {'%pass':>6}")
    for g in sorted(agg, key=lambda k: -agg[k]["total"]):
        a = agg[g]; pct = 100 * a["passou"] / max(a["total"], 1)
        print(f"{g:<34} {a['total']:>6} {a['passou']:>7} {a['desc']:>8} {pct:>5.0f}%")
    tot = sum(a["total"] for a in agg.values())
    pas = sum(a["passou"] for a in agg.values())
    print(f"{'TOTAL':<34} {tot:>6} {pas:>7} {tot-pas:>8} {100*pas/max(tot,1):>5.0f}%")

    print(f"\n=== Origem da resolução (entre as {origem['passou_total']} que PASSARAM) ===")
    pt = max(origem["passou_total"], 1)
    print(f"  CNPJ resolvido interno   : {origem['cnpj_interno']:>5}  ({100*origem['cnpj_interno']/pt:.0f}%)")
    print(f"  Domínio resolvido interno: {origem['dominio_interno']:>5}  ({100*origem['dominio_interno']/pt:.0f}%)")
    print(f"  Decisor reusado interno  : {origem['decisor_interno']:>5}  ({100*origem['decisor_interno']/pt:.0f}%)")
    print(f"  (o restante iria a web_search free-first / BrasilAPI; Hunter só batch noturno)")

    print(f"\n=== Tier atribuído (shadow) ===")
    for t, n in tiers.most_common():
        print(f"  {t:<10} {n}")

    print(f"\n=== Top motivos de DESCARTE ===")
    for m, n in motivos.most_common(10):
        print(f"  {m:<34} {n}")


if __name__ == "__main__":
    main()
