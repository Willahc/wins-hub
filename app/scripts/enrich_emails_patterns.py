"""
enrich_emails_patterns.py — Testa múltiplos patterns de email por decisor.

Hunter email-finder testa 1 pattern (firstlast). Mas empresas usam variados:
  - first.last@dom (Petrobras, Vale)
  - flast@dom (CTEEP, etc)
  - firstl@dom
  - first_last@dom

Pra cada decisor OURO/PRATA sem email mas com LK + domínio cacheado:
  1. Hunter email-verifier (não email-finder) — testa cada pattern (1 crédito/pattern)
  2. Apply primeiro com status=valid OU score>=70
"""
import argparse, os, re, sys, time
from collections import defaultdict
sys.path.insert(0, "/app")
import psycopg2
import requests
from unidecode import unidecode

DB = {"host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
      "dbname": os.getenv("DB_NAME","wins_hub"), "user": os.getenv("DB_USER","postgres"),
      "password": os.getenv("DB_PASSWORD","")}
HUNTER_VERIFIER = "https://api.hunter.io/v2/email-verifier"

def _norm(s): return unidecode((s or "").strip().lower())

def gerar_patterns(nome, dominio):
    parts = [p for p in re.split(r"\s+", _norm(nome)) if p.isalpha()]
    if len(parts) < 2: return []
    first, last = parts[0], parts[-1]
    if len(first) < 2 or len(last) < 2: return []
    return [
        f"{first}.{last}@{dominio}",   # first.last
        f"{first}{last}@{dominio}",     # firstlast
        f"{first[0]}{last}@{dominio}",  # flast
        f"{first}_{last}@{dominio}",    # first_last
        f"{first}{last[0]}@{dominio}",  # firstl
    ]

def verify(email):
    key = os.getenv("HUNTER_API_KEY","").strip()
    try:
        r = requests.get(HUNTER_VERIFIER, params={"email": email, "api_key": key}, timeout=20)
        if r.status_code != 200: return None
        d = r.json().get("data") or {}
        return {"status": d.get("status"), "score": d.get("score"), "accept_all": d.get("accept_all")}
    except: return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-patterns-por-decisor", type=int, default=3,
                    help="máx créditos por decisor (default 3 → testa first.last, firstlast, flast)")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (lower(d.nome), ed.dominio)
          d.nome, d.cargo, ed.dominio
        FROM decisores_obra d
        JOIN obras o ON o.id = d.obra_id
        JOIN empresa_dominios ed ON regexp_replace(ed.cnpj,'[^0-9]','','g') = regexp_replace(COALESCE(o.cnpj,''),'[^0-9]','','g')
        WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel
          AND d.excluido_em IS NULL
          AND COALESCE(d.email,'') = ''
          AND COALESCE(d.linkedin_url,'') <> ''
          AND COALESCE(d.hipotese_replicacao,'') <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO'
          AND d.nome ~ '[A-Za-zÀ-ú]{3,}\s+[A-Za-zÀ-ú]{3,}'
          AND d.nome NOT ILIKE 'Contato Comercial%' AND d.nome NOT ILIKE 'Equipe %'
          AND d.nome NOT ILIKE 'Gerência %' AND d.nome NOT ILIKE 'Site Manager%'
        ORDER BY lower(d.nome), ed.dominio
    """)
    candidatos = cur.fetchall()
    if args.limit: candidatos = candidatos[:args.limit]
    print(f"\nPatterns expandidos: {len(candidatos)} (nome,dominio) únicos")
    print(f"Modo: {'COMMIT' if args.commit else 'DRY-RUN'}")
    print(f"Custo máx: {len(candidatos) * args.max_patterns_por_decisor} créditos Hunter\n")
    if not args.commit:
        for n,c,d in candidatos[:5]:
            print(f"  {n[:25]:25} @ {d} → patterns: {gerar_patterns(n,d)[:args.max_patterns_por_decisor]}")
        return

    found = 0; creditos = 0
    t0 = time.time()
    for i,(nome, cargo, dominio) in enumerate(candidatos,1):
        patterns = gerar_patterns(nome, dominio)[:args.max_patterns_por_decisor]
        email_valido = None; status_final = None
        for em in patterns:
            res = verify(em); creditos += 1
            if not res: continue
            if res.get("status") == "valid":
                email_valido = em; status_final = "valid"; break
            if res.get("status") == "accept_all" and res.get("score", 0) >= 70:
                email_valido = em; status_final = "accept_all"; break
        if email_valido:
            cur.execute("""
                UPDATE decisores_obra SET email=%s,
                  confianca_match_componentes=COALESCE(confianca_match_componentes,'{}'::jsonb)
                    || jsonb_build_object('email_via_pattern_verified', jsonb_build_object('pattern', %s, 'status', %s, 'ts', NOW()::text))
                WHERE lower(nome)=lower(%s) AND COALESCE(email,'')='' AND excluido_em IS NULL
            """, (email_valido, email_valido, status_final, nome))
            n = cur.rowcount
            found += 1
            print(f"[{i:03d}/{len(candidatos)}] OK {nome[:25]:25} → {email_valido[:35]} status={status_final} ({n} obras)")
            conn.commit()
        else:
            print(f"[{i:03d}/{len(candidatos)}] -- {nome[:25]:25} @ {dominio} (testou {len(patterns)} patterns)")
        if creditos > 200:  # cap defensivo
            print("  CAP 200 créditos atingido — break"); break

    print(f"\n  Emails encontrados: {found}")
    print(f"  Créditos consumidos: {creditos}")
    print(f"  Tempo: {(time.time()-t0)/60:.1f} min")

if __name__ == "__main__":
    main()
