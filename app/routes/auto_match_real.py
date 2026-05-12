"""Auto-Match REAL integrado: prestador logado + JWT + wallet R$10/busca."""
import json
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from psycopg2.extras import RealDictCursor

from sales_intelligence.llm_enricher.client import get_client, MODEL_HAIKU

log = logging.getLogger("auto_match_real")

CUSTO_AUTO_MATCH_CENTAVOS = 1000  # R$ 10,00

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
- Use APENAS dados do input. NAO infira datas/numeros nao-presentes.

REGRA CRITICA:
Se a obra parecer OPERACAO FINANCEIRA (BNDES, limite de credito, financiamento, aquisicao de maquinas sem obra civil), NAO tente justificar match. Responda exatamente:
"Registro financeiro/BNDES - nao e obra fisica."

Responda APENAS o texto da razao (sem JSON, sem markdown)."""


class AutoMatchReq(BaseModel):
    limite: int = 10


def _gerar_razao_haiku(obra: dict, prest: dict) -> str:
    try:
        client = get_client()
        especialidades = prest.get("especialidades_tags") or []
        referencias = prest.get("referencias_obras") or []
        r = client.messages.create(
            model=MODEL_HAIKU, max_tokens=120,
            messages=[{"role": "user", "content": RAZAO_MATCH_PROMPT.format(
                fornecedor_nome=prest.get("razao_social") or "(fornecedor)",
                fornecedor_especialidades=", ".join(especialidades) or "(nao informado)",
                fornecedor_referencias="; ".join(referencias) or "(sem referencias)",
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
        log.warning(f"haiku falhou: {e}")
        return f"(falha gerar razao: {type(e).__name__})"


def _executar_match_sql(cur, prest: dict, limite: int = 10) -> list:
    """Score 4-dim + penalty BNDES + bonus decisor + dedup."""
    cnae_p = prest.get("cnaes_primario")
    cnaes_sec = prest.get("cnaes_secundarios") or []
    cnaes_all = [cnae_p] + cnaes_sec if cnae_p else cnaes_sec
    porte = prest.get("tamanho_porte") or "MEDIO"
    porte_map = {"ME": "01", "EPP": "03", "MEDIO": "DEMAIS", "GRANDE": "DEMAIS"}
    porte_db = porte_map.get(porte, "DEMAIS")

    sql = """
        WITH prestador AS (
            SELECT %(uf)s::text AS uf, %(municipio)s::text AS municipio,
                   %(porte)s::text AS porte, %(cnae_p)s::text AS cnae_p,
                   %(cnaes_sec)s::text[] AS cnaes_sec,
                   %(cnaes_all)s::text[] AS cnaes_all
        ),
        cat_relev AS (
            SELECT c.id, c.cnaes FROM categorias_servico c, prestador p
            WHERE c.ativo AND c.cnaes && p.cnaes_all
        ),
        obra_base AS (
            SELECT DISTINCT ON (LEFT(LOWER(o.nome), 80), o.cnpj)
                   o.id AS obra_id, o.nome AS obra_nome,
                   o.valor_formatado AS obra_valor_formatado,
                   o.valor_estimado, o.fase AS obra_fase, o.uf AS obra_uf,
                   o.empresa AS empresa_contratante, o.cnpj AS obra_cnpj,
                   LEFT(o.descricao, 400) AS obra_descricao,
                   o.municipio AS obra_municipio, o.setor AS obra_setor,
                   o.lead_score,
                   CASE
                     WHEN LOWER(o.nome) LIKE '%%bndes%%' OR LOWER(o.nome) LIKE '%%limite%%credit%%'
                       OR LOWER(o.nome) LIKE '%%financiamento%%' OR LOWER(o.nome) LIKE '%%aquisicao%%maquinas%%'
                       OR LOWER(o.descricao) LIKE '%%cfi do bndes%%'
                       OR LOWER(o.descricao) LIKE '%%limites de credito%%'
                     THEN 0.3 ELSE 1.0 END AS penalty_financeiro,
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
            SELECT oc.*, p.uf AS prest_uf,
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

    cur.execute(sql, {
        "uf": prest.get("uf") or "SP",
        "municipio": "",  # nao temos municipio especifico do prestador
        "porte": porte_db,
        "cnae_p": cnae_p,
        "cnaes_sec": cnaes_sec,
        "cnaes_all": cnaes_all,
        "limite": limite,
    })
    return [dict(r) for r in cur.fetchall()]


def build_auto_match_real_router(get_conn, requer_auth):
    """Builder pattern (compativel com routes/ existentes)."""
    router = APIRouter(prefix="/api/auto-match", tags=["auto-match"])

    @router.post("/buscar")
    def auto_match_buscar(req: AutoMatchReq, request: Request, u=Depends(requer_auth)):
        """Auto-match real do prestador logado. Custo R$10 do wallet."""

        # 1. Validar plano nao-GRATUITO
        plano = (u.get("plano") or "GRATUITO").upper()
        if plano == "GRATUITO":
            raise HTTPException(403, "Recurso premium. Assine STANDARD ou PREMIUM para usar Auto-Match.")

        prestador_id = u["sub"]
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # 2. Carregar prestador completo
                cur.execute("""
                    SELECT id, razao_social, cnpj, plano, uf,
                           COALESCE(creditos_ganhos,0) - COALESCE(creditos_consumidos,0) AS saldo,
                           cnaes_primario, cnaes_secundarios, ufs_atuacao,
                           capex_min_milhoes, capex_max_milhoes,
                           especialidades_tags, referencias_obras, tamanho_porte
                    FROM prestadores
                    WHERE id = %s AND excluido_em IS NULL
                """, (prestador_id,))
                prest = cur.fetchone()
                if not prest:
                    raise HTTPException(404, "Prestador nao encontrado.")

                prest = dict(prest)

                # 3. Validar campo minimo: CNAE primario obrigatorio
                if not prest.get("cnaes_primario"):
                    raise HTTPException(400,
                        "Complete seu perfil (CNAE primario obrigatorio) antes de usar Auto-Match. Vai em /perfil.")

                # 4. Verificar saldo
                saldo = int(prest.get("saldo") or 0)
                if saldo < CUSTO_AUTO_MATCH_CENTAVOS:
                    return {
                        "requer_pagamento": True,
                        "saldo_atual_centavos": saldo,
                        "saldo_atual_reais": saldo / 100,
                        "custo_centavos": CUSTO_AUTO_MATCH_CENTAVOS,
                        "custo_reais": CUSTO_AUTO_MATCH_CENTAVOS / 100,
                        "url_recarga": "/api/pagamento/criar_preferencia",
                        "mensagem": f"Saldo R${saldo/100:.2f} insuficiente (precisa R${CUSTO_AUTO_MATCH_CENTAVOS/100:.2f}). Recarregue para continuar.",
                    }

                # 5. Executar SQL match
                obras = _executar_match_sql(cur, prest, limite=req.limite)

                # 6. Buscar decisores production-ready
                cnpjs = list({o["obra_cnpj"] for o in obras})
                decisores_map = {}
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

                # 7. Gerar razoes via Haiku
                resultados = []
                for o in obras:
                    razao = _gerar_razao_haiku(o, prest)
                    resultados.append({
                        "obra_id": str(o["obra_id"]),
                        "obra_nome": o["obra_nome"],
                        "obra_valor_formatado": o["obra_valor_formatado"],
                        "obra_fase": o["obra_fase"],
                        "obra_uf": o["obra_uf"],
                        "empresa_contratante": o["empresa_contratante"],
                        "score": o["score_ajustado"],
                        "score_breakdown": {
                            "geo": o["score_geo"], "cnae": o["score_cnae"],
                            "situacao": o["score_situacao"], "porte": o["score_porte"],
                            "bruto": o["score_bruto"],
                        },
                        "razao_match": razao,
                        "tem_decisor_cacheado": bool(decisores_map.get(o["obra_cnpj"])),
                    })

                # 8. TRANSACAO ATOMICA: persist busca + debit + audit
                criterios_input = {
                    "cnaes_primario": prest.get("cnaes_primario"),
                    "cnaes_secundarios": prest.get("cnaes_secundarios") or [],
                    "ufs_atuacao": prest.get("ufs_atuacao") or [],
                    "porte": prest.get("tamanho_porte"),
                    "especialidades_tags": prest.get("especialidades_tags") or [],
                    "limite": req.limite,
                }
                cur.execute("""
                    INSERT INTO auto_match_buscas
                      (prestador_id, cnpj_prestador, criterios_input, resultados_output,
                       custo_centavos, ip_origem, user_agent)
                    VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                    RETURNING id
                """, (prestador_id, prest.get("cnpj"),
                      json.dumps(criterios_input),
                      json.dumps(resultados, default=str),
                      CUSTO_AUTO_MATCH_CENTAVOS,
                      request.client.host if request.client else None,
                      (request.headers.get("user-agent") or "")[:500]))
                busca_id = cur.fetchone()["id"]

                cur.execute(
                    "UPDATE prestadores SET creditos_consumidos = COALESCE(creditos_consumidos,0) + %s WHERE id = %s",
                    (CUSTO_AUTO_MATCH_CENTAVOS, prestador_id),
                )
                cur.execute(
                    "INSERT INTO interacoes (prestador_id, tipo, plano_momento, valor_cobrado) "
                    "VALUES (%s, %s, %s, %s)",
                    (prestador_id, "AUTO_MATCH", plano, CUSTO_AUTO_MATCH_CENTAVOS / 100.0),
                )
            conn.commit()
        finally:
            conn.close()

        log.info(f"auto_match buscar prestador={prestador_id} busca_id={busca_id} "
                 f"resultados={len(resultados)} custo_centavos={CUSTO_AUTO_MATCH_CENTAVOS}")

        return {
            "busca_id": busca_id,
            "saldo_anterior_centavos": saldo,
            "saldo_anterior_reais": saldo / 100,
            "saldo_atual_centavos": saldo - CUSTO_AUTO_MATCH_CENTAVOS,
            "saldo_atual_reais": (saldo - CUSTO_AUTO_MATCH_CENTAVOS) / 100,
            "custo_centavos": CUSTO_AUTO_MATCH_CENTAVOS,
            "custo_reais": CUSTO_AUTO_MATCH_CENTAVOS / 100,
            "total_resultados": len(resultados),
            "resultados": resultados,
        }

    @router.get("/historico")
    def auto_match_historico(u=Depends(requer_auth), limit: int = 20):
        """Retorna ultimas N buscas do prestador."""
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, criterios_input, resultados_output,
                           custo_centavos, debitado_em
                    FROM auto_match_buscas
                    WHERE prestador_id = %s
                    ORDER BY debitado_em DESC
                    LIMIT %s
                """, (u["sub"], limit))
                rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        return {"buscas": rows}


    @router.get("/ultima")
    def auto_match_ultima(u=Depends(requer_auth)):
        """Retorna ultima busca do prestador (visualizacao GRATUITA)."""
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, criterios_input, resultados_output, custo_centavos, debitado_em
                    FROM auto_match_buscas
                    WHERE prestador_id = %s
                    ORDER BY debitado_em DESC
                    LIMIT 1
                """, (u["sub"],))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return {"tem_busca": False}
        from datetime import datetime, timezone
        delta = datetime.now(timezone.utc) - row["debitado_em"]
        secs = delta.total_seconds()
        if secs < 60:
            tempo_txt = "agora há pouco"
        elif secs < 3600:
            tempo_txt = f"há {int(secs/60)} min"
        elif secs < 86400:
            tempo_txt = f"há {int(secs/3600)} h"
        else:
            tempo_txt = f"há {int(secs/86400)} dia(s)"
        return {
            "tem_busca": True,
            "busca_id": row["id"],
            "buscado_em": row["debitado_em"].isoformat(),
            "tempo_txt": tempo_txt,
            "resultados": row["resultados_output"] or [],
        }

    return router
