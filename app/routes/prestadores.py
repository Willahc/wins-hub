# === INÍCIO ===
"""
Rotas de matchmaking obra <-> prestadores.

GET  /api/obras/{oid}/prestadores-sugeridos
GET  /api/obras/{oid}/decisores            (agrupado por tipo_cargo, lista Anderson)
POST /api/obras/{oid}/gerar-matches        (admin/interno)
"""
import logging
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, HTTPException, Depends
from permissions import pode_ver_decisores_obra, pode_ver_conteudo_pago

log = logging.getLogger(__name__)


TIPO_CARGO_LABEL = {
    "GERENTE_SUPRIMENTOS":       "Gerente / Coordenador de Suprimentos",
    "GERENTE_COMPRAS":           "Gerente / Coordenador de Compras",
    "SUPPLY_CHAIN":              "Supply Chain Manager",
    "ENGENHEIRO_MECANICO_CIVIL": "Engenheiro Mecânico / Civil",
    "GERENTE_ENGENHARIA":        "Gerente de Engenharia",
    "PROJETISTA":                "Projetista",
    "COORDENADOR_MANUTENCAO":    "Coordenador de Manutenção",
    "GERENTE_INDUSTRIAL":        "Gerente Industrial",
    "COORDENADOR_OBRAS":         "Coordenador de Obras",
    "GERENTE_PROJETOS":          "Gerente de Projetos",
    "DECISOR_AUTO":              "Decisor",
    "OUTRO":                     "Outro",
}

TIPO_CARGO_ORDEM = [
    "GERENTE_SUPRIMENTOS", "GERENTE_COMPRAS", "SUPPLY_CHAIN",
    "GERENTE_ENGENHARIA", "ENGENHEIRO_MECANICO_CIVIL", "PROJETISTA",
    "GERENTE_PROJETOS", "COORDENADOR_OBRAS", "COORDENADOR_MANUTENCAO",
    "GERENTE_INDUSTRIAL", "OUTRO", "DECISOR_AUTO",
]


def build_router(get_conn, requer_auth):
    router = APIRouter(prefix="/api/obras", tags=["prestadores"])

    def obter_usuario_completo(u=Depends(requer_auth)):
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT email, COALESCE(plano,'GRATUITO') AS plano,
                           COALESCE(is_representante,false) AS is_representante
                    FROM prestadores WHERE id=%s
                """, (u["sub"],))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(401, "Usuário não encontrado")
                return {**u, **dict(row)}
        finally:
            conn.close()

    @router.get("/{oid}/prestadores-sugeridos")
    async def listar_prestadores_sugeridos(
        oid: str,
        regenerar: bool = False,
        u=Depends(obter_usuario_completo),
    ):
        if not pode_ver_conteudo_pago(u):
            raise HTTPException(402, "Upgrade necessario para visualizar prestadores sugeridos.")

        plano = (u.get("plano") if u else None) or "BASICO"

        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, nome, setor, uf, municipio FROM obras WHERE id=%s", (oid,))
                obra = cur.fetchone()
                if not obra:
                    raise HTTPException(404, "Obra nao encontrada.")

            desbloqueada = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM interacoes WHERE obra_id=%s AND prestador_id=%s AND tipo='DESBLOQUEIO' LIMIT 1",
                    (oid, u["sub"])
                )
                desbloqueada = cur.fetchone() is not None

            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM matches_obra_prestador WHERE obra_id=%s", (oid,))
                total_matches = cur.fetchone()[0]

            if total_matches == 0 or regenerar:
                conn.close()
                from services.matchmaking import gerar_matches_para_obra
                stats = gerar_matches_para_obra(oid)
                if stats.get("erro"):
                    raise HTTPException(500, "Erro gerando matches")
                total_matches = stats.get("matches_gerados", 0)
                conn = get_conn()

            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        c.codigo AS cat_codigo,
                        c.nome   AS cat_nome,
                        c.ordem  AS cat_ordem,
                        m.ranking,
                        m.cnpj,
                        m.nivel_proximidade,
                        m.score,
                        e.nome_fantasia,
                        e.razao_social,
                        e.municipio_nome,
                        e.uf,
                        e.telefone_1,
                        e.telefone_2,
                        e.email,
                        e.cnae_principal
                    FROM matches_obra_prestador m
                    JOIN categorias_servico c ON c.id = m.categoria_id
                    LEFT JOIN fornecedores e ON e.cnpj = m.cnpj
                    WHERE m.obra_id = %s
                    ORDER BY c.ordem, m.ranking
                """, (oid,))
                rows = cur.fetchall()

            categorias_dict = {}
            for r in rows:
                code = r["cat_codigo"]
                if code not in categorias_dict:
                    categorias_dict[code] = {
                        "codigo": code,
                        "nome": r["cat_nome"],
                        "ordem": r["cat_ordem"],
                        "qtd_matches": 0,
                        "prestadores": [],
                    }

                if desbloqueada:
                    prestador = {
                        "ranking": r["ranking"],
                        "cnpj": r["cnpj"],
                        "nome_fantasia": r["nome_fantasia"] or r["razao_social"],
                        "municipio": r["municipio_nome"],
                        "uf": r["uf"],
                        "telefone": r["telefone_1"] or r["telefone_2"],
                        "email": r["email"],
                        "cnae_principal": r["cnae_principal"],
                        "nivel_proximidade": r["nivel_proximidade"],
                        "score": float(r["score"]) if r["score"] else 0,
                    }
                else:
                    nome = r["nome_fantasia"] or r["razao_social"] or ""
                    nome_mascarado = (nome[:3] + "***") if len(nome) > 3 else "***"
                    prestador = {
                        "ranking": r["ranking"],
                        "nome_fantasia": nome_mascarado,
                        "municipio": r["municipio_nome"],
                        "uf": r["uf"],
                        "nivel_proximidade": r["nivel_proximidade"],
                        "score": float(r["score"]) if r["score"] else 0,
                        "_locked": True,
                    }

                categorias_dict[code]["prestadores"].append(prestador)
                categorias_dict[code]["qtd_matches"] += 1

            categorias = sorted(categorias_dict.values(), key=lambda x: x["ordem"])

            return {
                "obra": dict(obra),
                "desbloqueada": desbloqueada,
                "plano": plano,
                "total_matches": total_matches,
                "categorias": categorias,
            }

        finally:
            try:
                conn.close()
            except Exception:
                pass

    @router.get("/{oid}/decisores")
    async def listar_decisores(oid: str, u=Depends(obter_usuario_completo)):
        """Decisores agrupados por tipo_cargo (lista do Anderson).
        Contatos só para clientes pagantes (não-rep). Reps NUNCA veem contato.
        """
        plano = (u.get("plano") if u else None) or "BASICO"
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id::text, nome, cargo, tipo_cargo,
                           linkedin_url, email, telefone, fonte, observacoes,
                           registrado_em
                    FROM decisores_obra
                    WHERE obra_id = %s AND excluido_em IS NULL
                    ORDER BY tipo_cargo NULLS LAST, registrado_em DESC
                """, (oid,))
                rows = cur.fetchall()
        finally:
            conn.close()

        revelar = pode_ver_decisores_obra(u)
        decisores = []
        for r in rows:
            tipo = r["tipo_cargo"] or "OUTRO"
            d = {
                "id": r["id"],
                "tipo_cargo": tipo,
                "label": TIPO_CARGO_LABEL.get(tipo, tipo),
                "nome": r["nome"],
                "cargo": r["cargo"],
            }
            if revelar:
                d["linkedin"] = r["linkedin_url"] or None
                d["email"] = r["email"] or None
                d["telefone"] = r["telefone"] or None
                d["fonte"] = r["fonte"]
            else:
                d["bloqueado"] = True
            decisores.append(d)

        decisores.sort(key=lambda d: (
            TIPO_CARGO_ORDEM.index(d["tipo_cargo"]) if d["tipo_cargo"] in TIPO_CARGO_ORDEM else 99,
            d["nome"] or "",
        ))

        return {
            "obra_id": oid,
            "plano": plano,
            "acesso_completo": revelar,
            "total": len(decisores),
            "decisores": decisores,
        }

    @router.post("/{oid}/gerar-matches")
    async def gerar_matches_obra(oid: str, u=Depends(requer_auth)):
        if u.get("plano", "GRATUITO") == "GRATUITO":
            raise HTTPException(402, "Upgrade necessario.")

        from services.matchmaking import gerar_matches_para_obra
        stats = gerar_matches_para_obra(oid)
        if not stats.get("obra_encontrada"):
            raise HTTPException(404, "Obra nao encontrada.")
        if stats.get("erro"):
            raise HTTPException(500, "Erro interno.")
        return stats

    return router
# === FIM ===
