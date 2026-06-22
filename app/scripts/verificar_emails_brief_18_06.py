#!/usr/bin/env python3
"""Verifica via Hunter SÓ os emails inferidos do brief 18/06 (registrado_por='brief_18_06_2026').
Reusa hunter_verify/quota do verificar_emails_hunter.py. Dedup por email, propaga veredito.
  docker exec wins_hub-api-1 python /app/scripts/verificar_emails_brief_18_06.py          # dry-run
  docker exec wins_hub-api-1 python /app/scripts/verificar_emails_brief_18_06.py --commit  # gasta quota
"""
import argparse, sys, time
import psycopg2, psycopg2.extras
from verificar_emails_hunter import (
    DB_CONFIG, API, hunter_account_verifications, hunter_verify,
)

# Prioridade: decisores de compra/diretor-geral primeiro (alvos reais de outreach),
# depois maior cobertura de obras. Quota Hunter é escassa (reset 4.000 em 11/07).
PRIORIDADE = [
    'yuri.barbosa@gestamp.com',   # Diretor Compras Mercosul (decisor prioritário)
    'cesar.cavalier@fs.agr.br',   # Especialista Compras CAPEX FS
    'joao.daniel@tecumseh.com',   # Diretor Geral BR (cobre 2 obras Tecumseh)
    'ricardo.maciel@tecumseh.com',  # CEO Global (cobre 2 obras Tecumseh)
]
SQL = """
SELECT lower(d.email) AS email, count(*) AS linhas, count(DISTINCT d.obra_id) AS obras,
       COALESCE(array_position(%s::text[], lower(d.email)), 99) AS prio
FROM decisores_obra d
WHERE d.excluido_em IS NULL
  AND d.registrado_por = 'brief_18_06_2026'
  AND COALESCE(d.email,'') <> ''
  AND d.email_status IS NULL
GROUP BY lower(d.email)
ORDER BY prio ASC, obras DESC
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    if not API:
        print("ERRO: HUNTER_API_KEY ausente"); sys.exit(1)

    conn = psycopg2.connect(**DB_CONFIG); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(SQL, (PRIORIDADE,))
    cands = cur.fetchall()
    quota = hunter_account_verifications()
    # nunca gastar mais que a quota disponível; processa os top-prioridade que couberem
    cap = min(len(cands), max(0, quota)) if args.commit else len(cands)
    alvo, defer = cands[:cap], cands[cap:]
    print(f"Quota verifications restante: {quota}")
    print(f"Emails únicos do brief: {len(cands)} | verificando agora: {cap} | "
          f"{'COMMIT' if args.commit else 'DRY-RUN'}")
    for c in alvo:
        print(f"  VERIFICA  {c['email']}  ({c['obras']} obras)")
    for c in defer:
        print(f"  DEFER     {c['email']}  (quota insuficiente — pós-reset 11/07)")
    if not args.commit:
        print("DRY-RUN: zero quota gasta."); return

    counts = {}
    for i, c in enumerate(alvo, 1):
        try:
            status, result = hunter_verify(c["email"])
        except Exception as e:
            print(f"  [{i}] ERRO {c['email']}: {e!r}"); continue
        if status is None:
            continue
        try:
            cur.execute(
                "UPDATE decisores_obra SET email_status=%s, email_verify_result=%s, "
                "email_verificado_em=now() WHERE excluido_em IS NULL AND lower(email)=%s",
                (status, result, c["email"]))
            conn.commit()
        except Exception as e:
            conn.rollback(); print(f"  [{i}] UPDATE falhou {c['email']}: {e!r}"); continue
        counts[status] = counts.get(status, 0) + 1
        print(f"  [{i}] {c['email']} -> {status} / {result}", flush=True)
        time.sleep(0.3)
    print(f"\nFIM. Distribuição: {counts}")


if __name__ == "__main__":
    main()
