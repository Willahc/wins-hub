"""
Service de matchmaking obra <-> prestadores.

Pra cada obra, gera ate 50 sugestoes de prestadores POR CATEGORIA, ranqueados
por score 0-100 (proximidade geografica + CNAE + situacao + porte).
"""
import os
import logging
from typing import Optional
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

log = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

MAX_POR_CATEGORIA = 50
MAX_POR_CATEGORIA_NACIONAL = 200  # obra sem UF: cap mais alto pra cobrir BR todo (sem geo)
SCORE_MINIMO = 30
MAX_POR_PRESTADOR_ON_DEMAND = 500
RANKING_ON_DEMAND = 999  # placeholder; orchestrator semanal recalcula ranking real


def gerar_matches_para_obra(obra_id: str) -> dict:
    """
    Gera matches para uma obra especifica.
    Apaga matches antigos antes de gerar novos (idempotente).
    """
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    stats = {
        "obra_id": obra_id,
        "obra_encontrada": False,
        "categorias_processadas": 0,
        "matches_gerados": 0,
        "matches_por_categoria": {},
    }

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, nome, setor, uf, municipio
                FROM obras
                WHERE id = %s
            """, (obra_id,))
            obra = cur.fetchone()

            if not obra:
                log.warning(f"Obra nao encontrada: {obra_id}")
                return stats

            stats["obra_encontrada"] = True
            stats["obra_nome"] = obra["nome"]
            stats["obra_uf"] = obra["uf"]
            stats["obra_municipio"] = obra["municipio"]
            stats["obra_setor"] = obra["setor"]

            if not obra["setor"]:
                log.warning(f"Obra {obra_id} sem setor")
                return stats

            escopo = "regional" if obra["uf"] else "nacional"
            stats["escopo"] = escopo
            setor_norm = obra["setor"].upper().strip()

            cur.execute("""
                SELECT c.id, c.codigo, c.nome, c.cnaes, sc.prioridade
                FROM categorias_servico c
                JOIN setor_categorias sc ON sc.categoria_id = c.id
                WHERE upper(sc.setor) = %s AND c.ativo = TRUE
                ORDER BY sc.prioridade, c.ordem
            """, (setor_norm,))
            categorias = cur.fetchall()

            if not categorias:
                log.warning(f"Setor {setor_norm} nao tem categorias mapeadas")
                return stats

            log.info(f"Obra {obra['nome']} ({setor_norm}, {obra['municipio']}/{obra['uf']}, escopo={escopo}): {len(categorias)} categorias")

            ufs_vizinhas = []
            if escopo == "regional":
                cur.execute("SELECT uf_vizinha FROM ufs_vizinhas WHERE uf = %s", (obra["uf"],))
                ufs_vizinhas = [row["uf_vizinha"] for row in cur.fetchall()]

            cur.execute("DELETE FROM matches_obra_prestador WHERE obra_id = %s", (obra_id,))
            log.info(f"  matches antigos removidos: {cur.rowcount}")

            todos_matches = []
            for cat in categorias:
                if escopo == "nacional":
                    matches_cat = _buscar_prestadores_categoria_nacional(
                        cur,
                        obra_id=obra_id,
                        categoria_id=cat["id"],
                        cnaes_categoria=cat["cnaes"],
                    )
                else:
                    matches_cat = _buscar_prestadores_categoria(
                        cur,
                        obra_id=obra_id,
                        obra_uf=obra["uf"],
                        obra_municipio=obra["municipio"],
                        ufs_vizinhas=ufs_vizinhas,
                        categoria_id=cat["id"],
                        cnaes_categoria=cat["cnaes"],
                    )
                stats["matches_por_categoria"][cat["codigo"]] = len(matches_cat)
                todos_matches.extend(matches_cat)
                stats["categorias_processadas"] += 1

            if todos_matches:
                execute_values(cur, """
                    INSERT INTO matches_obra_prestador
                        (obra_id, cnpj, categoria_id, ranking, nivel_proximidade, score, escopo)
                    VALUES %s
                    ON CONFLICT (obra_id, cnpj, categoria_id) DO NOTHING
                """, todos_matches)
                stats["matches_gerados"] = len(todos_matches)

            conn.commit()
            log.info(f"  {stats['matches_gerados']} matches gerados em {stats['categorias_processadas']} categorias")

    except Exception as e:
        conn.rollback()
        log.exception(f"Erro processando obra {obra_id}: {e}")
        stats["erro"] = str(e)
    finally:
        conn.close()

    return stats


def _buscar_prestadores_categoria(
    cur,
    obra_id,
    obra_uf,
    obra_municipio,
    ufs_vizinhas,
    categoria_id,
    cnaes_categoria,
):
    cnaes_lista = list(cnaes_categoria)

    sql = """
        WITH candidatos AS (
            SELECT
                e.cnpj,
                e.uf,
                e.municipio_nome,
                CASE
                    WHEN upper(e.municipio_nome) = upper(%(obra_municipio)s) AND e.uf = %(obra_uf)s THEN 40
                    WHEN e.uf = %(obra_uf)s THEN 25
                    WHEN e.uf = ANY(%(ufs_vizinhas)s) THEN 15
                    ELSE 5
                END AS score_geo,
                CASE
                    WHEN e.cnae_principal = ANY(%(cnaes)s) THEN 30
                    WHEN e.cnae_secundarios && %(cnaes)s::text[] THEN 18
                    ELSE 0
                END AS score_cnae,
                15 AS score_situacao,
                CASE
                    WHEN e.porte IN ('05', 'DEMAIS') THEN 10
                    WHEN e.porte IN ('01', '03', 'ME', 'EPP') THEN 4
                    ELSE 7
                END AS score_porte,
                CASE
                    WHEN upper(e.municipio_nome) = upper(%(obra_municipio)s) AND e.uf = %(obra_uf)s THEN 'municipio'
                    WHEN e.uf = %(obra_uf)s THEN 'uf'
                    WHEN e.uf = ANY(%(ufs_vizinhas)s) THEN 'vizinha'
                    ELSE 'distante'
                END AS nivel_proximidade
            FROM fornecedores e
            WHERE e.situacao = 'ATIVA'
              AND (e.cnae_principal = ANY(%(cnaes)s) OR e.cnae_secundarios && %(cnaes)s::text[])
        )
        SELECT
            cnpj,
            nivel_proximidade,
            (score_geo + score_cnae + score_situacao + score_porte) AS score_total
        FROM candidatos
        WHERE (score_geo + score_cnae + score_situacao + score_porte) >= %(score_min)s
        ORDER BY score_total DESC, cnpj
        LIMIT %(limite)s
    """

    cur.execute(sql, {
        "obra_uf": obra_uf,
        "obra_municipio": obra_municipio or "",
        "ufs_vizinhas": ufs_vizinhas or [""],
        "cnaes": cnaes_lista,
        "score_min": SCORE_MINIMO,
        "limite": MAX_POR_CATEGORIA,
    })

    rows = cur.fetchall()
    matches = []
    for ranking, row in enumerate(rows, start=1):
        matches.append((
            obra_id,
            row["cnpj"],
            categoria_id,
            ranking,
            row["nivel_proximidade"],
            row["score_total"],
            "regional",
        ))
    return matches


def _buscar_prestadores_categoria_nacional(
    cur,
    obra_id,
    categoria_id,
    cnaes_categoria,
):
    """Variante sem filtro geografico — pra obras com setor mas sem UF (noticias setoriais).

    Score = CNAE + situacao + porte (geo=0). escopo='nacional' marca o tipo de match;
    nivel_proximidade='distante' (não há cálculo geográfico significativo).
    """
    cnaes_lista = list(cnaes_categoria)
    sql = """
        WITH candidatos AS (
            SELECT
                e.cnpj,
                CASE
                    WHEN e.cnae_principal = ANY(%(cnaes)s) THEN 30
                    WHEN e.cnae_secundarios && %(cnaes)s::text[] THEN 18
                    ELSE 0
                END AS score_cnae,
                15 AS score_situacao,
                CASE
                    WHEN e.porte IN ('05', 'DEMAIS') THEN 10
                    WHEN e.porte IN ('01', '03', 'ME', 'EPP') THEN 4
                    ELSE 7
                END AS score_porte
            FROM fornecedores e
            WHERE e.situacao = 'ATIVA'
              AND (e.cnae_principal = ANY(%(cnaes)s) OR e.cnae_secundarios && %(cnaes)s::text[])
        )
        SELECT
            cnpj,
            'distante' AS nivel_proximidade,
            (score_cnae + score_situacao + score_porte) AS score_total
        FROM candidatos
        WHERE (score_cnae + score_situacao + score_porte) >= %(score_min)s
        ORDER BY score_total DESC, cnpj
        LIMIT %(limite)s
    """
    cur.execute(sql, {
        "cnaes": cnaes_lista,
        "score_min": SCORE_MINIMO,
        "limite": MAX_POR_CATEGORIA_NACIONAL,
    })
    rows = cur.fetchall()
    matches = []
    for ranking, row in enumerate(rows, start=1):
        matches.append((
            obra_id,
            row["cnpj"],
            categoria_id,
            ranking,
            row["nivel_proximidade"],
            row["score_total"],
            "nacional",
        ))
    return matches


def gerar_matches_para_prestador(prestador_id: str) -> dict:
    """Gera/atualiza matches para um prestador específico (chamado on-login).

    Calcula score do prestador contra cada obra cujo setor mapeia para categorias
    compatíveis com os CNAEs do prestador. ON CONFLICT atualiza score; ranking
    ganha placeholder 999 e é recalculado em escala pelo orchestrator semanal
    quando reprocessa a obra inteira.
    """
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    stats = {
        "prestador_id": prestador_id,
        "cnpj": None,
        "matches_gerados": 0,
        "matches_atualizados": 0,
    }
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT p.cnpj,
                       e.cnae_principal,
                       COALESCE(e.cnae_secundarios, ARRAY[]::text[]) AS cnae_secundarios,
                       e.uf, e.municipio_nome, e.situacao, e.porte
                FROM prestadores p
                LEFT JOIN fornecedores e ON e.cnpj = p.cnpj
                WHERE p.id = %s
            """, (prestador_id,))
            row = cur.fetchone()
            if not row or not row.get("cnpj"):
                stats["erro"] = "prestador sem CNPJ cadastrado"
                return stats
            stats["cnpj"] = row["cnpj"]
            if not row.get("cnae_principal"):
                stats["erro"] = "CNPJ não encontrado em fornecedores"
                return stats
            if row.get("situacao") != "ATIVA":
                stats["erro"] = f"empresa não ativa (situação={row.get('situacao')})"
                return stats

            cnaes_prestador = [row["cnae_principal"]] + list(row["cnae_secundarios"])

            cur.execute(
                "DELETE FROM matches_obra_prestador WHERE cnpj = %s AND ranking = %s",
                (row["cnpj"], RANKING_ON_DEMAND),
            )
            stats["matches_removidos_on_demand"] = cur.rowcount

            sql = """
                WITH prestador AS (
                    SELECT %(cnpj)s::text       AS cnpj,
                           %(uf)s::text         AS uf,
                           %(municipio)s::text  AS municipio,
                           %(porte)s::text      AS porte,
                           %(cnae_p)s::text     AS cnae_principal,
                           %(cnaes_sec)s::text[] AS cnae_secundarios,
                           %(cnaes_all)s::text[] AS cnaes_all
                ),
                cat_relev AS (
                    SELECT c.id, c.cnaes
                    FROM categorias_servico c, prestador p
                    WHERE c.ativo AND c.cnaes && p.cnaes_all
                ),
                obra_cat AS (
                    SELECT DISTINCT o.id AS obra_id, o.uf AS obra_uf,
                                    o.municipio AS obra_municipio,
                                    cr.id AS categoria_id, cr.cnaes AS cat_cnaes
                    FROM obras o
                    JOIN setor_categorias sc ON upper(sc.setor) = upper(o.setor)
                    JOIN cat_relev cr ON cr.id = sc.categoria_id
                    WHERE (o.visivel IS NULL OR o.visivel = TRUE)
                      AND o.uf IS NOT NULL AND o.setor IS NOT NULL
                ),
                scored AS (
                    SELECT oc.obra_id, p.cnpj, oc.categoria_id,
                           CASE
                               WHEN upper(p.municipio) = upper(oc.obra_municipio) AND p.uf = oc.obra_uf THEN 'municipio'
                               WHEN p.uf = oc.obra_uf THEN 'uf'
                               WHEN p.uf = ANY(SELECT uf_vizinha FROM ufs_vizinhas WHERE uf = oc.obra_uf) THEN 'vizinha'
                               ELSE 'distante'
                           END AS nivel_proximidade,
                           (
                               CASE
                                   WHEN upper(p.municipio) = upper(oc.obra_municipio) AND p.uf = oc.obra_uf THEN 40
                                   WHEN p.uf = oc.obra_uf THEN 25
                                   WHEN p.uf = ANY(SELECT uf_vizinha FROM ufs_vizinhas WHERE uf = oc.obra_uf) THEN 15
                                   ELSE 5
                               END
                             + CASE
                                   WHEN p.cnae_principal = ANY(oc.cat_cnaes) THEN 30
                                   WHEN p.cnae_secundarios && oc.cat_cnaes THEN 18
                                   ELSE 0
                               END
                             + 15
                             + CASE
                                   WHEN p.porte IN ('05','DEMAIS') THEN 10
                                   WHEN p.porte IN ('01','03','ME','EPP') THEN 4
                                   ELSE 7
                               END
                           ) AS score
                    FROM obra_cat oc CROSS JOIN prestador p
                ),
                top_n AS (
                    SELECT obra_id, cnpj, categoria_id, nivel_proximidade, score
                    FROM scored
                    WHERE score >= %(score_min)s
                    ORDER BY score DESC, obra_id
                    LIMIT %(max_total)s
                )
                INSERT INTO matches_obra_prestador
                    (obra_id, cnpj, categoria_id, ranking, nivel_proximidade, score)
                SELECT obra_id, cnpj, categoria_id, %(ranking)s, nivel_proximidade, score
                FROM top_n
                ON CONFLICT (obra_id, cnpj, categoria_id) DO NOTHING
                RETURNING obra_id
            """
            cur.execute(sql, {
                "cnpj": row["cnpj"],
                "uf": row.get("uf") or "",
                "municipio": row.get("municipio_nome") or "",
                "porte": row.get("porte") or "",
                "cnae_p": row["cnae_principal"],
                "cnaes_sec": list(row["cnae_secundarios"]),
                "cnaes_all": cnaes_prestador,
                "score_min": SCORE_MINIMO,
                "max_total": MAX_POR_PRESTADOR_ON_DEMAND,
                "ranking": RANKING_ON_DEMAND,
            })
            stats["matches_gerados"] = len(cur.fetchall())
        conn.commit()
        log.info(
            f"prestador {prestador_id} (cnpj={row['cnpj']}): "
            f"{stats['matches_gerados']} novos, {stats['matches_atualizados']} atualizados"
        )
    except Exception as e:
        conn.rollback()
        log.exception(f"Erro processando prestador {prestador_id}: {e}")
        stats["erro"] = str(e)
    finally:
        conn.close()
    return stats


def gerar_matches_para_todas_obras(limite=None):
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            sql = "SELECT id FROM obras WHERE setor IS NOT NULL AND uf IS NOT NULL"
            if limite:
                sql += f" LIMIT {int(limite)}"
            cur.execute(sql)
            obra_ids = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    log.info(f"Processando {len(obra_ids)} obras...")
    total_matches = 0
    obras_com_match = 0

    for i, obra_id in enumerate(obra_ids, 1):
        stats = gerar_matches_para_obra(str(obra_id))
        if stats.get("matches_gerados", 0) > 0:
            obras_com_match += 1
            total_matches += stats["matches_gerados"]
        if i % 10 == 0:
            log.info(f"  {i}/{len(obra_ids)} processadas, {total_matches} matches")

    return {
        "obras_processadas": len(obra_ids),
        "obras_com_match": obras_com_match,
        "total_matches": total_matches,
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    if len(sys.argv) < 2:
        print("Uso:")
        print("  python matchmaking.py <obra_id>")
        print("  python matchmaking.py --todas")
        print("  python matchmaking.py --todas --limite 5")
        sys.exit(1)

    if sys.argv[1] == "--todas":
        limite = None
        if "--limite" in sys.argv:
            idx = sys.argv.index("--limite")
            limite = int(sys.argv[idx + 1])
        result = gerar_matches_para_todas_obras(limite)
        print(f"\nResultado: {result}")
    else:
        result = gerar_matches_para_obra(sys.argv[1])
        print(f"\nResultado:")
        for k, v in result.items():
            print(f"  {k}: {v}")
