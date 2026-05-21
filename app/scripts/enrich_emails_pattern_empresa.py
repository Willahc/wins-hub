"""
enrich_emails_pattern_empresa.py — Pattern por empresa via cross-reference.

Pra cada empresa onde temos >=2 emails confirmados:
  1. Detecta pattern dominante (first.last vs flast vs firstlast)
  2. Pra decisores sem email da MESMA empresa, gera candidato e valida via Hunter verifier
  3. Apply só se status=valid (não accept_all)
"""
import argparse, os, re, sys, time
from collections import Counter
sys.path.insert(0, "/app")
import psycopg2
import requests
from unidecode import unidecode

DB = {"host":os.getenv("DB_HOST","db"),"port":int(os.getenv("DB_PORT","5432")),
      "dbname":os.getenv("DB_NAME","wins_hub"),"user":os.getenv("DB_USER","postgres"),
      "password":os.getenv("DB_PASSWORD","")}
HUNTER_VERIFIER = "https://api.hunter.io/v2/email-verifier"

def _norm(s): return unidecode((s or "").strip().lower())

def detectar_pattern(samples):
    """Dado lista de (local, nome) confirmados, infere pattern dominante."""
    votes = Counter()
    for local, nome in samples:
        parts = [p for p in re.split(r"\s+", _norm(nome)) if p.isalpha() and len(p) >= 2]
        if len(parts) < 2: continue
        first, last = parts[0], parts[-1]
        local_norm = local.lower()
        if local_norm == f"{first}.{last}": votes["first.last"] += 1
        elif local_norm == f"{first}{last}": votes["firstlast"] += 1
        elif local_norm == f"{first[0]}{last}": votes["flast"] += 1
        elif local_norm == f"{first}{last[0]}": votes["firstl"] += 1
        elif local_norm == f"{first}_{last}": votes["first_last"] += 1
        # else: skip (no match)
    if not votes: return None
    return votes.most_common(1)[0]  # (pattern, count)

def gen_email(pattern, nome, dominio):
    parts = [p for p in re.split(r"\s+", _norm(nome)) if p.isalpha() and len(p) >= 2]
    if len(parts) < 2: return None
    first, last = parts[0], parts[-1]
    fmt = {
        "first.last": f"{first}.{last}",
        "firstlast": f"{first}{last}",
        "flast": f"{first[0]}{last}",
        "firstl": f"{first}{last[0]}",
        "first_last": f"{first}_{last}",
    }
    local = fmt.get(pattern)
    return f"{local}@{dominio}" if local else None

def verify(email):
    key = os.getenv("HUNTER_API_KEY","").strip()
    try:
        r = requests.get(HUNTER_VERIFIER, params={"email": email, "api_key": key}, timeout=20)
        if r.status_code != 200: return None
        d = r.json().get("data") or {}
        return {"status": d.get("status"), "score": d.get("score")}
    except: return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = psycopg2.connect(**DB); cur = conn.cursor()

    # 1. Mapear pattern por domínio
    cur.execute("""
        WITH com_email AS (
          SELECT DISTINCT d.nome,
            LOWER(split_part(d.email, '@', 2)) AS dominio,
            LOWER(split_part(d.email, '@', 1)) AS local
          FROM decisores_obra d JOIN obras o ON o.id = d.obra_id
          WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel
            AND d.excluido_em IS NULL AND COALESCE(d.email,'') <> ''
            AND COALESCE(d.hipotese_replicacao,'') <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO'
            AND d.nome ~ '[A-Za-zÀ-ú]{3,}\s+[A-Za-zÀ-ú]{3,}'
        )
        SELECT dominio, array_agg(ARRAY[local, nome]) AS samples
        FROM com_email WHERE dominio IS NOT NULL
        GROUP BY dominio HAVING COUNT(*) >= 2
    """)
    patterns = {}
    for dom, samples_raw in cur.fetchall():
        samples = [(s[0], s[1]) for s in samples_raw]
        pat = detectar_pattern(samples)
        if pat and pat[1] >= 1:  # pelo menos 2 confirmações do mesmo pattern
            patterns[dom] = pat[0]
            print(f"  {dom:35} → {pat[0]:12} ({pat[1]} confirmações)")

    print(f"\nPatterns detectados em {len(patterns)} domínios\n")

    # 2. Pra cada decisor sem email cujo domínio tem pattern, tentar
    cur.execute("""
        SELECT DISTINCT ON (lower(d.nome), ed.dominio)
          d.nome, ed.dominio
        FROM decisores_obra d
        JOIN obras o ON o.id = d.obra_id
        JOIN empresa_dominios ed ON regexp_replace(ed.cnpj,'[^0-9]','','g') = regexp_replace(COALESCE(o.cnpj,''),'[^0-9]','','g')
        WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel
          AND d.excluido_em IS NULL
          AND COALESCE(d.email,'') = '' AND COALESCE(d.linkedin_url,'') <> ''
          AND COALESCE(d.hipotese_replicacao,'') <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO'
          AND d.nome ~ '[A-Za-zÀ-ú]{3,}\s+[A-Za-zÀ-ú]{3,}'
          AND d.nome NOT ILIKE 'Contato Comercial%' AND d.nome NOT ILIKE 'Equipe %'
        ORDER BY lower(d.nome), ed.dominio
    """)
    candidatos = cur.fetchall()
    cand_com_pattern = [(n,d) for n,d in candidatos if d in patterns]
    print(f"Candidatos com pattern conhecido: {len(cand_com_pattern)}/{len(candidatos)}")
    print(f"Custo: {len(cand_com_pattern)} créditos Hunter\n")

    if not args.commit:
        for n,d in cand_com_pattern[:10]:
            print(f"  {n[:25]:25} @ {d:25} → {gen_email(patterns[d],n,d)}")
        return

    found = 0; creditos = 0
    for i,(nome, dominio) in enumerate(cand_com_pattern, 1):
        email = gen_email(patterns[dominio], nome, dominio)
        if not email: continue
        res = verify(email); creditos += 1
        if res and res.get("status") in ("valid","accept_all") and (res.get("score",0) >= 60 or res.get("status")=="valid"):
            cur.execute("""
                UPDATE decisores_obra SET email=%s,
                  confianca_match_componentes=COALESCE(confianca_match_componentes,'{}'::jsonb)
                    || jsonb_build_object('pattern_empresa', jsonb_build_object(
                         'pattern', %s, 'status', %s, 'score', %s, 'ts', NOW()::text))
                WHERE lower(nome)=lower(%s) AND COALESCE(email,'')='' AND excluido_em IS NULL
            """, (email, patterns[dominio], res.get("status"), res.get("score"), nome))
            n = cur.rowcount
            found += 1
            print(f"[{i:03d}/{len(cand_com_pattern)}] OK {nome[:25]:25} → {email[:35]} status={res.get('status')} score={res.get('score')} ({n} obras)")
            conn.commit()
        else:
            st = (res or {}).get("status","fail")
            print(f"[{i:03d}/{len(cand_com_pattern)}] -- {nome[:25]:25} → {email[:35]} status={st}")

    print(f"\n  Encontrados: {found}/{len(cand_com_pattern)}")
    print(f"  Créditos consumidos: {creditos}")

if __name__ == "__main__":
    main()
