"""
Sincroniza decisores de `decisores_obra` (Anderson CSV) para `obras.nivel1_*`
quando o decisor candidato traz qualidade-ouro maior que o nivel1_* atual.

Regra (c) — melhor qualidade ganha:
  - Se obras.nivel1_* já é OURO completo (nome + cargo decisor + email|linkedin)
    → mantém intocado.
  - Senão, se há decisor candidato em decisores_obra que satisfaça:
      tipo_cargo IS NOT NULL AND tipo_cargo != ''
      AND nome != ''
      AND (email != '' OR linkedin_url != '')
      AND cargo_decisor_keyword(cargo)  -- duplo check com filtro canônico
    → promove para nivel1_*.

Tie-breaker entre múltiplos candidatos: email > linkedin > registrado_em DESC.

Inclui também o fix do typo #4: 'Engenahria Civil' → 'Engenharia Civil'.

Por padrão DRY-RUN (rollback). Para persistir: passar --commit.

Uso:
    docker exec wins_hub-api-1 python /app/scripts/sincronizar_decisores_ouro.py
    docker exec wins_hub-api-1 python /app/scripts/sincronizar_decisores_ouro.py --commit
"""
import sys
sys.path.insert(0, "/app")

import argparse
import psycopg2
from psycopg2.extras import RealDictCursor

from services.matchmaking import DB_CONFIG


SQL_OURO_FILTER = """
    nivel1_nome IS NOT NULL AND nivel1_nome != ''
    AND (COALESCE(nivel1_email,'') != '' OR COALESCE(nivel1_linkedin,'') != '')
    AND cargo_decisor_keyword(nivel1_cargo)
    AND COALESCE(fonte_tipo,'OFICIAL') != 'NOTICIA'
    AND (visivel IS NULL OR visivel = TRUE)
"""

SQL_COUNT_OURO = f"SELECT COUNT(*) FROM obras WHERE {SQL_OURO_FILTER}"

SQL_FIX_TYPO = """
    UPDATE obras
    SET nivel1_cargo = 'Engenharia Civil'
    WHERE nivel1_cargo = 'Engenahria Civil'
"""

# Sincronização: substitui nivel1_* somente se atual NÃO for ouro completo
# e candidato traz qualidade ouro
SQL_SYNC = """
    WITH candidato AS (
        SELECT DISTINCT ON (d.obra_id)
            d.obra_id,
            d.nome      AS novo_nome,
            d.cargo     AS novo_cargo,
            d.email     AS novo_email,
            d.linkedin_url AS novo_linkedin
        FROM decisores_obra d
        WHERE d.excluido_em IS NULL
          AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo != ''
          AND COALESCE(d.nome,'') != ''
          AND (COALESCE(d.email,'') != '' OR COALESCE(d.linkedin_url,'') != '')
          AND cargo_decisor_keyword(d.cargo)
        ORDER BY d.obra_id,
            -- email é preferido sobre linkedin
            (CASE WHEN COALESCE(d.email,'') != '' THEN 0 ELSE 1 END),
            -- tie-breaker: cadastro mais recente
            d.registrado_em DESC NULLS LAST
    )
    UPDATE obras o
       SET nivel1_nome     = c.novo_nome,
           nivel1_cargo    = c.novo_cargo,
           nivel1_email    = NULLIF(c.novo_email, ''),
           nivel1_linkedin = NULLIF(c.novo_linkedin, '')
      FROM candidato c
     WHERE c.obra_id = o.id
       -- Regra (c): só atualiza se nivel1_* atual NÃO for ouro completo
       AND NOT (
           o.nivel1_nome IS NOT NULL AND o.nivel1_nome != ''
           AND (COALESCE(o.nivel1_email,'') != '' OR COALESCE(o.nivel1_linkedin,'') != '')
           AND cargo_decisor_keyword(o.nivel1_cargo)
       )
"""

SQL_AMOSTRA_DEPOIS = f"""
    SELECT LEFT(o.empresa,30) AS empresa,
           LEFT(o.nome,40) AS obra,
           o.uf,
           LEFT(o.nivel1_nome,28) AS decisor,
           LEFT(o.nivel1_cargo,30) AS cargo,
           CASE WHEN COALESCE(o.nivel1_email,'')!='' THEN '✉' ELSE '' END
        || CASE WHEN COALESCE(o.nivel1_linkedin,'')!='' THEN ' in' ELSE '' END AS contato
    FROM obras o
    WHERE {SQL_OURO_FILTER}
    ORDER BY random()
    LIMIT 10
"""


def main(commit: bool):
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(SQL_COUNT_OURO)
            ouro_antes = cur.fetchone()["count"]

            cur.execute(SQL_FIX_TYPO)
            fixou_typo = cur.rowcount

            cur.execute(SQL_SYNC)
            sincronizou = cur.rowcount

            cur.execute(SQL_COUNT_OURO)
            ouro_depois = cur.fetchone()["count"]

            print()
            print("=" * 70)
            print(f"  Modo:         {'COMMIT' if commit else 'DRY-RUN (rollback no final)'}")
            print(f"  Typo fixado:  {fixou_typo} obras ('Engenahria Civil' → 'Engenharia Civil')")
            print(f"  Sincronizado: {sincronizou} obras (decisores_obra → nivel1_*)")
            print("-" * 70)
            print(f"  Obras-ouro ANTES:  {ouro_antes}")
            print(f"  Obras-ouro DEPOIS: {ouro_depois}")
            print(f"  Diff:              +{ouro_depois - ouro_antes}")
            print("=" * 70)

            print()
            print("Amostra aleatória (10 obras-ouro depois):")
            cur.execute(SQL_AMOSTRA_DEPOIS)
            print(f"  {'EMPRESA':30} {'UF':3} {'DECISOR':28} {'CARGO':30} CONTATO")
            for r in cur.fetchall():
                print(f"  {r['empresa']:30} {r['uf']:3} "
                      f"{(r['decisor'] or ''):28} {(r['cargo'] or ''):30} "
                      f"{r['contato']}")

        if commit:
            conn.commit()
            print("\n✅ COMMITADO.")
        else:
            conn.rollback()
            print("\n⚠️  DRY-RUN — nenhuma alteração persistida. Use --commit para aplicar.")
    except Exception as e:
        conn.rollback()
        print(f"❌ ERRO: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true",
                   help="Aplica alterações (default: dry-run)")
    args = p.parse_args()
    main(args.commit)
