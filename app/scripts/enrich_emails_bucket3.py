"""
enrich_emails_bucket3.py — Recupera email de decisores OURO/PRATA via Hunter.

Fluxo:
  1. Lista decisores OURO/PRATA visíveis com linkedin mas sem email
  2. Agrupa por CNPJ-raiz; busca domínio em empresa_dominios
  3. Para cada domínio: Hunter domain-search (ou cache decisores_cache)
  4. Cross-match nome decisor ↔ Hunter (first+last fuzzy) → UPDATE email
  5. Skip se decisor já tem hipotese_replicacao=REPLICADO (não preencher FP)

Dry-run por padrão. --commit aplica.
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
import psycopg2.extras
import requests
from unidecode import unidecode


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Cache TTL — não reusa Hunter cache mais velho que isso (dias)
HUNTER_CACHE_TTL_DIAS = 14   # cache curto pra forçar refresh nos antigos limitados
HUNTER_FINDER_URL = "https://api.hunter.io/v2/email-finder"


def email_finder_por_nome(dominio: str, first_name: str, last_name: str) -> dict:
    """Hunter email-finder direcionado: 1 crédito por nome+domínio.
    Retorna {email, confidence, position, score, verification_status} ou {erro}."""
    api_key = os.getenv("HUNTER_API_KEY", "").strip()
    if not api_key:
        return {"erro": "HUNTER_API_KEY ausente"}
    if not first_name and not last_name:
        return {"erro": "nome_vazio"}
    params = {
        "domain": dominio,
        "first_name": first_name,
        "last_name": last_name,
        "api_key": api_key,
    }
    try:
        r = requests.get(HUNTER_FINDER_URL, params=params, timeout=30)
    except requests.exceptions.RequestException as e:
        return {"erro": f"rede:{e}"}
    if r.status_code == 429:
        return {"erro": "rate_limit"}
    if r.status_code == 404:
        return {"erro": "not_found"}
    if r.status_code != 200:
        return {"erro": f"http_{r.status_code}"}
    payload = r.json()
    data = payload.get("data") or {}
    return {
        "email": data.get("email"),
        "score": data.get("score"),                # 0-100
        "verification": (data.get("verification") or {}).get("status"),
        "position": data.get("position"),
        "company": data.get("company"),
    }


CANDIDATOS_SQL = """
WITH alvo AS (
  SELECT DISTINCT LEFT(regexp_replace(COALESCE(o.cnpj,''), '[^0-9]', '', 'g'), 8) AS cnpj_raiz
  FROM obras o
  JOIN decisores_obra d ON d.obra_id = o.id AND d.excluido_em IS NULL
  WHERE o.classificacao_computed IN ('OURO','PRATA')
    AND o.visivel = true
    AND COALESCE(d.linkedin_url,'') <> ''
    AND COALESCE(d.email,'') = ''
)
SELECT
  d.id::text AS dec_id,
  d.nome,
  d.cargo,
  d.linkedin_url,
  COALESCE(d.hipotese_replicacao, '') AS hipotese,
  o.id::text AS obra_id,
  o.empresa AS obra_empresa,
  LEFT(regexp_replace(COALESCE(o.cnpj,''),'[^0-9]','','g'),8) AS cnpj_raiz,
  ed.dominio,
  ed.empresa_nome AS dominio_empresa
FROM decisores_obra d
JOIN obras o ON o.id = d.obra_id
JOIN alvo a ON LEFT(regexp_replace(COALESCE(o.cnpj,''),'[^0-9]','','g'),8) = a.cnpj_raiz
LEFT JOIN empresa_dominios ed
  ON LEFT(regexp_replace(ed.cnpj,'[^0-9]','','g'),8) = a.cnpj_raiz
WHERE o.classificacao_computed IN ('OURO','PRATA')
  AND o.visivel = true
  AND d.excluido_em IS NULL
  AND COALESCE(d.linkedin_url,'') <> ''
  AND COALESCE(d.email,'') = ''
  AND COALESCE(ed.dominio,'') <> ''
ORDER BY a.cnpj_raiz
"""

CACHE_HUNTER_SQL = """
SELECT emails, hunter_atualizado_em
FROM decisores_cache
WHERE dominio = %s
  AND emails IS NOT NULL
  AND jsonb_array_length(emails) >= 50  -- ignora caches antigos limitados a 10
  AND hunter_atualizado_em > NOW() - INTERVAL '%s days'
ORDER BY hunter_atualizado_em DESC
LIMIT 1
"""


def _norm(s):
    return unidecode((s or "").strip().lower())


def _split_first_last(nome_completo: str):
    """Retorna (primeiro_nome, ultimo_nome) normalizados."""
    parts = [p for p in re.split(r"\s+", _norm(nome_completo)) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def _match_score(dec_nome: str, hunter_first: str, hunter_last: str) -> int:
    """0-100: 100=match completo, 60=match parcial (só primeiro OU só último), 0=miss."""
    df, dl = _split_first_last(dec_nome)
    hf = _norm(hunter_first)
    hl = _norm(hunter_last)
    if not df or not hf:
        return 0
    if df == hf and dl and hl and dl == hl:
        return 100
    if df == hf and (not dl or not hl):
        return 80
    if dl and hl and dl == hl and df != hf:
        return 50
    if df == hf:
        return 60
    return 0


# Cache em memória só pra esta run (constraint UNIQUE de decisores_cache é por cnpj)
_DOMINIO_HUNTER_CACHE: dict[str, list] = {}


def hunter_emails_por_dominio(cur, dominio: str, dry_run: bool):
    """Retorna lista de emails do Hunter para o domínio. Cache em memória."""
    if dominio in _DOMINIO_HUNTER_CACHE:
        emails = _DOMINIO_HUNTER_CACHE[dominio]
        return emails, f"mem_cache({len(emails)})"
    if dry_run:
        return [], "dry_run_skip"
    res = buscar_emails_dominio_bulk(dominio)
    if "erro" in res:
        return [], f"hunter_err:{res['erro'][:60]}"
    emails = res.get("emails") or []
    _DOMINIO_HUNTER_CACHE[dominio] = emails
    return emails, f"hunter_fresh({len(emails)})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="máx decisores a processar (0 = todos)")
    ap.add_argument("--min-finder-score", type=int, default=50,
                    help="Hunter finder score mínimo (0-100, default 50)")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(CANDIDATOS_SQL)
    rows = cur.fetchall()
    candidatos = [dict(zip([c.name for c in cur.description], r)) for r in rows]

    # Dedup por (nome_norm, dominio) — economiza créditos.
    # Cada chave única → 1 call Hunter. UPDATE replica pra todos os dec_ids dessa chave.
    por_chave: dict[tuple[str, str], list] = defaultdict(list)
    for c in candidatos:
        key = (_norm(c["nome"]), c["dominio"])
        por_chave[key].append(c)
    chaves = list(por_chave.keys())
    if args.limit:
        chaves = chaves[: args.limit]
    print(f"  Decisores brutos: {len(candidatos)}")
    print(f"  Chaves (nome+dominio) únicas: {len(por_chave)} → {len(chaves)} processadas")

    print(f"\nBucket 3 — email-finder direcionado")
    print(f"  Modo: {'COMMIT' if args.commit else 'DRY-RUN'}")
    print(f"  Custo estimado: {len(chaves)} créditos Hunter\n")

    if not args.commit:
        print("DRY-RUN não faz call Hunter. Use --commit.")
        return

    total_found = 0
    total_skip_replicado = 0
    total_not_found = 0
    total_erro = 0
    total_dec_updateados = 0
    updates: list[tuple[str, str, int, str]] = []  # (dec_id, email, score, status)
    t0 = time.time()

    for i, key in enumerate(chaves, 1):
        decisores_dessa_chave = por_chave[key]
        # Usa o primeiro como representante. Se algum tem REPLICADO, skip TODOS.
        if any(d["hipotese"] == "REPLICADO_PROVAVEL_FALSO_POSITIVO" for d in decisores_dessa_chave):
            total_skip_replicado += len(decisores_dessa_chave)
            continue
        rep = decisores_dessa_chave[0]
        first, last = _split_first_last(rep["nome"])
        if not first or not last:
            print(f"[{i:02d}/{len(chaves)}] SKIP nome incompleto: {rep['nome']!r}")
            continue
        res = email_finder_por_nome(rep["dominio"], first, last)
        if "erro" in res:
            if res["erro"] == "not_found":
                total_not_found += 1
                print(f"[{i:02d}/{len(chaves)}] -- not_found {rep['nome'][:30]} @ {rep['dominio']}")
            else:
                total_erro += 1
                print(f"[{i:02d}/{len(chaves)}] ER {res['erro'][:30]} {rep['nome'][:30]}")
            continue
        email = res.get("email")
        score = res.get("score") or 0
        verif = res.get("verification") or "?"
        if not email:
            total_not_found += 1
            print(f"[{i:02d}/{len(chaves)}] -- vazio {rep['nome'][:30]} @ {rep['dominio']}")
            continue
        if score < args.min_finder_score:
            print(f"[{i:02d}/{len(chaves)}] LOW {rep['nome'][:30]:30} → {email[:35]:35} score={score} verif={verif}")
            continue
        total_found += 1
        for dec in decisores_dessa_chave:
            updates.append((dec["dec_id"], email, score, verif))
        total_dec_updateados += len(decisores_dessa_chave)
        print(f"[{i:02d}/{len(chaves)}] OK  {rep['nome'][:30]:30} → {email[:35]:35} score={score} verif={verif} (replicado em {len(decisores_dessa_chave)} obras)")

    if updates:
        for dec_id, email, score, verif in updates:
            cur.execute(
                """
                UPDATE decisores_obra
                   SET email = %s,
                       confianca_match_componentes =
                         COALESCE(confianca_match_componentes, '{}'::jsonb)
                         || jsonb_build_object('email_via_hunter', jsonb_build_object(
                              'finder_score', %s::int,
                              'verification', %s::text,
                              'ts', NOW()::text
                         ))
                 WHERE id = %s
                   AND COALESCE(email,'') = ''
                """,
                (email, score, verif, dec_id),
            )
        conn.commit()

    print(f"\n{'='*60}")
    print(f"  Pessoas únicas com email encontrado (score>={args.min_finder_score}): {total_found}")
    print(f"  Decisores UPDATEd em obras: {total_dec_updateados}")
    print(f"  Not found / vazio:   {total_not_found}")
    print(f"  Erros:               {total_erro}")
    print(f"  Skip REPLICADO (dec): {total_skip_replicado}")
    print(f"  Créditos consumidos: ~{len(chaves)}")
    print(f"  Tempo: {time.time()-t0:.1f}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
