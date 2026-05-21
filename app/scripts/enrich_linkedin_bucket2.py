"""
enrich_linkedin_bucket2.py — Recupera LinkedIn URL de decisores OURO/PRATA via Serper.

Fluxo:
  1. Lista decisores OURO/PRATA visíveis com email mas sem linkedin_url
  2. Dedup por (nome, empresa) — Serper call única por pessoa
  3. Serper query: `site:linkedin.com/in "Nome Decisor" "Empresa"`
  4. Parse primeiro resultado válido (linkedin.com/in/<slug>)
  5. UPDATE decisores_obra.linkedin_url

Dry-run --commit obrigatório pra escrever.
"""
import argparse
import json
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

CANDIDATOS_SQL = """
SELECT
  d.id::text AS dec_id,
  d.nome,
  d.cargo,
  d.email,
  COALESCE(d.hipotese_replicacao,'') AS hipotese,
  o.id::text AS obra_id,
  o.empresa AS obra_empresa
FROM decisores_obra d
JOIN obras o ON o.id = d.obra_id
WHERE o.classificacao_computed IN ('OURO','PRATA')
  AND o.visivel = true
  AND d.excluido_em IS NULL
  AND COALESCE(d.email,'') <> ''
  AND COALESCE(d.linkedin_url,'') = ''
  AND COALESCE(d.nome,'') <> ''
  AND COALESCE(o.empresa,'') <> ''
  -- Exige pelo menos 2 palavras alfabéticas de >=3 chars: filtra "Ari", "Agatha", "Ana Cabral (CEO)" etc
  AND array_length(
        regexp_split_to_array(
          regexp_replace(d.nome, '[^A-Za-zÀ-ú\\s]', '', 'g'),
          '\\s+'
        ), 1
      ) >= 2
  AND d.nome ~ '[A-Za-zÀ-ú]{3,}\\s+[A-Za-zÀ-ú]{3,}'
  -- Exclui placeholders sanitizados/genéricos
  AND d.nome NOT ILIKE 'Contato Comercial%'
  AND d.nome NOT ILIKE 'Equipe %'
  AND d.nome NOT ILIKE 'Equip %'
  AND d.nome NOT ILIKE 'Gerência %'
  AND d.nome NOT ILIKE 'Gerencia %'
  AND d.nome NOT ILIKE 'Departamento%'
  AND d.nome NOT ILIKE 'Setor%'
  AND d.nome NOT ILIKE 'Diretoria%'
  AND d.nome NOT ILIKE 'Direção%'
  AND d.nome NOT ILIKE 'Direcao%'
  AND d.nome NOT ILIKE 'Time %'
  AND d.nome NOT ILIKE 'Coordenação%'
  AND d.nome NOT ILIKE 'Coordenacao%'
  AND d.nome NOT ILIKE 'Procurement Team%'
  AND d.nome NOT ILIKE 'Supply Team%'
ORDER BY d.nome
"""


def _norm(s):
    return unidecode((s or "").strip().lower())


def _extrair_slug_linkedin(url: str) -> str | None:
    """Extrai 'slug' de https://linkedin.com/in/slug ou variantes regionais."""
    if not url:
        return None
    m = re.search(r"linkedin\.com/in/([a-zA-Z0-9\-_%]+)", url, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).split("?")[0].split("#")[0].rstrip("/")


def serper_busca(query: str, num: int = 10) -> list[dict]:
    """POST Serper, retorna lista de results (organic)."""
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        return []
    try:
        r = requests.post(
            SERPER_URL,
            json={"q": query, "num": num},
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return (r.json().get("organic") or [])[:num]
    except requests.RequestException:
        return []


def _nome_no_titulo(nome_dec: str, titulo: str) -> bool:
    """Heurística: primeiro+último nome do decisor aparecem no titulo do LinkedIn."""
    parts = [p for p in re.split(r"\s+", _norm(nome_dec)) if len(p) > 2]
    if len(parts) < 2:
        return False
    primeiro = parts[0]
    ultimo = parts[-1]
    t = _norm(titulo)
    return primeiro in t and ultimo in t


def _limpar_nome(nome: str) -> str:
    """Remove parenteses, barras, slashes e cargos concatenados.
    Ex: 'Ana Cabral (CEO) / Project Manager' → 'Ana Cabral'"""
    # Corta no primeiro ( / | -, mantém só nome próprio antes
    cleaned = re.split(r"[\(/|]", nome, maxsplit=1)[0].strip()
    return cleaned or nome


def buscar_linkedin(nome: str, empresa: str) -> tuple[str | None, str]:
    """Retorna (linkedin_url, motivo). Motivo é log curto."""
    if not nome or not empresa:
        return None, "input_invalido"
    nome_clean = _limpar_nome(nome)
    if len(nome_clean.split()) < 2:
        return None, "nome_curto_pos_clean"
    # Limpa empresa: remove S.A./LTDA/etc do final
    emp_clean = re.sub(r"\b(s\.?a\.?|ltda\.?|s/a|eireli|holding)\b", "",
                       empresa.lower()).strip(" -.,").strip()
    emp_principal = emp_clean.split()[0:3]
    emp_q = " ".join(emp_principal)

    query = f'site:linkedin.com/in/ "{nome_clean}" "{emp_q}"'
    results = serper_busca(query, num=5)
    for r in results:
        url = r.get("link") or ""
        titulo = r.get("title") or ""
        slug = _extrair_slug_linkedin(url)
        if not slug:
            continue
        if _nome_no_titulo(nome_clean, titulo):
            return url, f"match_strict"

    # Fallback: query sem aspas em empresa
    query2 = f'site:linkedin.com/in/ "{nome_clean}" {emp_q}'
    results2 = serper_busca(query2, num=5)
    for r in results2:
        url = r.get("link") or ""
        titulo = r.get("title") or ""
        slug = _extrair_slug_linkedin(url)
        if not slug:
            continue
        if _nome_no_titulo(nome_clean, titulo):
            return url, f"match_loose"

    return None, "no_match"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(CANDIDATOS_SQL)
    rows = cur.fetchall()
    candidatos = [dict(zip([c.name for c in cur.description], r)) for r in rows]

    # Dedup por (nome_norm, empresa_norm)
    por_chave: dict[tuple[str, str], list] = defaultdict(list)
    for c in candidatos:
        key = (_norm(c["nome"]), _norm(c["obra_empresa"]))
        por_chave[key].append(c)
    chaves = list(por_chave.keys())
    if args.limit:
        chaves = chaves[: args.limit]

    print(f"\nBucket 2 — Serper LinkedIn search")
    print(f"  Decisores brutos: {len(candidatos)}")
    print(f"  Chaves (nome+empresa) únicas: {len(por_chave)} → {len(chaves)} processadas")
    print(f"  Modo: {'COMMIT' if args.commit else 'DRY-RUN'}\n")

    if not args.commit:
        print("DRY-RUN não faz call Serper. Use --commit.\n")
        for k in chaves[:5]:
            print(f"  amostra: {por_chave[k][0]['nome']!r} @ {por_chave[k][0]['obra_empresa'][:40]!r}")
        return

    total_found = 0
    total_no_match = 0
    total_skip_replicado = 0
    total_skip_input = 0
    updates: list[tuple[str, str]] = []
    t0 = time.time()

    for i, key in enumerate(chaves, 1):
        decisores_dessa_chave = por_chave[key]
        if any(d["hipotese"] == "REPLICADO_PROVAVEL_FALSO_POSITIVO" for d in decisores_dessa_chave):
            total_skip_replicado += len(decisores_dessa_chave)
            continue
        rep = decisores_dessa_chave[0]
        if not rep["nome"] or not rep["obra_empresa"]:
            total_skip_input += 1
            continue
        url, motivo = buscar_linkedin(rep["nome"], rep["obra_empresa"])
        if not url:
            total_no_match += 1
            print(f"[{i:02d}/{len(chaves)}] -- {rep['nome'][:30]:30} @ {rep['obra_empresa'][:30]:30} → no_match")
            continue
        total_found += 1
        for dec in decisores_dessa_chave:
            updates.append((dec["dec_id"], url))
        print(f"[{i:02d}/{len(chaves)}] OK {rep['nome'][:30]:30} → {url[:60]} ({motivo}, {len(decisores_dessa_chave)} obras)")

    if updates:
        for dec_id, url in updates:
            cur.execute(
                """
                UPDATE decisores_obra
                   SET linkedin_url = %s,
                       confianca_match_componentes =
                         COALESCE(confianca_match_componentes,'{}'::jsonb)
                         || jsonb_build_object('linkedin_via_serper', jsonb_build_object('ts', NOW()::text))
                 WHERE id = %s
                   AND COALESCE(linkedin_url,'') = ''
                """,
                (url, dec_id),
            )
        conn.commit()

    print(f"\n{'='*60}")
    print(f"  Pessoas únicas com LinkedIn encontrado: {total_found}")
    print(f"  Decisores UPDATEd: {len(updates)}")
    print(f"  No match: {total_no_match}")
    print(f"  Skip REPLICADO: {total_skip_replicado}")
    print(f"  Skip input vazio: {total_skip_input}")
    print(f"  Calls Serper: ~{(total_found + total_no_match) * 2}")
    print(f"  Tempo: {time.time()-t0:.1f}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
