"""
enrich_lk_nivel1.py — Serper LinkedIn search direto em obras.nivel1_*.

Diferença do bucket2 anterior: opera em obras.nivel1_* (visível ao front-end),
não em decisores_obra. Garante que LK descoberto aparece na vitrine.
"""
import argparse
import os
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, "/app")
import psycopg2
import requests
from unidecode import unidecode

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

SERPER_URL = "https://google.serper.dev/search"

SQL = """
SELECT
  o.id::text AS obra_id,
  o.nivel1_nome AS nome,
  o.empresa AS obra_empresa
FROM obras o
WHERE o.classificacao_computed IN ('OURO','PRATA')
  AND o.visivel
  AND COALESCE(o.nivel1_email,'') <> ''
  AND COALESCE(o.nivel1_linkedin,'') = ''
  AND COALESCE(o.nivel1_nome,'') <> ''
  AND COALESCE(o.empresa,'') <> ''
  AND array_length(regexp_split_to_array(regexp_replace(o.nivel1_nome, '[^A-Za-zÀ-ú\\s]','','g'), '\\s+'), 1) >= 2
  AND o.nivel1_nome ~ '[A-Za-zÀ-ú]{3,}\\s+[A-Za-zÀ-ú]{3,}'
  AND o.nivel1_nome NOT ILIKE 'Contato Comercial%'
  AND o.nivel1_nome NOT ILIKE 'Equipe %' AND o.nivel1_nome NOT ILIKE 'Equip %'
  AND o.nivel1_nome NOT ILIKE 'Gerência %' AND o.nivel1_nome NOT ILIKE 'Gerencia %'
  AND o.nivel1_nome NOT ILIKE 'Departamento%' AND o.nivel1_nome NOT ILIKE 'Setor%'
  AND o.nivel1_nome NOT ILIKE 'Diretoria%' AND o.nivel1_nome NOT ILIKE 'Direção%'
  AND o.nivel1_nome NOT ILIKE 'Direcao%' AND o.nivel1_nome NOT ILIKE 'Coordenação%'
  AND o.nivel1_nome NOT ILIKE 'Coordenacao%' AND o.nivel1_nome NOT ILIKE 'Time %'
  AND o.nivel1_nome NOT ILIKE 'Site Manager%' AND o.nivel1_nome NOT ILIKE 'Project Manager%'
  AND o.nivel1_nome NOT ILIKE 'Dir. %'
  AND o.nivel1_nome NOT ILIKE 'Contato %'
ORDER BY o.empresa
"""


def _norm(s): return unidecode((s or "").strip().lower())

def _limpar_nome(nome):
    return re.split(r"[\(/|]", nome, maxsplit=1)[0].strip() or nome

def _extrair_slug(url):
    m = re.search(r"linkedin\.com/in/([a-zA-Z0-9\-_%]+)", url or "", re.IGNORECASE)
    return m.group(1).split("?")[0].rstrip("/") if m else None

def _nome_no_titulo(nome, titulo):
    parts = [p for p in re.split(r"\s+", _norm(nome)) if len(p) > 2]
    if len(parts) < 2:
        return False
    return parts[0] in _norm(titulo) and parts[-1] in _norm(titulo)


def serper(query, num=5):
    key = os.getenv("SERPER_API_KEY","").strip()
    if not key:
        return []
    try:
        r = requests.post(SERPER_URL,
            json={"q": query, "num": num},
            headers={"X-API-KEY": key, "Content-Type":"application/json"},
            timeout=15)
        if r.status_code != 200:
            return []
        return (r.json().get("organic") or [])[:num]
    except requests.RequestException:
        return []


def buscar(nome, empresa):
    nome_c = _limpar_nome(nome)
    if len(nome_c.split()) < 2:
        return None, "nome_curto"
    emp_clean = re.sub(r"\b(s\.?a\.?|ltda\.?|s/a|eireli|holding)\b","", empresa.lower()).strip(" -.,").strip()
    emp_q = " ".join(emp_clean.split()[:3])
    for q in [f'site:linkedin.com/in/ "{nome_c}" "{emp_q}"',
              f'site:linkedin.com/in/ "{nome_c}" {emp_q}']:
        for r in serper(q, 5):
            url = r.get("link") or ""
            if not _extrair_slug(url):
                continue
            if _nome_no_titulo(nome_c, r.get("title") or ""):
                return url, "match"
    return None, "no_match"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(SQL)
    rows = cur.fetchall()
    candidatos = [{"obra_id": r[0], "nome": r[1], "empresa": r[2]} for r in rows]

    # Dedup por (nome, empresa)
    por_chave = defaultdict(list)
    for c in candidatos:
        key = (_norm(c["nome"]), _norm(c["empresa"]))
        por_chave[key].append(c)
    chaves = list(por_chave.keys())
    if args.limit: chaves = chaves[: args.limit]

    print(f"\nObras OURO/PRATA com email mas sem LK em nivel1_*: {len(candidatos)}")
    print(f"Chaves únicas: {len(por_chave)} → {len(chaves)} processadas")
    print(f"Modo: {'COMMIT' if args.commit else 'DRY-RUN'}\n")

    if not args.commit:
        for k in chaves[:10]: print(f"  amostra: {por_chave[k][0]['nome'][:30]} @ {por_chave[k][0]['empresa'][:40]}")
        return

    total_found = total_no_match = 0
    updates = []
    t0 = time.time()
    for i, key in enumerate(chaves, 1):
        rep = por_chave[key][0]
        url, motivo = buscar(rep["nome"], rep["empresa"])
        if not url:
            total_no_match += 1
            print(f"[{i:02d}/{len(chaves)}] -- {rep['nome'][:30]:30} @ {rep['empresa'][:30]}")
            continue
        total_found += 1
        for c in por_chave[key]:
            updates.append((c["obra_id"], url))
        print(f"[{i:02d}/{len(chaves)}] OK {rep['nome'][:30]:30} → {url[:55]} ({len(por_chave[key])} obras)")

    if updates:
        for obra_id, url in updates:
            cur.execute(
                "UPDATE obras SET nivel1_linkedin=%s WHERE id=%s AND COALESCE(nivel1_linkedin,'')=''",
                (url, obra_id))
        conn.commit()

    print(f"\n{'='*60}")
    print(f"  LK encontrado: {total_found}")
    print(f"  Obras UPDATEd: {len(updates)}")
    print(f"  No match: {total_no_match}")
    print(f"  Tempo: {time.time()-t0:.1f}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
