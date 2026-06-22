#!/usr/bin/env python3
"""
verificar_emails_hunter.py — Verifica entregabilidade dos emails de decisores via
Hunter email-verifier. Idempotente, respeita a quota de VERIFICATIONS da conta
(para ao esgotar), prioriza OURO > PRATA e fontes mais arriscadas (inferencia/
replicacao/padrao) primeiro. Pula emails já-Hunter e já-verificados.

Grava em decisores_obra: email_status (valid|invalid|accept_all|webmail|
disposable|unknown), email_verify_result (deliverable|undeliverable|risky|unknown),
email_verificado_em.

Uso:
  # dry-run (NÃO gasta quota — só lista candidatos e quanto rodaria):
  docker exec wins_hub-api-1 python /app/scripts/verificar_emails_hunter.py --limit 258

  # commit (gasta quota, para sozinho ao esgotar):
  docker exec wins_hub-api-1 python /app/scripts/verificar_emails_hunter.py --commit --limit 258

Marker em observacoes não é tocado; status fica nas colunas dedicadas.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import psycopg2
import psycopg2.extras

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}
UA = "Mozilla/5.0 (X11; Linux x86_64) wins-verify"
API = os.getenv("HUNTER_API_KEY") or os.getenv("HUNTER_KEY")

# ESTRATÉGIA ALAVANCADA: dedup por EMAIL ÚNICO, prioriza o que cobre mais obras
# (OURO primeiro), verifica 1x e PROPAGA o veredito a todas as linhas com o mesmo
# email. 1 crédito valida N obras.
CANDIDATOS_SQL = """
SELECT lower(d.email) AS email,
       count(DISTINCT d.obra_id) AS obras
FROM decisores_obra d
JOIN obras o ON o.id = d.obra_id
WHERE d.excluido_em IS NULL
  AND COALESCE(d.email,'') <> ''
  AND d.email_status IS NULL
GROUP BY lower(d.email)
ORDER BY bool_or(o.classificacao_computed='OURO') DESC,
         count(DISTINCT d.obra_id) DESC
LIMIT %s
"""


def hunter_account_verifications():
    req = urllib.request.Request(
        "https://api.hunter.io/v2/account",
        headers={"User-Agent": UA, "Authorization": f"Bearer {API}"},
    )
    d = json.loads(urllib.request.urlopen(req, timeout=20).read())["data"]
    v = d.get("requests", {}).get("verifications", {})
    return int(v.get("available", 0)) - int(v.get("used", 0))


def hunter_verify(email):
    qs = urllib.parse.urlencode({"email": email})
    req = urllib.request.Request(
        f"https://api.hunter.io/v2/email-verifier?{qs}",
        headers={"User-Agent": UA, "Authorization": f"Bearer {API}"},
    )
    for tent in range(3):
        try:
            body = json.loads(urllib.request.urlopen(req, timeout=30).read())
            d = body.get("data")
            if not d:  # resposta de erro sem 'data' (domínio inválido etc.)
                return "unknown", "unknown"
            return d.get("status"), d.get("result")
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limit
                time.sleep(2 + tent * 2)
                continue
            if e.code in (222, 400):  # invalid email format etc → trata como invalid
                return "invalid", "undeliverable"
            raise
        except Exception:
            if tent == 2:
                raise
            time.sleep(1 + tent)
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=258)
    args = ap.parse_args()

    if not API:
        print("ERRO: HUNTER_API_KEY ausente no env"); sys.exit(1)

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    quota = hunter_account_verifications()
    print(f"Quota verifications restante: {quota}")
    # nunca gastar mais que a quota (margem de 2)
    teto = min(args.limit, max(0, quota - 2)) if args.commit else args.limit
    cur.execute(CANDIDATOS_SQL, (teto,))
    cands = cur.fetchall()
    obras_alvo = sum(c["obras"] for c in cands)
    print(f"Emails únicos a verificar: {len(cands)} (cobrem {obras_alvo} obras) | teto={teto} | {'COMMIT' if args.commit else 'DRY-RUN'}")
    if not args.commit:
        print("  DRY-RUN: nenhuma chamada Hunter feita, zero quota gasta.")
        print("  Top 10 por alcance:", [(c["email"], c["obras"]) for c in cands[:10]])
        return

    counts = {}; feitos = 0; obras_cobertas = 0
    for i, c in enumerate(cands, 1):
        try:
            status, result = hunter_verify(c["email"])
        except Exception as e:
            print(f"  [{i}] ERRO {c['email']}: {e!r}"); continue
        if status is None:
            continue
        # PROPAGA o veredito a TODAS as linhas com o mesmo email (commit por email +
        # rollback no erro = resiliente ao deadlock do trigger de recompute)
        try:
            cur.execute(
                "UPDATE decisores_obra SET email_status=%s, email_verify_result=%s, "
                "email_verificado_em=now() WHERE excluido_em IS NULL AND lower(email)=%s",
                (status, result, c["email"]),
            )
            conn.commit()
        except Exception as e:
            conn.rollback(); print(f"  [{i}] UPDATE falhou {c['email']}: {e!r}"); continue
        counts[status] = counts.get(status, 0) + 1
        feitos += 1; obras_cobertas += c["obras"]
        if feitos % 25 == 0:
            print(f"  ...{feitos} emails | {obras_cobertas} obras cobertas | {counts}", flush=True)
        time.sleep(0.3)
    print(f"\nFIM. Emails verificados: {feitos} | obras cobertas: {obras_cobertas}")
    print(f"Distribuição: {counts}")


if __name__ == "__main__":
    main()
