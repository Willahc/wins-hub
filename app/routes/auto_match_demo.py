"""Demo: Auto-Match Fornecedor v2 com 3 fixes (penalty BNDES + bonus decisor + dedup)."""
import os
from datetime import datetime
from typing import List, Optional, Dict, Any

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter
from pydantic import BaseModel

from sales_intelligence.llm_enricher.client import get_client, MODEL_HAIKU

router = APIRouter(prefix="/auto-match-demo", tags=["demo"])

FORNECEDOR_DEMO = {
    "cnpj": "16654006000110",
    "razao_social": "TETRA TECH BRASIL",
    "cnae_principal": "7112000",
    "cnae_secundarios": ["4313400", "7119799"],
    "uf": "SP",
    "municipio_nome": "Sao Paulo",
    "porte": "DEMAIS",
    "situacao": "ATIVA",
    "especialidades": [
        "geotecnica", "sondagem", "fundacao",
        "investigacao subsolo", "instrumentacao",
        "hidrogeologia", "monitoramento ambiental",
    ],
    "referencias": [
        "Aeroporto Galeao R$19bi (sondagens fundacao)",
        "Vale Carajas (instrumentacao geotecnica)",
        "CCR ViaSul (pacotes geotecnicos)",
    ],
}

RAZAO_MATCH_PROMPT = """Voce analisa compatibilidade entre fornecedor B2B e obra de infraestrutura.

FORNECEDOR:
Nome: {fornecedor_nome}
Especialidades: {fornecedor_especialidades}
Referencias: {fornecedor_referencias}

OBRA:
Nome: {obra_nome}
Capex: {obra_valor_formatado}
Fase: {obra_fase}
UF: {obra_uf}
Descricao: {obra_descricao}
Empresa contratante: {empresa_contratante}

TAREFA: Em 1-2 frases (maximo 200 caracteres), explique por que essa obra e uma boa oportunidade para o fornecedor.

REGRAS:
- Mencionar fato especifico da obra (nao generico)
- Conectar com 1 especialidade do fornecedor
- Tom analitico, nao vendedor
- Sem cliches ("oportunidade unica", "imperdivel")
- Use APENAS dados do input. NAO infira datas/numeros nao-presentes.

REGRA CRITICA:
Se a obra parecer ser OPERACAO FINANCEIRA (BNDES, limite de credito, financiamento, aquisicao de maquinas/equipamentos sem contexto de obra civil), NAO tente justificar match com expertise tecnica. Responda exatamente:
"Registro financeiro/BNDES - nao e obra fisica, nao aplicavel a servicos de consultoria geotecnica."

Responda APENAS o texto da razao (sem JSON, sem markdown)."""


class MatchResult(BaseModel):
    obra_id: str
    obra_nome: str
    obra_valor_formatado: Optional[str]
    obra_fase: str
    obra_uf: Optional[str]
    empresa_contratante: Optional[str]
    score: int
    score_breakdown: Dict[str, int]
    razao_match: str
    decisor_associado: Optional[Dict[str, Any]] = None
    tem_decisor_cacheado: bool = False


class AutoMatchResponse(BaseModel):
    fornecedor_nome: str
    buscado_em: str
    creditos_consumidos_mock: int
    custo_simulado_brl: float
    total_matches: int
    resultados: List[MatchResult]


def _db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def _gerar_razao_haiku(obra: Dict, fornec: Dict) -> str:
    try:
        client = get_client()
        r = client.messages.create(
            model=MODEL_HAIKU,
            max_tokens=120,
            messages=[{"role": "user", "content": RAZAO_MATCH_PROMPT.format(
                fornecedor_nome=fornec["razao_social"],
                fornecedor_especialidades=", ".join(fornec["especialidades"]),
                fornecedor_referencias="; ".join(fornec["referencias"]),
                obra_nome=obra["obra_nome"],
                obra_valor_formatado=obra.get("obra_valor_formatado") or "(valor)",
                obra_fase=obra["obra_fase"],
                obra_uf=obra.get("obra_uf") or "?",
                obra_descricao=(obra.get("obra_descricao") or "")[:400],
                empresa_contratante=obra.get("empresa_contratante") or "?",
            )}],
        )
        return r.content[0].text.strip()[:300]
    except Exception as e:
        return f"(falha gerar razao: {type(e).__name__})"


@router.post("/buscar", response_model=AutoMatchResponse)
async def auto_match_buscar(limite: int = 10):
    """Demo v2: penalty BNDES + bonus decisor + dedup por (nome, cnpj)."""
    fornec = FORNECEDOR_DEMO
    cnaes_all = [fornec["cnae_principal"]] + fornec["cnae_secundarios"]

    sql = """
        WITH prestador AS (
            SELECT %(uf)s::text AS uf, %(municipio)s::text AS municipio,
                   %(porte)s::text AS porte, %(cnae_p)s::text AS cnae_p,
                   %(cnaes_sec)s::text[] AS cnaes_sec,
                   %(cnaes_all)s::text[] AS cnaes_all
        ),
        cat_relev AS (
            SELECT c.id, c.cnaes
            FROM categorias_servico c, prestador p
            WHERE c.ativo AND c.cnaes && p.cnaes_all
        ),
        obra_base AS (
            -- FIX 3 DEDUP: 1 row por (nome similar + cnpj)
            SELECT DISTINCT ON (LEFT(LOWER(o.nome), 80), o.cnpj)
                   o.id AS obra_id, o.nome AS obra_nome,
                   o.valor_formatado AS obra_valor_formatado,
                   o.valor_estimado, o.fase AS obra_fase, o.uf AS obra_uf,
                   o.empresa AS empresa_contratante, o.cnpj AS obra_cnpj,
                   LEFT(o.descricao, 400) AS obra_descricao,
                   o.municipio AS obra_municipio, o.setor AS obra_setor,
                   o.lead_score,
                   -- FIX 1 PENALTY BNDES/financiamento
                   CASE
                     WHEN LOWER(o.nome) LIKE '%%bndes%%'
                       OR LOWER(o.nome) LIKE '%%limite%%credit%%'
                       OR LOWER(o.nome) LIKE '%%financiamento%%'
                       OR LOWER(o.nome) LIKE '%%aquisicao%%maquinas%%'
                       OR LOWER(o.descricao) LIKE '%%cfi do bndes%%'
                       OR LOWER(o.descricao) LIKE '%%limites de credito%%'
                     THEN 0.3 ELSE 1.0
                   END AS penalty_financeiro,
                   -- FIX 2 BONUS DECISOR
                   CASE WHEN EXISTS (
                     SELECT 1 FROM empresa_decisores_cache d
                     WHERE d.cnpj = o.cnpj AND d.trabalha_atualmente = true
                       AND d.email_status = 'verified_smtp'
                       AND d.filtro_llm_confianca IN ('alta','media')
                       AND d.excluido_em IS NULL
                   ) THEN 1.3 ELSE 1.0 END AS bonus_decisor
            FROM obras o
            WHERE o.visivel = true AND o.valor_estimado IS NOT NULL
              AND o.fase IN ('EM_EXECUCAO','PLANEJAMENTO','LICENCA_INSTALACAO',
                             'LICENCA_PREVIA','PROJETO','LICITACAO_ABERTA')
              AND upper(o.setor) IN ('ENERGIA','MINERACAO','INFRAESTRUTURA','PORTUARIO')
              AND o.lead_score >= 70
            ORDER BY LEFT(LOWER(o.nome), 80), o.cnpj, o.lead_score DESC
        ),
        obra_cat AS (
            SELECT ob.*, cr.cnaes AS cat_cnaes
            FROM obra_base ob
            JOIN setor_categorias sc ON upper(sc.setor) = upper(ob.obra_setor)
            JOIN cat_relev cr ON cr.id = sc.categoria_id
        ),
        scored AS (
            SELECT oc.*, p.uf AS prest_uf, p.municipio AS prest_mun,
                   CASE
                       WHEN upper(p.municipio) = upper(oc.obra_municipio) AND p.uf = oc.obra_uf THEN 40
                       WHEN p.uf = oc.obra_uf THEN 25
                       WHEN p.uf = ANY(SELECT uf_vizinha FROM ufs_vizinhas WHERE uf = oc.obra_uf) THEN 15
                       ELSE 5
                   END AS score_geo,
                   CASE
                       WHEN p.cnae_p = ANY(oc.cat_cnaes) THEN 30
                       WHEN p.cnaes_sec && oc.cat_cnaes THEN 18
                       ELSE 0
                   END AS score_cnae,
                   15 AS score_situacao,
                   CASE
                       WHEN p.porte IN ('05','DEMAIS') THEN 10
                       WHEN p.porte IN ('01','03','ME','EPP') THEN 4
                       ELSE 7
                   END AS score_porte
            FROM obra_cat oc CROSS JOIN prestador p
        ),
        ranked AS (
            SELECT *,
                   (score_geo + score_cnae + score_situacao + score_porte) AS score_bruto,
                   ((score_geo + score_cnae + score_situacao + score_porte) * penalty_financeiro * bonus_decisor)::int AS score_ajustado,
                   ROW_NUMBER() OVER (PARTITION BY obra_id
                                      ORDER BY (score_geo+score_cnae+score_situacao+score_porte) DESC) AS rn
            FROM scored
        )
        SELECT * FROM ranked
        WHERE rn = 1 AND score_bruto >= 30
        ORDER BY score_ajustado DESC,
                 COALESCE(lead_score, 0) DESC,
                 COALESCE(valor_estimado, 0) DESC,
                 obra_id
        LIMIT %(limite)s
    """

    conn = _db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, {
                "uf": fornec["uf"], "municipio": fornec["municipio_nome"],
                "porte": fornec["porte"], "cnae_p": fornec["cnae_principal"],
                "cnaes_sec": fornec["cnae_secundarios"], "cnaes_all": cnaes_all,
                "limite": limite,
            })
            obras = cur.fetchall()

            cnpjs = [o["obra_cnpj"] for o in obras]
            decisores_map: Dict[str, Dict[str, Any]] = {}
            if cnpjs:
                cur.execute("""
                    SELECT d.cnpj, d.nome_pessoa, d.cargo_raw, d.email
                    FROM empresa_decisores_cache d
                    WHERE d.cnpj = ANY(%s)
                      AND d.trabalha_atualmente = true
                      AND d.email_status = 'verified_smtp'
                      AND d.excluido_em IS NULL
                    ORDER BY d.filtro_llm_confianca DESC NULLS LAST
                """, (cnpjs,))
                for row in cur.fetchall():
                    decisores_map.setdefault(row["cnpj"], {
                        "nome": row["nome_pessoa"],
                        "cargo": row["cargo_raw"],
                        "email": row["email"],
                    })
    finally:
        conn.close()

    resultados: List[MatchResult] = []
    for o in obras:
        razao = _gerar_razao_haiku(dict(o), fornec)
        resultados.append(MatchResult(
            obra_id=str(o["obra_id"]),
            obra_nome=o["obra_nome"],
            obra_valor_formatado=o["obra_valor_formatado"],
            obra_fase=o["obra_fase"],
            obra_uf=o["obra_uf"],
            empresa_contratante=o["empresa_contratante"],
            score=o["score_ajustado"],
            score_breakdown={
                "geo": o["score_geo"], "cnae": o["score_cnae"],
                "situacao": o["score_situacao"], "porte": o["score_porte"],
                "bruto": o["score_bruto"],
            },
            razao_match=razao,
            decisor_associado=decisores_map.get(o["obra_cnpj"]),
            tem_decisor_cacheado=bool(decisores_map.get(o["obra_cnpj"])),
        ))

    return AutoMatchResponse(
        fornecedor_nome=fornec["razao_social"],
        buscado_em=datetime.utcnow().isoformat(),
        creditos_consumidos_mock=1,
        custo_simulado_brl=4.90,
        total_matches=len(resultados),
        resultados=resultados,
    )


@router.get("/info")
async def info_fornecedor():
    return {
        "razao_social": FORNECEDOR_DEMO["razao_social"],
        "cnpj": FORNECEDOR_DEMO["cnpj"],
        "uf": FORNECEDOR_DEMO["uf"],
        "especialidades": FORNECEDOR_DEMO["especialidades"],
        "referencias": FORNECEDOR_DEMO["referencias"],
    }
