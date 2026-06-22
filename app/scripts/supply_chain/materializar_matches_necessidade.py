#!/usr/bin/env python3
"""
materializar_matches_necessidade.py — Camada de PRECISÃO (#3 do ciclo da obra).
Para cada obra com necessidades[] enriquecidas, casa cada necessidade ESPECÍFICA com
fornecedores reais via necessidade_cnae_map.yaml (necessidade -> prefixos CNAE), priorizando
mesmo UF + porte, anexando decisor se houver. Substitui a média setorial do Leontief pelo
fornecedor exato do escopo. Idempotente.
Uso: docker exec wins_hub-api-1 python /app/scripts/supply_chain/materializar_matches_necessidade.py
"""
import os, unicodedata, yaml, psycopg2

_MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "necessidade_cnae_map.yaml")
TOP_N = 8


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def conn():
    return psycopg2.connect(host=os.getenv("DB_HOST", "db"), dbname=os.getenv("DB_NAME", "wins_hub"),
                            user=os.getenv("DB_USER", "postgres"), password=os.getenv("DB_PASSWORD", ""))


def cnae_para_necessidade(necessidade, regras):
    n = norm(necessidade)
    pref, labels = set(), []
    for chave, r in regras.items():
        if norm(chave) in n:
            pref.update(r["cnae"]); labels.append(r.get("label", chave))
    return sorted(pref), labels


def main():
    regras = yaml.safe_load(open(_MAP, encoding="utf-8"))["regras"]
    c = conn(); cur = c.cursor(); ins = c.cursor()
    cur.execute("DELETE FROM matches_necessidade_fornecedor")
    cur.execute("""SELECT id, uf, necessidades FROM obras
                   WHERE necessidades IS NOT NULL AND array_length(necessidades,1)>0
                     AND classificacao_computed='OURO' AND visivel""")
    obras = cur.fetchall()
    total = 0
    for oid, uf, neces in obras:
        for nec in (neces or []):
            pref, _ = cnae_para_necessidade(nec, regras)
            if not pref:
                continue
            like = " OR ".join(["cnae_principal LIKE %s"] * len(pref))
            cur.execute(f"""SELECT cnpj, razao_social, uf, capital_social FROM fornecedores
                            WHERE situacao_cadastral='02' AND ({like})
                            ORDER BY (uf=%s) DESC, capital_social DESC NULLS LAST LIMIT %s""",
                        [p + "%" for p in pref] + [uf, TOP_N])
            for cnpj, razao, fuf, cap in cur.fetchall():
                cur.execute("SELECT nome_pessoa FROM empresa_decisores_cache WHERE cnpj=%s AND excluido_em IS NULL LIMIT 1", (cnpj,))
                d = cur.fetchone(); dec = d[0] if d else None
                mesmo = (fuf == uf)
                score = ((50 if mesmo else 25)
                         + (30 if (cap or 0) >= 1e7 else 20 if (cap or 0) >= 1e6 else 10 if (cap or 0) >= 1e5 else 5)
                         + (20 if dec else 0))
                ins.execute("""INSERT INTO matches_necessidade_fornecedor
                  (obra_id,necessidade,cnae_prefixos,fornecedor_cnpj,fornecedor_razao,fornecedor_uf,mesmo_uf,capital_social,tem_decisor,decisor_nome,score)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (str(oid), nec, ",".join(pref), cnpj, razao, fuf, mesmo, cap, dec is not None, dec, score))
                total += 1
    c.commit()
    print(f"OK: {total} matches por necessidade em {len(obras)} obras")
    c.close()


if __name__ == "__main__":
    main()
