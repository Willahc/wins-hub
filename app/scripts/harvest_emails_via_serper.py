"""
harvest_emails_via_serper.py — Email harvest via Serper SERP scraping.
Implementa a lógica essencial do theHarvester (email enumeration) sem deps externas:
  1. Pra cada (empresa, dominio) target, Serper queries com pattern '@dominio'
  2. Parse emails das SERPs (title + snippet)
  3. Filter por dominio target + cross-match nome decisor conhecido
  4. UPDATE obras.nivel1_email se vazio e match defensável

Foca em OURO/PRATA sem email mas com LK (cross-match por nome).
"""
import argparse, json, os, re, sys, time
from collections import defaultdict

sys.path.insert(0, "/app")
import psycopg2
import requests
from unidecode import unidecode

DB = {
    "host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
    "dbname": os.getenv("DB_NAME","wins_hub"),
    "user": os.getenv("DB_USER","postgres"), "password": os.getenv("DB_PASSWORD",""),
}
SERPER = "https://google.serper.dev/search"
EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+")
EMAIL_BLACKLIST = ("noreply@", "no-reply@", "dpo@", "lgpd@", "ouvidoria@", "imprensa@", "rh@")

def _norm(s): return unidecode((s or "").strip().lower())

def serper(query, num=10):
    key = os.getenv("SERPER_API_KEY","").strip()
    if not key: return []
    try:
        r = requests.post(SERPER, json={"q": query, "num": num},
            headers={"X-API-KEY": key, "Content-Type":"application/json"}, timeout=15)
        return (r.json().get("organic") or []) if r.status_code==200 else []
    except requests.RequestException: return []

def harvest(dominio: str, empresa: str) -> set[str]:
    """Retorna set de emails @dominio encontrados via SERP."""
    queries = [
        f'"@{dominio}" "{empresa}"',
        f'"@{dominio}" contato',
        f'"@{dominio}" filetype:pdf',
    ]
    encontrados = set()
    for q in queries:
        for r in serper(q, 10):
            blob = f"{r.get('title','')} {r.get('snippet','')} {r.get('link','')}"
            for em in EMAIL_RE.findall(blob):
                em_low = em.lower()
                if not em_low.endswith("@" + dominio.lower()):
                    continue
                if any(em_low.startswith(b) for b in EMAIL_BLACKLIST):
                    continue
                encontrados.add(em_low)
    return encontrados

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = psycopg2.connect(**DB); cur = conn.cursor()
    # Empresas OURO/PRATA com decisor (LK) mas sem email + domínio conhecido
    cur.execute("""
        SELECT DISTINCT o.empresa, ed.dominio
        FROM obras o
        JOIN empresa_dominios ed ON regexp_replace(ed.cnpj,'[^0-9]','','g') = regexp_replace(COALESCE(o.cnpj,''),'[^0-9]','','g')
        WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel
          AND COALESCE(o.nivel1_email,'') = ''
          AND COALESCE(o.nivel1_nome,'') <> ''
          AND COALESCE(ed.dominio,'') <> ''
          AND COALESCE(o.cnpj,'') <> ''
        ORDER BY o.empresa
    """)
    targets = cur.fetchall()
    if args.limit: targets = targets[:args.limit]
    print(f"\nHarvest emails: {len(targets)} (empresa, dominio) targets\n")

    if not args.commit:
        for e,d in targets[:10]: print(f"  {e[:40]:40} @ {d}")
        print("\nDRY-RUN. Use --commit pra rodar Serper + UPDATE.")
        return

    total_emails = total_matches = 0
    t0 = time.time()
    for i, (empresa, dominio) in enumerate(targets, 1):
        emails = harvest(dominio, empresa)
        total_emails += len(emails)
        # Cross-match: pra cada obra dessa empresa com decisor sem email, tentar
        # bater first_name no email
        cur.execute("""
            SELECT id::text, nivel1_nome
            FROM obras
            WHERE classificacao_computed IN ('OURO','PRATA') AND visivel
              AND COALESCE(nivel1_email,'') = ''
              AND COALESCE(nivel1_nome,'') <> ''
              AND empresa = %s
        """, (empresa,))
        obras = cur.fetchall()
        match_em_empresa = 0
        for obra_id, nome in obras:
            parts = [p for p in re.split(r"\s+", _norm(nome)) if len(p)>2]
            if len(parts) < 2: continue
            first, last = parts[0], parts[-1]
            best = None
            for em in emails:
                local = em.split("@")[0].lower()
                if first in local and last in local: best = em; break
                if last in local and not best: best = em
            if not best:
                # se só tem 1 email genérico (compras@/contato@), aplica como fallback
                generic = [e for e in emails if any(e.startswith(g) for g in ("fornecedores@","compras@","suprimentos@","comercial@","atendimento@","contato@"))]
                if generic: best = generic[0]
            if best:
                cur.execute("UPDATE obras SET nivel1_email=%s WHERE id=%s AND COALESCE(nivel1_email,'')=''", (best, obra_id))
                if cur.rowcount: match_em_empresa += 1; total_matches += 1
        print(f"[{i:02d}/{len(targets)}] {empresa[:35]:35} | dom={dominio[:22]:22} emails={len(emails)} match={match_em_empresa}")
    conn.commit()
    print(f"\n  Emails encontrados: {total_emails}")
    print(f"  Obras UPDATEd: {total_matches}")
    print(f"  Tempo: {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
