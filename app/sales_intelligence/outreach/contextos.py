"""Extrai contexto (obra-gancho + fornecedor) do banco para Outreach."""
import os
import logging
from typing import Optional, Dict

import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger("outreach.contextos")

FASE_HUMANO = {
    "EM_EXECUCAO": "em execucao",
    "PLANEJAMENTO": "em planejamento",
    "LICENCA_INSTALACAO": "com licenca de instalacao",
    "LICENCA_PREVIA": "com licenca previa",
    "PROJETO": "em fase de projeto",
    "LICITACAO_ABERTA": "com licitacao aberta",
    "OPERACAO": "em operacao",
    "CONCLUIDA": "concluida",
}


def _db_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def buscar_obra_top_pro_decisor(cnpj_empresa: str) -> Optional[Dict]:
    """Encontra a obra mais relevante (maior valor + lead_score) pra cnpj.

    Filtra fases ativas (exclui OPERACAO e CONCLUIDA).
    Retorna dict com obra_nome, obra_valor_formatado, obra_fase, obra_descricao, obra_uf.
    """
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    nome AS obra_nome,
                    valor_formatado AS obra_valor_formatado,
                    fase AS fase_enum,
                    LEFT(descricao, 400) AS obra_descricao,
                    uf AS obra_uf
                FROM obras
                WHERE cnpj = %s
                  AND visivel = true
                  AND fase IN ('EM_EXECUCAO', 'PLANEJAMENTO', 'LICENCA_INSTALACAO',
                               'LICENCA_PREVIA', 'PROJETO', 'LICITACAO_ABERTA')
                  AND valor_estimado IS NOT NULL
                ORDER BY lead_score DESC NULLS LAST, valor_estimado DESC NULLS LAST
                LIMIT 1
            """, (cnpj_empresa,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return None

    return {
        "obra_nome": row["obra_nome"],
        "obra_valor_formatado": row["obra_valor_formatado"] or "(valor nao informado)",
        "obra_fase": FASE_HUMANO.get(row["fase_enum"], row["fase_enum"].lower()),
        "obra_descricao": row["obra_descricao"],
        "obra_uf": row["obra_uf"],
    }


def buscar_fornecedor_piloto() -> Dict:
    """Cliente piloto hardcoded — exemplo demo.

    Em producao real, virá do cadastro de clientes WNS Hub.
    """
    return {
        "fornecedor_nome": "EMPRESA DEMO",
        "fornecedor_servicos": [
            "Consultoria geotecnica",
            "Sondagens e investigacoes de subsolo",
            "Estudos hidrogeologicos",
            "Monitoramento ambiental",
        ],
        "fornecedor_referencias": [
            "Aeroporto Galeao R$19bi (sondagens fundacao)",
            "Vale Carajas (instrumentacao geotecnica)",
            "CCR ViaSul (pacotes geotecnicos)",
        ],
    }
