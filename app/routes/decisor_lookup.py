"""Decisor lookup async (P3.1 ao vivo) com job_id + polling status.

POST /api/auto-match/decisor/buscar  body {obra_id}  -> {job_id, status:'iniciado'}
GET  /api/auto-match/status/{job_id} -> {etapa, progresso, mensagem, decisores_count, ...}

Job state persistido em decisor_jobs (Postgres) — funciona com multiplos uvicorn workers.
TTL 5min via WHERE atualizado_em > NOW() - INTERVAL '5 minutes'.
Wallet debita SOMENTE em sucesso (>=1 decisor). Hunter quota intocada (permitir_hunter=False).
"""
import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from psycopg2.extras import RealDictCursor, Json

log = logging.getLogger("decisor_lookup")

CUSTO_DECISOR_CENTAVOS = 1000  # R$ 10,00


def _ensure_table(get_conn):
    """Cria tabela decisor_jobs idempotentemente."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS decisor_jobs (
                    job_id           uuid PRIMARY KEY,
                    user_id          uuid NOT NULL,
                    obra_id          uuid,
                    cnpj             text,
                    etapa            text NOT NULL DEFAULT 'iniciado',
                    progresso        int  NOT NULL DEFAULT 5,
                    mensagem         text NOT NULL DEFAULT 'Iniciando...',
                    decisores_count  int  NOT NULL DEFAULT 0,
                    decisores        jsonb,
                    erro             text,
                    criado_em        timestamptz NOT NULL DEFAULT now(),
                    atualizado_em    timestamptz NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS decisor_jobs_user_atualizado_idx
                  ON decisor_jobs (user_id, atualizado_em DESC)
            """)
        conn.commit()
    finally:
        conn.close()


def _insert_job(get_conn, job_id: str, user_id: str, obra_id: str, cnpj: str) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO decisor_jobs (job_id, user_id, obra_id, cnpj, etapa, progresso, mensagem)
                VALUES (%s, %s, %s, %s, 'iniciado', 5, 'Preparando busca...')
            """, (job_id, user_id, obra_id, cnpj))
        conn.commit()
    finally:
        conn.close()


def _update_job(get_conn, job_id: str, **fields) -> None:
    if not fields:
        return
    set_parts = []
    vals: list = []
    for k, v in fields.items():
        if k == "decisores":
            set_parts.append("decisores = %s")
            vals.append(Json(v))
        else:
            set_parts.append(f"{k} = %s")
            vals.append(v)
    set_parts.append("atualizado_em = now()")
    vals.append(job_id)
    sql = f"UPDATE decisor_jobs SET {', '.join(set_parts)} WHERE job_id = %s"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, vals)
        conn.commit()
    finally:
        conn.close()


def _read_job(get_conn, job_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT job_id::text, user_id::text, obra_id::text, cnpj,
                       etapa, progresso, mensagem, decisores_count,
                       decisores, erro,
                       criado_em, atualizado_em
                FROM decisor_jobs
                WHERE job_id = %s AND atualizado_em > NOW() - INTERVAL '5 minutes'
            """, (job_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _buscar_dominio_inline(conn, cnpj: str, empresa_nome: Optional[str]) -> Optional[str]:
    """Lookup empresa_dominios; se vazio, tenta descobrir via chain e persiste."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT dominio, holding_dominio FROM empresa_dominios WHERE cnpj=%s",
            (cnpj,),
        )
        row = cur.fetchone()
        if row:
            dom = row.get("dominio") or row.get("holding_dominio")
            if dom:
                return dom
    if not empresa_nome:
        return None
    try:
        from sales_intelligence.camada1_identificacao.descobrir_dominio import descobrir_dominio_via_chain
        dom = descobrir_dominio_via_chain(empresa_nome)
    except Exception as e:
        log.warning(f"descobrir_dominio_via_chain falhou cnpj={cnpj}: {e}")
        return None
    if not dom:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca, dominio_status)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (cnpj) DO UPDATE SET
                    dominio = COALESCE(empresa_dominios.dominio, EXCLUDED.dominio),
                    fonte = COALESCE(empresa_dominios.fonte, EXCLUDED.fonte),
                    atualizado_em = NOW()
            """, (cnpj, (empresa_nome or "")[:255], dom, "chain_descoberto", 3, "ok"))
        conn.commit()
    except Exception as e:
        log.warning(f"persist empresa_dominios falhou cnpj={cnpj}: {e}")
    return dom


class BuscarDecisorReq(BaseModel):
    obra_id: str


def build_decisor_lookup_router(get_conn, requer_auth):
    _ensure_table(get_conn)

    router = APIRouter(prefix="/api/auto-match", tags=["decisor-lookup"])

    @router.post("/decisor/buscar")
    async def buscar_decisor(req: BuscarDecisorReq, request: Request, u=Depends(requer_auth)):
        user_id = u["sub"]

        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT COALESCE(creditos_ganhos,0)-COALESCE(creditos_consumidos,0) AS saldo "
                    "FROM prestadores WHERE id=%s",
                    (user_id,),
                )
                row = cur.fetchone()
                saldo = int((row or {}).get("saldo") or 0)
                if saldo < CUSTO_DECISOR_CENTAVOS:
                    raise HTTPException(402, {
                        "motivo": "saldo_insuficiente",
                        "saldo_centavos": saldo,
                        "custo_centavos": CUSTO_DECISOR_CENTAVOS,
                    })
                cur.execute(
                    "SELECT id, nome, empresa, cnpj FROM obras WHERE id=%s",
                    (req.obra_id,),
                )
                obra = cur.fetchone()
                if not obra:
                    raise HTTPException(404, "Obra nao encontrada")
                cnpj_raw = (obra.get("cnpj") or "").strip()
                cnpj_clean = "".join(c for c in cnpj_raw if c.isdigit())
                if len(cnpj_clean) != 14:
                    raise HTTPException(400, "Obra sem CNPJ valido")
                empresa = obra.get("empresa") or ""
                obra_id_str = str(obra["id"])
        finally:
            conn.close()

        job_id = str(uuid.uuid4())
        _insert_job(get_conn, job_id, user_id, obra_id_str, cnpj_clean)
        asyncio.create_task(_executar_p31_job(job_id, cnpj_clean, empresa, user_id, get_conn))
        log.info(f"decisor_lookup iniciado job={job_id} user={user_id} cnpj={cnpj_clean}")
        return {"job_id": job_id, "status": "iniciado"}

    @router.get("/status/{job_id}")
    async def status_job(job_id: str, u=Depends(requer_auth)):
        try:
            uuid.UUID(job_id)
        except (ValueError, AttributeError):
            raise HTTPException(404, "Job nao encontrado ou expirado")
        job = _read_job(get_conn, job_id)
        if not job:
            raise HTTPException(404, "Job nao encontrado ou expirado")
        if job.get("user_id") != u["sub"]:
            raise HTTPException(403, "Job de outro usuario")
        resp = {
            "etapa": job["etapa"],
            "progresso": job["progresso"],
            "mensagem": job["mensagem"],
            "decisores_count": job["decisores_count"],
        }
        if job["etapa"] == "pronto":
            resp["decisores"] = job.get("decisores") or []
        if job.get("erro"):
            resp["erro"] = job["erro"]
        return resp

    return router


async def _executar_p31_job(job_id: str, cnpj: str, empresa: str, user_id: str, get_conn) -> None:
    """Rota: dominio -> search -> email -> persist + debit wallet (em sucesso)."""
    try:
        _update_job(get_conn, job_id,
                    etapa="dominio", progresso=15,
                    mensagem="Buscando dominio da empresa...")
        conn = get_conn()
        try:
            dominio = await asyncio.to_thread(_buscar_dominio_inline, conn, cnpj, empresa)
        finally:
            conn.close()

        _update_job(get_conn, job_id,
                    etapa="search", progresso=40,
                    mensagem="Procurando decisores em fontes publicas...")
        from sales_intelligence.camada3_decisores.orquestrador import descobrir_decisores
        decisores = await asyncio.to_thread(descobrir_decisores, cnpj, empresa, False)

        _update_job(get_conn, job_id,
                    etapa="email", progresso=75,
                    mensagem="Validando emails...")
        if dominio and decisores:
            from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
            decisores = await asyncio.to_thread(
                enriquecer_decisores_com_email,
                cnpj, dominio, decisores, None, False,
            )

        from sales_intelligence.db.cache_decisores import gravar_decisor
        for d in (decisores or []):
            try:
                await asyncio.to_thread(gravar_decisor, d)
            except Exception as e:
                log.warning(f"gravar_decisor falhou: {e}")

        decisores_export = []
        for idx, d in enumerate(decisores or []):
            if not getattr(d, "nome_pessoa", None):
                continue
            slug = getattr(d, "linkedin_slug", None)
            decisores_export.append({
                "id": f"live_{idx}",
                "tipo_cargo": "OUTRO",
                "label": "Decisor",
                "nome": d.nome_pessoa,
                "cargo": getattr(d, "cargo_raw", "") or "",
                "email": getattr(d, "email", None),
                "email_status": getattr(d, "email_status", None),
                "linkedin": (f"https://linkedin.com/in/{slug}" if slug else None),
                "telefone": None,
                "fonte": "AUTO_MATCH",
                "is_auto_descoberto": True,
                "confianca": getattr(d, "confianca", None),
            })

        if decisores_export:
            try:
                conn_w = get_conn()
                try:
                    with conn_w.cursor() as cur:
                        cur.execute(
                            "UPDATE prestadores SET creditos_consumidos = COALESCE(creditos_consumidos,0) + %s WHERE id=%s",
                            (CUSTO_DECISOR_CENTAVOS, user_id),
                        )
                        cur.execute(
                            "INSERT INTO interacoes (prestador_id, tipo, plano_momento, valor_cobrado) "
                            "VALUES (%s, %s, %s, %s)",
                            (user_id, "DECISOR_LIVE", "PREMIUM", CUSTO_DECISOR_CENTAVOS / 100.0),
                        )
                    conn_w.commit()
                finally:
                    conn_w.close()
            except Exception as e:
                log.error(f"debit wallet falhou job={job_id}: {e}")

        _update_job(get_conn, job_id,
                    etapa="pronto", progresso=100,
                    mensagem=(f"Pronto! {len(decisores_export)} decisor(es) encontrado(s)."
                              if decisores_export else "Nenhum decisor encontrado nas fontes publicas."),
                    decisores_count=len(decisores_export),
                    decisores=decisores_export)
        log.info(f"decisor_lookup pronto job={job_id} n={len(decisores_export)}")
    except Exception as e:
        log.exception(f"p31 job={job_id} falhou: {e}")
        try:
            _update_job(get_conn, job_id,
                        etapa="erro", progresso=0,
                        mensagem="Nao foi possivel encontrar decisores agora. Tente novamente.",
                        erro=str(e)[:200])
        except Exception as e2:
            log.error(f"falha registrar erro job={job_id}: {e2}")
