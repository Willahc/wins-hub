"""
Rotas REST de Fornecedores.

GET /api/fornecedores            - Listagem com filtros + paginação
GET /api/fornecedores/facetas    - Counts agregados estilo Pichau (Amazon Model B)
GET /api/fornecedores/{cnpj}     - Detalhe de um fornecedor + obras matched

Filtros aceitos (todos opcionais; CSV onde indicado):
  busca       texto livre (razao_social, nome_fantasia, cnae_principal)
  ufs         CSV de UFs                  ex: "SP,RJ"
  portes      CSV de portes               ex: "ME,EPP"
  setores     CSV de setores DAS OBRAS    ex: "MINERACAO,ENERGIA"
              (fornecedor é incluído se aparece como match em ao menos
               uma obra de algum dos setores listados)
  score_min   int 0..100. 0 = todos       (média de m.score do fornecedor)
  limit/offset paginação (defaults 30/0)
"""
import time
from typing import Optional
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, HTTPException


# Bandas de score expostas como facetas
SCORE_BANDS = (60, 70, 80, 90)

# Cache in-memory pra /facetas. TTL longo porque o caso "sem filtro" lê da MV
# (mv_fornecedores_facetas_global + mv_fornecedores_score_bands_global), que é
# refresh-ada pelo orchestrator. Para chaves com filtro, 1h é seguro porque o
# universo de fornecedores raramente muda intra-dia.
_facetas_cache: dict = {}
_FACETAS_TTL = 3600

# Cache key sentinel pro caso sem filtro (todos params None / score_min=0).
_FACETAS_GLOBAL_KEY = "None|None|None|None|0"


def _parse_csv_upper(s: Optional[str]) -> list[str]:
    return [x.strip().upper() for x in (s or "").split(",") if x.strip()]


def _facetas_from_mv(conn) -> dict:
    """Lê facetas globais (sem filtro) das materialized views — <5ms.

    Retorna o mesmo schema que o endpoint /facetas: {ufs, portes, setores, scores, total}.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT tipo, valor, qtd
            FROM mv_fornecedores_facetas_global
            ORDER BY tipo, qtd DESC
        """)
        rows = cur.fetchall()

        cur.execute("""
            SELECT ge60, ge70, ge80, ge90, total
            FROM mv_fornecedores_score_bands_global
        """)
        bands_row = cur.fetchone()

    ufs_facet, portes_facet, setores_facet = [], [], []
    porte_order = {"ME": 1, "EPP": 2, "DEMAIS": 3}
    for r in rows:
        tipo, valor, qtd = r["tipo"], r["valor"], r["qtd"]
        if tipo == "uf":
            ufs_facet.append({"uf": valor, "qtd": qtd})
        elif tipo == "porte":
            portes_facet.append({"porte": valor, "qtd": qtd})
        elif tipo == "setor":
            setores_facet.append({"setor": valor, "qtd": qtd})

    portes_facet.sort(key=lambda x: porte_order.get(x["porte"], 99))

    scores_facet = {f"ge{b}": bands_row[f"ge{b}"] for b in SCORE_BANDS}

    return {
        "ufs": ufs_facet,
        "portes": portes_facet,
        "setores": setores_facet,
        "scores": scores_facet,
        "total": bands_row["total"],
    }


def warmup_facetas_cache(get_conn) -> None:
    """Popula _facetas_cache com a chave 'sem filtro' lendo das MVs.

    Chamado no startup da API (lifespan) para evitar a 1ª chamada quente custar
    qualquer latência. Idempotente: pode ser re-chamado a qualquer momento.
    """
    conn = get_conn()
    try:
        result = _facetas_from_mv(conn)
    finally:
        conn.close()
    _facetas_cache[_FACETAS_GLOBAL_KEY] = (time.time(), result)


def _build_filters(busca, ufs, portes, setores, score_min, skip=None, cnaes=None, alias="e"):
    """
    Monta o WHERE compartilhado entre listagem e facetas.
    `skip` exclui o filtro do próprio campo (Amazon Model B).
    Retorna (sql_where, params).
    """
    conds: list[str] = ["1=1"]
    params: list = []

    if busca:
        # ILIKE na expressão concatenada usa idx_fornecedores_search_trgm (gin_trgm_ops).
        # Segundo predicado restringe match a razao_social/nome_fantasia: o bitmap
        # entrega ~poucas linhas e o Recheck filtra os hits que casariam só em
        # cnae_descricao. Mantém o índice — sem rebuild.
        like = f"%{busca}%"
        conds.append(f"(COALESCE({alias}.razao_social,'') || ' ' || "
                     f"COALESCE({alias}.nome_fantasia,'') || ' ' || "
                     f"COALESCE({alias}.cnae_descricao,'')) ILIKE %s")
        params.append(like)
        conds.append(f"(COALESCE({alias}.razao_social,'') ILIKE %s "
                     f"OR COALESCE({alias}.nome_fantasia,'') ILIKE %s)")
        params.append(like)
        params.append(like)

    if ufs and skip != "uf":
        conds.append(f"{alias}.uf = ANY(%s)")
        params.append(ufs)

    if portes and skip != "porte":
        # Mapa UI 3-níveis → portes RFB:
        # GRANDE → ['GRANDE']  ·  MEDIA → ['MEDIA']
        # PEQUENA → MEI/ME/EPP/DEMAIS + NULL (default RFB sem porte declarado)
        portes_expanded = set()
        include_null = False
        for p in portes:
            if p == 'PEQUENA':
                portes_expanded.update(['MEI', 'ME', 'EPP', 'DEMAIS'])
                include_null = True
            else:
                portes_expanded.add(p)
        if include_null:
            conds.append(f"({alias}.porte = ANY(%s) OR {alias}.porte IS NULL)")
        else:
            conds.append(f"{alias}.porte = ANY(%s)")
        params.append(list(portes_expanded))
    if cnaes and skip != "cnae":
        conds.append(f"{alias}.cnae_principal = ANY(%s)")
        params.append(cnaes)

    if setores and skip != "setor":
        conds.append(
            f"{alias}.cnpj IN (SELECT cnpj FROM fornecedor_setores WHERE setor = ANY(%s))"
        )
        params.append(setores)

    if score_min and skip != "score":
        conds.append("m.score_medio >= %s")
        params.append(score_min)

    return " AND ".join(conds), params


# Pre-aggregated table fornecedor_matches_summary (~5MB, sempre cacheada).
# Refresh via REFRESH function pos-matchmaking. Hash join trivial.
# LEFT JOIN: fornecedores sem matches aparecem com qtd=NULL (tratado via COALESCE).
_BASE_FROM = """
    FROM fornecedores e
    LEFT JOIN fornecedor_matches_summary m ON m.cnpj = e.cnpj
"""


def build_router(get_conn):
    router = APIRouter(prefix="/api/fornecedores", tags=["fornecedores"])

    @router.get("/facetas")
    async def facetas(
        busca: Optional[str] = None,
        ufs: Optional[str] = None,
        portes: Optional[str] = None,
        setores: Optional[str] = None,
        score_min: int = 0,
    ):
        cache_key = f"{busca}|{ufs}|{portes}|{setores}|{score_min}"
        now = time.time()
        cached = _facetas_cache.get(cache_key)
        if cached and now - cached[0] < _FACETAS_TTL:
            return cached[1]

        # Fast-path: caso "sem filtro" lê das materialized views (<5ms) em vez
        # de rodar 5 agregações sobre 2.6M linhas (~18s). Cobre o load inicial
        # da aba Fornecedores, que é o gargalo dominante.
        if cache_key == _FACETAS_GLOBAL_KEY:
            conn = get_conn()
            try:
                result = _facetas_from_mv(conn)
            finally:
                conn.close()
            _facetas_cache[cache_key] = (now, result)
            return result

        ufs_l = _parse_csv_upper(ufs)
        portes_l = _parse_csv_upper(portes)
        setores_l = _parse_csv_upper(setores)
        score_v = score_min if score_min > 0 else None

        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # ── UFs (skip uf) ─────────────────────────────────────────
                w, p = _build_filters(busca, ufs_l, portes_l, setores_l, score_v, skip="uf")
                cur.execute(f"""
                    SELECT e.uf, COUNT(DISTINCT e.cnpj) AS qtd
                    {_BASE_FROM}
                    WHERE {w} AND e.uf IS NOT NULL
                    GROUP BY e.uf
                    ORDER BY qtd DESC
                """, p)
                ufs_facet = [dict(r) for r in cur.fetchall()]

                # ── Portes (skip porte) ───────────────────────────────────
                w, p = _build_filters(busca, ufs_l, portes_l, setores_l, score_v, skip="porte")
                cur.execute(f"""
                    SELECT e.porte, COUNT(DISTINCT e.cnpj) AS qtd
                    {_BASE_FROM}
                    WHERE {w} AND e.porte IN ('ME','EPP','DEMAIS')
                    GROUP BY e.porte
                    ORDER BY CASE e.porte WHEN 'ME' THEN 1 WHEN 'EPP' THEN 2 WHEN 'DEMAIS' THEN 3 END
                """, p)
                portes_facet = [dict(r) for r in cur.fetchall()]

                # ── Setores das obras (skip setor) ────────────────────────
                # Para contar fornecedores por setor, juntamos com obras via
                # matches_obra_prestador. Um fornecedor que atua em N setores
                # aparece em N linhas — comportamento esperado de faceta.
                w, p = _build_filters(busca, ufs_l, portes_l, setores_l, score_v, skip="setor")
                cur.execute(f"""
                    SELECT o.setor, COUNT(DISTINCT e.cnpj) AS qtd
                    {_BASE_FROM}
                    INNER JOIN matches_obra_prestador m2 ON e.cnpj = m2.cnpj
                    INNER JOIN obras o ON m2.obra_id = o.id
                    WHERE {w} AND COALESCE(o.setor,'') <> ''
                    GROUP BY o.setor
                    ORDER BY qtd DESC
                """, p)
                setores_facet = [dict(r) for r in cur.fetchall()]

                # ── Score: bandas (skip score) ────────────────────────────
                w, p = _build_filters(busca, ufs_l, portes_l, setores_l, score_v, skip="score")
                bands_select = ", ".join(
                    f"COUNT(DISTINCT e.cnpj) FILTER (WHERE m.score_medio >= {b}) AS ge{b}"
                    for b in SCORE_BANDS
                )
                cur.execute(f"SELECT {bands_select} {_BASE_FROM} WHERE {w}", p)
                scores_facet = dict(cur.fetchone())

                # ── Total absoluto com TODOS os filtros aplicados ─────────
                w, p = _build_filters(busca, ufs_l, portes_l, setores_l, score_v)
                cur.execute(f"SELECT COUNT(DISTINCT e.cnpj) AS total {_BASE_FROM} WHERE {w}", p)
                total = cur.fetchone()["total"]
        finally:
            conn.close()

        result = {
            "ufs": ufs_facet,
            "portes": portes_facet,
            "setores": setores_facet,
            "scores": scores_facet,
            "total": total,
        }
        _facetas_cache[cache_key] = (now, result)
        return result

    @router.get("/categorias_lote")
    async def categorias_lote(cnpjs: str):
        """Top 3 categorias_servico por CNPJ, agregadas a partir de matches_obra_prestador.

        Usado pela UI da aba Fornecedores: 2-step fetch — após carregar os cards
        via /api/fornecedores, o frontend chama isto com os CNPJs visíveis
        e renderiza as chips de especialidade.

        Lote típico = 30 CNPJs (paginação). Lateral por CNPJ no índice idx_matches_cnpj.
        """
        cnpj_list = [c.strip() for c in (cnpjs or "").split(",") if c.strip()]
        if not cnpj_list:
            return {"categorias": {}}
        if len(cnpj_list) > 100:
            raise HTTPException(400, "Máximo 100 CNPJs por lote.")

        sql = """
            SELECT m.cnpj,
                   array_agg(c.nome ORDER BY cnt DESC) FILTER (WHERE rn <= 3) AS top3
            FROM (
                SELECT m.cnpj, m.categoria_id,
                       COUNT(*) AS cnt,
                       ROW_NUMBER() OVER (PARTITION BY m.cnpj ORDER BY COUNT(*) DESC) AS rn
                FROM matches_obra_prestador m
                WHERE m.cnpj = ANY(%s)
                GROUP BY m.cnpj, m.categoria_id
            ) m
            JOIN categorias_servico c ON c.id = m.categoria_id
            GROUP BY m.cnpj
        """
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (cnpj_list,))
                rows = cur.fetchall()
        finally:
            conn.close()

        return {"categorias": {cnpj: (top3 or []) for cnpj, top3 in rows}}

    _cnaes_lista_cache = {"data": None, "ts": 0.0}

    @router.get("/cnaes-lista")
    async def cnaes_lista():
        """CNAEs mais comuns em fornecedores ativos (top 200), com codigo+descricao+count.
        Usado pelo sidebar filter de CNAE em /fornecedores. Cache 1h.

        IMPORTANTE: declarado antes de /{cnpj} pra FastAPI não casar como CNPJ."""
        import time as _time
        now = _time.time()
        if _cnaes_lista_cache["data"] is not None and (now - _cnaes_lista_cache["ts"]) < 3600:
            return _cnaes_lista_cache["data"]
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        f.cnae_principal AS codigo,
                        COALESCE(c.descricao, f.cnae_principal) AS descricao,
                        COUNT(*) AS total
                    FROM mv_fornecedores_lista_global f
                    LEFT JOIN cnae_oficial c ON c.codigo = f.cnae_principal
                    WHERE f.cnae_principal IS NOT NULL AND f.cnae_principal <> ''
                    GROUP BY f.cnae_principal, c.descricao
                    ORDER BY total DESC
                    LIMIT 200
                """)
                rows = [{"codigo": r["codigo"], "descricao": r["descricao"], "total": int(r["total"])} for r in cur.fetchall()]
        finally:
            conn.close()
        payload = {"cnaes": rows, "total": len(rows)}
        _cnaes_lista_cache["data"] = payload
        _cnaes_lista_cache["ts"] = now
        return payload

    @router.get("/{cnpj}")
    async def detalhe(cnpj: str, limit_obras: int = 5):
        """Detalhe completo + obras onde aparece como sugerido."""
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        e.cnpj, e.razao_social, e.nome_fantasia, e.cnae_principal,
                        e.cnae_descricao, e.cnae_secundarios,
                        e.municipio_nome, e.uf, e.email,
                        e.telefone_1, e.telefone_2, e.porte, e.capital_social,
                        e.data_abertura, e.logradouro, e.numero, e.bairro, e.cep,
                        COALESCE(m.qtd, 0) AS matches_count,
                        COALESCE(ROUND(m.score_medio::numeric, 0), 0) AS score
                    FROM fornecedores e
                    LEFT JOIN (
                        SELECT cnpj, COUNT(*) AS qtd, AVG(score) AS score_medio
                        FROM matches_obra_prestador
                        WHERE cnpj = %s
                        GROUP BY cnpj
                    ) m ON e.cnpj = m.cnpj
                    WHERE e.cnpj = %s
                """, (cnpj, cnpj))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(404, "Fornecedor nao encontrado.")

                fornec = dict(row)
                if fornec.get("capital_social"):
                    fornec["capital_social"] = float(fornec["capital_social"])
                if fornec.get("data_abertura"):
                    fornec["data_abertura"] = str(fornec["data_abertura"])
                if fornec.get("score"):
                    fornec["score"] = int(fornec["score"])

                # Lookup descrições via tabela oficial CNAE 2.3 (IBGE)
                all_codes = []
                if fornec.get("cnae_principal"):
                    all_codes.append(fornec["cnae_principal"])
                if fornec.get("cnae_secundarios"):
                    all_codes.extend(fornec["cnae_secundarios"])
                desc_map = {}
                if all_codes:
                    cur.execute("""
                        SELECT codigo, descricao FROM cnae_oficial
                        WHERE codigo = ANY(%s)
                    """, (all_codes,))
                    for r in cur.fetchall():
                        desc_map[r["codigo"]] = r["descricao"]

                cnae_lista = []
                if fornec.get("cnae_principal"):
                    code = fornec["cnae_principal"]
                    cnae_lista.append({
                        "codigo": code,
                        "descricao": desc_map.get(code) or f"CNAE {code}",
                        "eh_principal": True
                    })
                for c in (fornec.get("cnae_secundarios") or []):
                    cnae_lista.append({
                        "codigo": c,
                        "descricao": desc_map.get(c) or f"CNAE {c}",
                        "eh_principal": False
                    })
                fornec["cnae_lista"] = cnae_lista

                cur.execute("""
                    SELECT id, nome, empresa, setor, uf, fase, score
                    FROM (
                        SELECT DISTINCT ON (o.id)
                               o.id, o.nome, o.empresa, o.setor, o.uf, o.fase, m.score
                        FROM matches_obra_prestador m
                        INNER JOIN obras o ON m.obra_id = o.id
                        WHERE m.cnpj = %s
                        ORDER BY o.id, m.score DESC NULLS LAST
                    ) sub
                    ORDER BY score DESC NULLS LAST
                    LIMIT %s
                """, (cnpj, limit_obras))
                obras = []
                for r in cur.fetchall():
                    d = dict(r)
                    d["id"] = str(d["id"])
                    if d.get("score"):
                        d["score"] = int(d["score"])
                    obras.append(d)
                fornec["obras_matched"] = obras
        finally:
            conn.close()
        return fornec

    @router.get("")
    async def listar(
        busca: Optional[str] = None,
        ufs: Optional[str] = None,
        portes: Optional[str] = None,
        setores: Optional[str] = None,
        cnaes: Optional[str] = None,
        ordem: Optional[str] = None,
        score_min: int = 0,
        limit: int = 30,
        offset: int = 0,
    ):
        ufs_l = _parse_csv_upper(ufs)
        portes_l = _parse_csv_upper(portes)
        setores_l = _parse_csv_upper(setores)
        # CNAEs são códigos (não-UPPER), parseamos como CSV simples
        cnaes_l = [x.strip() for x in (cnaes or "").split(",") if x.strip()] or None
        score_v = score_min if score_min > 0 else None

        _orderby_map = {
            "recente":   "e.cadastrado DESC NULLS LAST, e.razao_social",
            "nome_asc":  "e.razao_social ASC",
            "nome_desc": "e.razao_social DESC",
        }
        _orderby_sql = _orderby_map.get((ordem or "").strip().lower())

        # Fast-path: caso "sem filtro" lê de mv_fornecedores_lista_global
        # (top 5000 já ordenados por matches_count DESC, cadastrado DESC,
        # razao_social). <5ms vs ~3.6s do plano com Sort sobre 2.6M linhas.
        # Só aplica fast-path se NÃO houver ordem custom (mv tem ordem fixa).
        sem_filtro = (
            not busca and not ufs_l and not portes_l
            and not setores_l and not cnaes_l and score_v is None
            and not _orderby_sql
        )
        def _run(query, prm):
            conn = get_conn()
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(query, prm)
                    return cur.fetchall()
            finally:
                conn.close()

        if sem_filtro and offset + limit <= 5000:
            rows = _run("""
                SELECT
                    cnpj, razao_social, nome_fantasia, cnae_principal,
                    municipio_nome, uf, email, telefone_1, porte,
                    capital_social, data_abertura,
                    matches_count, score
                FROM mv_fornecedores_lista_global
                ORDER BY matches_count DESC, cadastrado DESC, razao_social
                LIMIT %s OFFSET %s
            """, [limit, offset])
        else:
            w, params = _build_filters(busca, ufs_l, portes_l, setores_l, score_v, cnaes=cnaes_l)
            select_cols = """
                SELECT
                    e.cnpj, e.razao_social, e.nome_fantasia, e.cnae_principal,
                    e.municipio_nome, e.uf, e.email, e.telefone_1, e.porte,
                    e.capital_social, e.data_abertura,
                    COALESCE(m.qtd, 0) AS matches_count,
                    COALESCE(ROUND(m.score_medio::numeric, 0)::int, 0) AS score
            """
            if busca and not _orderby_sql:
                # Busca livre em 2 niveis: matched-first via indice trgm parcial
                # (idx_forn_search_matched WHERE matches_count>0) -> rapido. O
                # full-scan dos sem-match so roda se a pagina passar dos matched.
                like_pref = f"{busca}%"
                like_sub = f"%{busca}%"
                relevancia = (
                    "(CASE "
                    "WHEN COALESCE(e.razao_social,'') ILIKE %s "
                    "OR COALESCE(e.nome_fantasia,'') ILIKE %s THEN 2 "
                    "WHEN COALESCE(e.razao_social,'') ILIKE %s "
                    "OR COALESCE(e.nome_fantasia,'') ILIKE %s THEN 1 "
                    "ELSE 0 END) DESC"
                )
                rel_params = [like_pref, like_pref, like_sub, like_sub]
                need = offset + limit
                # Tier matched com barreira de otimizacao (OFFSET 0): a busca
                # roda isolada no inner -> planner usa idx_forn_search_matched
                # (parcial). Facetas (uf/porte/cnae/setor) filtram POR FORA via
                # alias 'sub'; sem isso o planner cai no trgm cheio (lento).
                busca_cond = (
                    "(COALESCE(e.razao_social,'') || ' ' || "
                    "COALESCE(e.nome_fantasia,'') || ' ' || "
                    "COALESCE(e.cnae_descricao,'')) ILIKE %s "
                    "AND (COALESCE(e.razao_social,'') ILIKE %s "
                    "OR COALESCE(e.nome_fantasia,'') ILIKE %s)"
                )
                busca_params = [like_sub, like_sub, like_sub]
                inner_score = " AND m.score_medio >= %s" if score_v else ""
                inner_score_params = [score_v] if score_v else []
                facet_w, facet_params = _build_filters(
                    None, ufs_l, portes_l, setores_l, None, cnaes=cnaes_l, alias="sub"
                )
                relevancia_sub = (
                    "(CASE "
                    "WHEN COALESCE(sub.razao_social,'') ILIKE %s "
                    "OR COALESCE(sub.nome_fantasia,'') ILIKE %s THEN 2 "
                    "WHEN COALESCE(sub.razao_social,'') ILIKE %s "
                    "OR COALESCE(sub.nome_fantasia,'') ILIKE %s THEN 1 "
                    "ELSE 0 END) DESC"
                )
                rel_params_sub = [like_pref, like_pref, like_sub, like_sub]
                matched = _run(
                    f"SELECT * FROM ({select_cols}{_BASE_FROM} "
                    f"WHERE {busca_cond} AND e.matches_count > 0{inner_score} "
                    f"ORDER BY e.matches_count DESC LIMIT 20000 OFFSET 0) sub "
                    f"WHERE {facet_w} "
                    f"ORDER BY {relevancia_sub}, sub.matches_count DESC, sub.razao_social ASC "
                    f"LIMIT %s",
                    busca_params + inner_score_params + facet_params + rel_params_sub + [need],
                )
                if len(matched) >= need:
                    rows = matched[offset:need]
                else:
                    m_count = len(matched)
                    head = matched[offset:] if offset < m_count else []
                    remaining = limit - len(head)
                    un_offset = offset - m_count if offset > m_count else 0
                    unmatched = _run(
                        f"{select_cols}{_BASE_FROM} WHERE {w} AND e.matches_count = 0 "
                        f"ORDER BY {relevancia}, e.razao_social ASC LIMIT %s OFFSET %s",
                        list(params) + rel_params + [remaining, un_offset],
                    ) if remaining > 0 else []
                    rows = head + unmatched
            else:
                ob = _orderby_sql or "e.matches_count DESC, e.cadastrado DESC, e.razao_social"
                rows = _run(
                    f"{select_cols}{_BASE_FROM} WHERE {w} ORDER BY {ob} LIMIT %s OFFSET %s",
                    list(params) + [limit, offset],
                )

        fornec = []
        for r in rows:
            d = dict(r)
            if d.get("capital_social"):
                d["capital_social"] = float(d["capital_social"])
            if d.get("data_abertura"):
                d["data_abertura"] = str(d["data_abertura"])
            fornec.append(d)

        return {"fornecedores": fornec, "limit": limit, "offset": offset}

    return router
