#!/usr/bin/env python3
"""
materializar_cadeia_obra.py — Popula matches_cadeia_obra (camada 3 do ciclo da obra).
Para cada obra OURO, aplica o motor Leontief (impacto_economico.model) e grava 1 linha
por divisão CNAE de insumo com demanda > R$10mi, cruzando com o inventário de fornecedores
(na base, no UF da obra, com decisor). Idempotente (limpa e repopula).
Uso: python materializar_cadeia_obra.py            # todas OURO
     python materializar_cadeia_obra.py --obra <id> # 1 obra (não limpa as outras)
"""
import sys, argparse
from collections import defaultdict
sys.path.insert(0, "/app/scripts")
import impacto_economico as ie


def _inventarios(cur):
    cur.execute("SELECT divisao_cnae, count(*) FROM fornecedores WHERE situacao_cadastral='02' AND divisao_cnae IS NOT NULL GROUP BY 1")
    inv = {d: c for d, c in cur.fetchall()}
    cur.execute("SELECT divisao_cnae, uf, count(*) FROM fornecedores WHERE situacao_cadastral='02' AND divisao_cnae IS NOT NULL AND uf IS NOT NULL GROUP BY 1,2")
    inv_uf = {(d, u): c for d, u, c in cur.fetchall()}
    cur.execute("""SELECT f.divisao_cnae, count(*) FROM fornecedores f
                   JOIN decisores_preservados p ON p.cnpj=f.cnpj
                   WHERE f.situacao_cadastral='02' AND f.divisao_cnae IS NOT NULL GROUP BY 1""")
    inv_dec = {d: c for d, c in cur.fetchall()}
    return inv, inv_uf, inv_dec


def _linhas_obra(m, setor, uf, valor, inv, inv_uf, inv_dec, piso_mi=10.0):
    act = m["setor_map"].get((setor or "").upper(), m["default_atividade"])
    if act not in m["multiplicador"]:
        act = m["default_atividade"]
    capex_mi = float(valor) / 1e6
    dem = defaultdict(float); coefs = defaultdict(float); nome = {}
    for sup, coef in m["leontief"].get(act, {}).items():
        d = sup[:2]
        dem[d] += coef * capex_mi; coefs[d] += coef
        nome.setdefault(d, m["atividades"].get(sup, "")[:40])
    out = []
    for d, v in dem.items():
        if v <= piso_mi:
            continue
        out.append((d, nome[d], round(coefs[d], 5), round(v, 1),
                    inv.get(d, 0), inv_uf.get((d, uf), 0), inv_dec.get(d, 0)))
    return out


_INS = """INSERT INTO matches_cadeia_obra
  (obra_id, cnae_insumo_div, setor_insumo_nome, coeficiente_leontief, demanda_estimada_mi,
   fornecedores_na_base, fornecedores_no_uf, com_decisor)
  VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"""


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--obra"); args = ap.parse_args()
    m = ie.model(); conn = ie.get_conn(); cur = conn.cursor()
    inv, inv_uf, inv_dec = _inventarios(cur)
    if args.obra:
        cur.execute("SELECT id,setor,uf,valor_estimado FROM obras WHERE id=%s", (args.obra,))
        obras = cur.fetchall()
        cur.execute("DELETE FROM matches_cadeia_obra WHERE obra_id=%s", (args.obra,))
    else:
        cur.execute("""SELECT id,setor,uf,valor_estimado FROM obras
                       WHERE classificacao_computed='OURO' AND visivel AND valor_estimado>0 AND capex_fonte IS NULL""")
        obras = cur.fetchall()
        cur.execute("DELETE FROM matches_cadeia_obra")
    ins = obras_n = 0
    for oid, setor, uf, valor in obras:
        linhas = _linhas_obra(m, setor, uf, valor, inv, inv_uf, inv_dec)
        for ln in linhas:
            cur.execute(_INS, (str(oid),) + ln); ins += 1
        if linhas:
            obras_n += 1
    conn.commit()
    print(f"OK: linhas={ins} obras={obras_n}")
    cur.execute("""SELECT cnae_insumo_div, count(*) AS obras, round(sum(demanda_estimada_mi)/1000,1) AS demanda_bi,
                   max(fornecedores_na_base) AS inv FROM matches_cadeia_obra GROUP BY 1 ORDER BY demanda_bi DESC LIMIT 12""")
    print(f"\n{'div':<5}{'obras':>7}{'demanda_bi':>12}{'inventario':>12}")
    for d, o, db, iv in cur.fetchall():
        print(f"{d:<5}{o:>7}{db:>12}{iv:>12}")
    conn.close()


if __name__ == "__main__":
    main()
