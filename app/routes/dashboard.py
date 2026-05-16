# === INÍCIO ===
"""
Rotas do dashboard SIGNAL (KPIs e contagens).
GET /api/dashboard/kpis        - KPIs gerais + por fase + por setor
GET /api/dashboard/ufs         - UFs com count de obras
GET /api/dashboard/setores     - Faceta de setores (filtros aplicados)
GET /api/dashboard/fases       - Faceta de fases (filtros aplicados)
GET /api/dashboard/ufs_facet   - Faceta de UFs (filtros aplicados)

Endpoints de fornecedores (lista, facetas, detalhe) ficam em
routes/fornecedores.py com prefixo /api/fornecedores — padrão REST.
"""
import logging
import time
from typing import Optional
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter

log = logging.getLogger(__name__)

_kpis_cache = {"data": None, "ts": 0.0}
_KPIS_TTL = 300


# === Helper para filtros facetados ===

def _build_facet_query(coluna, ufs, setores, fases, busca):
    """
    Monta query GROUP BY que respeita os outros filtros mas IGNORA o filtro
    da propria coluna (faceted search estilo Amazon).
    """
    cond = ["1=1"]
    params = []
    if coluna != "uf" and ufs:
        cond.append("uf = ANY(%s)")
        params.append(ufs)
    if coluna != "setor" and setores:
        cond.append("setor = ANY(%s)")
        params.append(setores)
    if coluna != "fase" and fases:
        cond.append("fase = ANY(%s)")
        params.append(fases)
    if busca:
        cond.append("(nome ILIKE %s OR empresa ILIKE %s)")
        params.append(f"%{busca}%")
        params.append(f"%{busca}%")
    cond.append(f"{coluna} IS NOT NULL AND {coluna} != ''")
    cond.append("(visivel IS NULL OR visivel = true)")
    cond.append("COALESCE(fonte,'') != 'anp_pte'")
    sql = f"""
        SELECT {coluna} as valor, COUNT(*) as total
        FROM obras
        WHERE {" AND ".join(cond)}
        GROUP BY {coluna}
        ORDER BY total DESC
    """
    return sql, params


def build_router(get_conn):
    router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

    @router.get("/kpis")
    async def kpis():
        # ═══════════════════════════════════════════════════════════════
        # REGRA IMUTÁVEL — NÃO ALTERAR SEM APROVAÇÃO EXPLÍCITA DO WILLIAM
        # Este endpoint retorna TOTAIS GERAIS (obras / fornecedores / matches / score_medio).
        # NÃO é contador OURO/PRATA — esses ficam em app/main.py:
        #   - /api/dashboard/ouro_count      (COUNT classificacao_computed='OURO')
        #   - /api/dashboard/prata_count     (COUNT classificacao_computed='PRATA')
        #   - /api/dashboard/stats-public    (ouro + prata + pipeline + capex_total_bi)
        # Os filtros de prospecção (OURO_DECISOR_SQL, PRATA_MATCH_SQL) existem SEPARADOS
        # e só são usados em /api/dashboard/matches_ouro e /api/dashboard/times_ouro —
        # NUNCA nos contadores do hero/dashboard. Discutir antes de mexer.
        # ═══════════════════════════════════════════════════════════════
        now = time.time()
        if _kpis_cache["data"] is not None and now - _kpis_cache["ts"] < _KPIS_TTL:
            return _kpis_cache["data"]

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Filtro canônico: obras visíveis (NOTICIAs entram no total geral,
                # ficam fora só do funil Ouro/Prata — ver CLAUDE.md).
                # anp_pte: agregados macro ANP-PTE sem CNPJ por linha, distorcem totais.
                cur.execute("SELECT COUNT(*) FROM obras WHERE (visivel IS NULL OR visivel = true) AND COALESCE(fonte,'') != 'anp_pte'")
                obras = cur.fetchone()[0]

                cur.execute("SELECT COUNT(*) FROM fornecedores")
                empresas = cur.fetchone()[0]

                cur.execute("SELECT COUNT(*) FROM matches_obra_prestador")
                matches = cur.fetchone()[0]

                cur.execute("SELECT COALESCE(ROUND(AVG(score)::numeric, 0), 0) FROM matches_obra_prestador")
                score_medio = int(cur.fetchone()[0] or 0)

                # Fases (top 6)
                cur.execute("""
                    SELECT fase, COUNT(*) as total
                    FROM obras
                    WHERE fase IS NOT NULL
                      AND (visivel IS NULL OR visivel = true)
                      AND COALESCE(fonte,'') != 'anp_pte'
                    GROUP BY fase
                    ORDER BY total DESC
                    LIMIT 6
                """)
                fases_raw = cur.fetchall()
                max_fase = fases_raw[0][1] if fases_raw else 1
                fases = [
                    {"fase": f, "total": t, "pct": int(100 * t / max_fase)}
                    for f, t in fases_raw
                ]

                # Setores (top 8)
                cur.execute("""
                    SELECT setor, COUNT(*) as total
                    FROM obras
                    WHERE setor IS NOT NULL
                      AND (visivel IS NULL OR visivel = true)
                      AND COALESCE(fonte,'') != 'anp_pte'
                    GROUP BY setor
                    ORDER BY total DESC
                    LIMIT 8
                """)
                setores_raw = cur.fetchall()
                max_setor = setores_raw[0][1] if setores_raw else 1
                setores = [
                    {"setor": s, "total": t, "pct": int(100 * t / max_setor)}
                    for s, t in setores_raw
                ]
        finally:
            conn.close()

        result = {
            "obras": obras,
            "fornecedores": empresas,
            "matches": matches,
            "score_medio": score_medio,
            "fases": fases,
            "setores": setores,
        }
        _kpis_cache["data"] = result
        _kpis_cache["ts"] = now
        return result

    @router.get("/ufs")
    async def ufs():
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT uf, COUNT(*) as total
                    FROM obras
                    WHERE uf IS NOT NULL AND uf != ''
                      AND (visivel IS NULL OR visivel = true)
                      AND COALESCE(fonte,'') != 'anp_pte'
                    GROUP BY uf
                    ORDER BY total DESC
                """)
                rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            return {"ufs": []}

        max_total = rows[0][1]
        return {
            "ufs": [
                {"uf": uf, "total": total, "pct": int(100 * total / max_total)}
                for uf, total in rows
            ]
        }

    @router.get("/setores")
    async def setores_filtered(
        ufs: Optional[str] = None, setores: Optional[str] = None,
        fases: Optional[str] = None, busca: Optional[str] = None
    ):
        ufs_list = [u for u in (ufs or "").split(",") if u]
        setores_list = [s for s in (setores or "").split(",") if s]
        fases_list = [f for f in (fases or "").split(",") if f]
        sql, params = _build_facet_query("setor", ufs_list, setores_list, fases_list, busca)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        finally:
            conn.close()
        return {"setores": [{"setor": r[0], "total": r[1]} for r in rows]}

    @router.get("/fases")
    async def fases_filtered(
        ufs: Optional[str] = None, setores: Optional[str] = None,
        fases: Optional[str] = None, busca: Optional[str] = None
    ):
        ufs_list = [u for u in (ufs or "").split(",") if u]
        setores_list = [s for s in (setores or "").split(",") if s]
        fases_list = [f for f in (fases or "").split(",") if f]
        sql, params = _build_facet_query("fase", ufs_list, setores_list, fases_list, busca)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        finally:
            conn.close()
        return {"fases": [{"fase": r[0], "total": r[1]} for r in rows]}

    @router.get("/ufs_facet")
    async def ufs_facet(
        ufs: Optional[str] = None, setores: Optional[str] = None,
        fases: Optional[str] = None, busca: Optional[str] = None
    ):
        ufs_list = [u for u in (ufs or "").split(",") if u]
        setores_list = [s for s in (setores or "").split(",") if s]
        fases_list = [f for f in (fases or "").split(",") if f]
        sql, params = _build_facet_query("uf", ufs_list, setores_list, fases_list, busca)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        finally:
            conn.close()
        return {"ufs": [{"uf": r[0], "total": r[1]} for r in rows]}

    return router
# === FIM ===
