#!/usr/bin/env python3
"""
resolve_cnpj_deterministic.py — Resolve CNPJs deterministamente buscando no banco de dados local.
"""
import sys, re, unicodedata
sys.path.insert(0, "/app")
import psycopg2, psycopg2.extras
from services.brasilapi import DB_CONFIG

GENERIC = {"usina","consorcio","unidade","agencia","projeto","lt","linha","sistema","ferrograo",
           "transmissao","leilao","lote","obra","obras","planta","terminal","porto","campo","nova"}

def deaccent(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii","ignore").decode()

def valida_cnpj(c):
    c = re.sub(r"\D","",c or "")
    if len(c)!=14 or c==c[0]*14: return False
    def dv(b,p): s=sum(int(b[i])*p[i] for i in range(len(p))); r=s%11; return '0' if r<2 else str(11-r)
    return dv(c,[5,4,3,2,9,8,7,6,5,4,3,2])==c[12] and dv(c,[6,5,4,3,2,9,8,7,6,5,4,3,2])==c[13]

def main():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    cur.execute("""
      SELECT o.id, o.nome, o.empresa,
        (SELECT split_part(d.cargo,'@',2) FROM decisores_obra d
         WHERE d.obra_id=o.id AND d.cargo LIKE '%@%' AND d.excluido_em IS NULL LIMIT 1) cargo_emp
      FROM obras o
      WHERE o.visivel
        AND (o.cnpj IS NULL OR o.cnpj = '' OR NOT cnpj_valido(o.cnpj))
    """)
    obras = cur.fetchall()
    print(f"Obras sem CNPJ qualificadas: {len(obras)}\n")
    
    hit = 0
    for o in obras:
        emp = o["empresa"]
        if not emp:
            for sep in (" — ", " – ", " - "):
                if sep in o["nome"]:
                    cand = o["nome"].split(sep)[0].strip()
                    if 3 <= len(cand) <= 45 and cand.lower().split()[0] not in GENERIC:
                        emp = cand
                        break
            if not emp and o["cargo_emp"]:
                emp = o["cargo_emp"].strip()
                
        if not emp:
            continue
            
        emp_clean = emp.strip()
        cur.execute("SELECT cnpj, razao_social FROM fornecedores WHERE lower(razao_social) = lower(%s) AND cnpj IS NOT NULL LIMIT 1", (emp_clean,))
        row = cur.fetchone()
        if not row:
            prefix = emp_clean.split()[0]
            if len(prefix) >= 4 and prefix.lower() not in GENERIC:
                cur.execute("SELECT cnpj, razao_social FROM fornecedores WHERE lower(razao_social) LIKE lower(%s) AND cnpj IS NOT NULL ORDER BY length(razao_social) LIMIT 3", (prefix + "%",))
                rows = cur.fetchall()
                valid = [r for r in rows if valida_cnpj(r["cnpj"])]
                if valid:
                    row = valid[0]
                    
        if row and valida_cnpj(row["cnpj"]):
            hit += 1
            print(f"  ✓ [{emp_clean[:25]}] -> {row['cnpj']} {row['razao_social'][:35]}")
            cur.execute("""
                UPDATE obras SET cnpj = %s, cnpj_status = 'resolvido_deterministic_db',
                observacoes_validacao = COALESCE(observacoes_validacao || ' | ', '') || %s
                WHERE id = %s
            """, (row["cnpj"], f"deterministic_match_db: {row['razao_social']}", o["id"]))
            cur.execute("SELECT recompute_classificacao_obra(%s)", (o["id"],))
            conn.commit()
            
    print(f"\nTotal resolvido deterministicamente: {hit} de {len(obras)}")
    conn.close()

if __name__ == "__main__":
    main()
