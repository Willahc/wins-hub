#!/usr/bin/env python3
"""
impacto_economico.py — Gerador de relatório de impacto econômico de obras (Modelo 3).

Dado CAPEX + setor + UF de uma obra, aplica a Matriz de Leontief (IBGE MIP 2015, Nível 67)
e devolve multiplicador de produção, produção total gerada, PIB (valor adicionado) e
empregos estimados (diretos+indiretos), além do encadeamento setorial.

Uso:
  python impacto_economico.py --obra <uuid>        # 1 obra (imprime + persiste)
  python impacto_economico.py --all                # todas OURO capex_fonte IS NULL (batch + agregado)
  python impacto_economico.py --all --no-persist   # batch sem gravar cache
"""
from __future__ import annotations
import os, json, argparse
import psycopg2, psycopg2.extras

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "leontief_2015_n67.json")
_MODEL = None


def model():
    global _MODEL
    if _MODEL is None:
        with open(_DATA, encoding="utf-8") as fh:
            _MODEL = json.load(fh)
    return _MODEL


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"), port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "postgres"), password=os.getenv("DB_PASSWORD", ""),
    )


def computar_impacto(valor_estimado, setor, uf=None, top_n=8):
    """PURO (sem DB): aplica o modelo Leontief e devolve o dict do relatório.
    Devolve None se não houver CAPEX. Setor desconhecido cai no default (Construção)."""
    if valor_estimado is None or float(valor_estimado) <= 0:
        return None
    m = model()
    setor = (setor or "").strip().upper()
    act = m["setor_map"].get(setor, m["default_atividade"])
    if act not in m["multiplicador"]:
        act = m["default_atividade"]
    capex = float(valor_estimado)
    capex_mi = capex / 1e6
    mult = float(m["multiplicador"][act])
    jpm = float(m["jobs_por_milhao"].get(act, 6.0))
    va = float(m["va_ratio"])
    producao_mi = mult * capex_mi               # R$ milhões de produção total
    pib_mi = producao_mi * va
    empregos = int(round(producao_mi * jpm))
    # encadeamento setorial (top_n setores da cadeia, em R$ milhões)
    col = m["leontief"].get(act, {})
    top = sorted(col.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    breakdown = [{"setor_cod": c, "setor_nome": m["atividades"].get(c, c),
                  "producao_mi": round(v * capex_mi, 1)} for c, v in top]
    return {
        "setor_obra": setor or None, "uf": uf,
        "setor_ibge": act, "atividade_nome": m["atividades"].get(act, act),
        "capex_bi": round(capex / 1e9, 3),
        "multiplicador": round(mult, 3),
        "producao_gerada_bi": round(producao_mi / 1e3, 3),
        "pib_va_bi": round(pib_mi / 1e3, 3),
        "empregos_estimados": empregos,
        "encadeamento_setorial": breakdown,
        "fonte": m["fonte"],
    }


_UPSERT = """
INSERT INTO obras_impacto_economico
  (obra_id, setor_ibge, atividade_nome, multiplicador, capex_bi,
   producao_gerada_bi, pib_va_bi, empregos_estimados, gerado_em)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (obra_id) DO UPDATE SET
  setor_ibge=EXCLUDED.setor_ibge, atividade_nome=EXCLUDED.atividade_nome,
  multiplicador=EXCLUDED.multiplicador, capex_bi=EXCLUDED.capex_bi,
  producao_gerada_bi=EXCLUDED.producao_gerada_bi, pib_va_bi=EXCLUDED.pib_va_bi,
  empregos_estimados=EXCLUDED.empregos_estimados, gerado_em=now();
"""


def gerar_relatorio_impacto(obra_id, conn=None, persist=True):
    """Busca CAPEX/setor/UF da obra, computa o impacto e (opcional) grava no cache."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute("SELECT id, nome, setor, uf, valor_estimado, capex_fonte, "
                      "classificacao_computed FROM obras WHERE id=%s", (str(obra_id),))
            o = c.fetchone()
        if not o:
            return None
        rel = computar_impacto(o["valor_estimado"], o["setor"], o["uf"])
        if rel is None:
            return None
        rel["obra_id"] = str(o["id"])
        rel["obra_nome"] = o["nome"]
        if persist:
            with conn.cursor() as c:
                c.execute(_UPSERT, (str(o["id"]), rel["setor_ibge"], rel["atividade_nome"],
                                    rel["multiplicador"], rel["capex_bi"],
                                    rel["producao_gerada_bi"], rel["pib_va_bi"],
                                    rel["empregos_estimados"]))
            conn.commit()
        return rel
    finally:
        if own:
            conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obra")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args()
    persist = not args.no_persist
    conn = get_conn()
    try:
        if args.obra:
            r = gerar_relatorio_impacto(args.obra, conn, persist)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return
        if args.all:
            with conn.cursor() as c:
                c.execute("SELECT id FROM obras WHERE classificacao_computed='OURO' AND visivel "
                          "AND capex_fonte IS NULL AND valor_estimado IS NOT NULL AND valor_estimado>0")
                ids = [r[0] for r in c.fetchall()]
            n = 0
            tot_capex = tot_prod = tot_pib = 0.0
            tot_emp = 0
            for oid in ids:
                r = gerar_relatorio_impacto(oid, conn, persist)
                if not r:
                    continue
                n += 1
                tot_capex += r["capex_bi"]; tot_prod += r["producao_gerada_bi"]
                tot_pib += r["pib_va_bi"]; tot_emp += r["empregos_estimados"]
            print(json.dumps({
                "obras": n,
                "capex_total_bi": round(tot_capex, 1),
                "producao_total_gerada_bi": round(tot_prod, 1),
                "pib_va_total_bi": round(tot_pib, 1),
                "empregos_totais_estimados": tot_emp,
            }, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
