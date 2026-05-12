"""
Script único: gera matches para obras-ouro que ficaram órfãs (sem entradas em
matches_obra_prestador). Rodar uma vez após import via PLANILHA do Anderson
ou após captação que pulou matchmaking automático.

Idempotente: services.matchmaking.gerar_matches_para_obra faz DELETE WHERE obra_id=
antes de inserir, e o INSERT usa ON CONFLICT DO NOTHING. Não toca obras que já
têm matches.

Uso: docker exec wins_hub-api-1 python /app/scripts/regerar_matches_orfas.py
"""
import sys
sys.path.insert(0, "/app")

import psycopg2
from psycopg2.extras import RealDictCursor

from services.matchmaking import DB_CONFIG, gerar_matches_para_obra


SQL_ORFAS = """
    SELECT o.id::text AS id,
           COALESCE(o.empresa, '(sem empresa)') AS empresa,
           LEFT(o.nome, 50) AS nome,
           o.uf,
           o.fonte
    FROM obras o
    WHERE nivel1_nome IS NOT NULL AND nivel1_nome != ''
      AND (COALESCE(nivel1_email, '') != '' OR COALESCE(nivel1_linkedin, '') != '')
      AND cargo_decisor_keyword(nivel1_cargo)
      AND COALESCE(fonte_tipo, 'OFICIAL') != 'NOTICIA'
      AND (visivel IS NULL OR visivel = TRUE)
      AND NOT EXISTS (
          SELECT 1 FROM matches_obra_prestador m WHERE m.obra_id = o.id
      )
    ORDER BY o.uf, o.lead_score DESC NULLS LAST
"""


def main():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(SQL_ORFAS)
        orfas = cur.fetchall()
    conn.close()

    print(f"Encontradas {len(orfas)} obras-ouro órfãs (sem matches).")
    print(f"{'-' * 110}")
    print(f"{'UF':3} | {'FONTE':22} | {'EMPRESA':32} | {'OBRA':50} | MATCHES")
    print(f"{'-' * 110}")

    total = 0
    falhas = 0
    sem_categoria = 0
    for obra in orfas:
        stats = gerar_matches_para_obra(obra["id"])
        gerados = stats.get("matches_gerados", 0)
        total += gerados
        if "erro" in stats:
            falhas += 1
            tag = "ERRO"
        elif gerados == 0 and stats.get("categorias_processadas", 0) == 0:
            sem_categoria += 1
            tag = "—"
        else:
            tag = str(gerados)
        print(f"{obra['uf']:3} | {(obra['fonte'] or '')[:22]:22} | "
              f"{obra['empresa'][:32]:32} | {obra['nome'][:50]:50} | {tag}")

    print(f"{'-' * 110}")
    print(f"TOTAL: {total} matches gerados em {len(orfas)} obras "
          f"(falhas: {falhas}, sem categoria mapeada: {sem_categoria})")


if __name__ == "__main__":
    main()
