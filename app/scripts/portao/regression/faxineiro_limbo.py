#!/usr/bin/env python3
"""faxineiro_limbo.py — Fase 4. Auto-REJEITA obras NOTICIA/NULL em limbo:
sem CNPJ válido, sem decisor, sem classificação, paradas há 7+ dias (não enriqueceram).
NÃO deleta — soft-reject (visivel=false + motivo_invisivel='limbo_expirado'), preservando
atividade comercial. Ver feedback_motivo_invisivel_rule.

Uso:  docker exec wins_hub-api-1 python /app/scripts/portao/regression/faxineiro_limbo.py          # DRYRUN
      docker exec wins_hub-api-1 python /app/scripts/portao/regression/faxineiro_limbo.py --commit  # aplica
"""
import os, sys
import psycopg2, psycopg2.extras

DIAS = 7
MARKER = "auto:faxineiro_limbo"
DB = dict(host=os.getenv("DB_HOST", "db"), port=int(os.getenv("DB_PORT", "5432")),
          dbname=os.getenv("DB_NAME", "wins_hub"),
          user=os.getenv("DB_USER", "postgres"), password=os.getenv("DB_PASSWORD", ""))

ALVO = f"""
  COALESCE(fonte_tipo,'') = 'NOTICIA'
  AND classificacao_computed IS NULL
  AND (cnpj IS NULL OR cnpj = '' OR cnpj_status = 'invalid_dv')
  AND criado_em < now() - interval '{DIAS} days'
  AND visivel IS NOT FALSE
  AND NOT EXISTS (SELECT 1 FROM decisores_obra d WHERE d.obra_id = obras.id AND d.excluido_em IS NULL)
"""


def main():
    commit = "--commit" in sys.argv
    conn = psycopg2.connect(**DB); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(f"SELECT count(*) n FROM obras WHERE {ALVO}")
    total = cur.fetchone()["n"]
    cur.execute(f"""SELECT COALESCE(fonte,'(null)') fonte, count(*) n,
                           min((now()::date - criado_em::date)) dias_min,
                           max((now()::date - criado_em::date)) dias_max
                    FROM obras WHERE {ALVO} GROUP BY 1 ORDER BY 2 DESC""")
    porfonte = cur.fetchall()
    cur.execute(f"""SELECT left(nome,55) nome, COALESCE(fonte,'') fonte,
                           (now()::date - criado_em::date) dias
                    FROM obras WHERE {ALVO} ORDER BY criado_em LIMIT 12""")
    amostra = cur.fetchall()

    print(f"=== FAXINEIRO LIMBO — {'COMMIT' if commit else 'DRYRUN'} | alvo: NOTICIA/NULL sem CNPJ válido, sem decisor, 7+ dias ===")
    print(f"Total a rejeitar: {total}\n")
    print(f"{'fonte':<32} {'qtd':>4} {'dias_min':>8} {'dias_max':>8}")
    for r in porfonte:
        print(f"{r['fonte']:<32} {r['n']:>4} {r['dias_min']:>8} {r['dias_max']:>8}")
    print("\nAmostra (12 mais antigas):")
    for r in amostra:
        print(f"  [{r['dias']:>3}d] {r['fonte']:<26} {r['nome']}")

    if not commit:
        print("\nDRYRUN: nada alterado. Rode com --commit para aplicar.")
        return

    TETO = 150  # teto de segurança: pico anômalo (bug a montante) não oculta em massa sem revisão
    if total > TETO and "--force" not in sys.argv:
        print(f"\nABORTADO: {total} candidatas > teto {TETO}. Possível bug a montante — "
              f"investigue. Rode com --force para confirmar (nada alterado).")
        return

    cur.execute(f"""
        UPDATE obras SET
            classificacao_computed = 'REJEITADO',
            visivel = false,
            motivo_invisivel = 'limbo_expirado',
            observacoes_validacao = COALESCE(observacoes_validacao,'') || ' | {MARKER}'
        WHERE {ALVO}
    """)
    n = cur.rowcount
    conn.commit()
    print(f"\nCOMMIT: {n} obras rejeitadas (soft, sem DELETE — atividade comercial preservada).")


if __name__ == "__main__":
    main()
