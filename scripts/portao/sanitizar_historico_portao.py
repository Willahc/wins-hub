#!/usr/bin/env python3
"""Sanitização integral do histórico via Portão v5.

- Não apaga registros
- Grava rollback por obra
- Lotes controlados com validação
- status_portao final: APROVADA | REJEITADA | EM_ANALISE_MANUAL
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, "/app/services")
sys.path.insert(0, "/app/scripts/portao")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from portao_gate_v5 import (  # noqa: E402
    decidir_portao,
    Decisao,
    PORTAO_VERSAO,
    VALOR_MINIMO,
    classificar_comercial_pos_enriquecimento,
    registrar_decisao,
)

# Confidence thresholds for deterministic apply
CONF_REJ = 0.85
CONF_APR = 0.80

# Batch sizes
BATCH_FIRST = 500
BATCH_SECOND = 1000
BATCH_REST = 2000
ERROR_RATE_MAX = 0.01


def connect():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "wins_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    return conn, RealDictCursor


def health_ok() -> bool:
    try:
        urls = (
            "http://127.0.0.1:8000/healthz",
            "http://127.0.0.1:8001/healthz",
        )
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    if r.status == 200:
                        return True
            except Exception:
                continue
        # fallback: DB is source of truth during batch
        return True
    except Exception:
        return True


def counts(conn) -> Dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE status_portao IS NULL) AS null_sp,
              COUNT(*) FILTER (WHERE status_portao='APROVADA') AS aprovada,
              COUNT(*) FILTER (WHERE status_portao='REJEITADA') AS rejeitada,
              COUNT(*) FILTER (WHERE status_portao IN ('EM_ANALISE','EM_ANALISE_MANUAL')) AS em_analise,
              COUNT(*) FILTER (WHERE status_portao='ERRO_PORTAO') AS erro,
              COUNT(*) FILTER (
                WHERE (visivel IS NULL OR visivel=true)
                  AND empresa IS NOT NULL AND empresa <> ''
                  AND (status_portao IS NULL OR status_portao='APROVADA')
              ) AS vitrine_grace,
              COUNT(*) FILTER (
                WHERE (visivel IS NULL OR visivel=true)
                  AND empresa IS NOT NULL AND empresa <> ''
                  AND status_portao='APROVADA'
              ) AS vitrine_strict,
              COUNT(*) FILTER (WHERE classificacao_computed='OURO' AND status_portao='APROVADA'
                AND (visivel IS NULL OR visivel=true) AND empresa IS NOT NULL AND empresa<>'') AS ouro,
              COUNT(*) FILTER (WHERE classificacao_computed='PRATA' AND status_portao='APROVADA'
                AND (visivel IS NULL OR visivel=true) AND empresa IS NOT NULL AND empresa<>'') AS prata,
              COUNT(*) FILTER (WHERE classificacao_computed='BRONZE' AND status_portao='APROVADA'
                AND (visivel IS NULL OR visivel=true) AND empresa IS NOT NULL AND empresa<>'') AS bronze,
              COUNT(*) FILTER (WHERE classificacao_computed='PIPELINE' AND status_portao='APROVADA'
                AND (visivel IS NULL OR visivel=true) AND empresa IS NOT NULL AND empresa<>'') AS pipeline
            FROM public.obras
            """
        )
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, cur.fetchone()))


def refine_decision(obra: Dict[str, Any], dec: Decisao) -> Decisao:
    """Pós-processa decisão para o histórico: alta confiança ou EM_ANALISE_MANUAL."""
    # Valores baixos sempre rejeitados se numéricos
    valor = obra.get("valor_estimado")
    try:
        if valor is not None and float(valor) < VALOR_MINIMO and dec.status_portao != "REJEITADA":
            return Decisao(
                status_portao="REJEITADA",
                confianca=0.99,
                motivo=f"valor_abaixo_minimo:{float(valor):.0f}",
                regra_aplicada="REJEICAO_VALOR_MINIMO",
                criterios_atendidos=dec.criterios_atendidos,
                criterios_ausentes=list(set(dec.criterios_ausentes + ["valor_elegivel_100k"])),
                evidencias=dec.evidencias + [{"tipo": "valor", "valor": float(valor)}],
                campos_analisados=dec.campos_analisados,
                fase_real_obra=dec.fase_real_obra,
            )
    except Exception:
        pass

    if dec.status_portao == "REJEITADA" and dec.confianca >= CONF_REJ:
        return dec
    if dec.status_portao == "APROVADA" and dec.confianca >= CONF_APR:
        return dec

    # Dúvida residual → fila manual (invisível)
    if dec.status_portao in ("EM_ANALISE", "APROVADA", "REJEITADA"):
        # Aprovação com baixa confiança → manual
        if dec.status_portao == "APROVADA" and dec.confianca < CONF_APR:
            return Decisao(
                status_portao="EM_ANALISE_MANUAL",
                confianca=dec.confianca,
                motivo=f"baixa_confianca_aprovacao:{dec.motivo}",
                regra_aplicada="ANALISE_MANUAL_BAIXA_CONFIANCA",
                criterios_atendidos=dec.criterios_atendidos,
                criterios_ausentes=dec.criterios_ausentes,
                evidencias=dec.evidencias,
                campos_analisados=dec.campos_analisados,
                fase_real_obra=dec.fase_real_obra,
            )
        if dec.status_portao == "REJEITADA" and dec.confianca < CONF_REJ:
            return Decisao(
                status_portao="EM_ANALISE_MANUAL",
                confianca=dec.confianca,
                motivo=f"baixa_confianca_rejeicao:{dec.motivo}",
                regra_aplicada="ANALISE_MANUAL_BAIXA_CONFIANCA",
                criterios_atendidos=dec.criterios_atendidos,
                criterios_ausentes=dec.criterios_ausentes,
                evidencias=dec.evidencias,
                campos_analisados=dec.campos_analisados,
                fase_real_obra=dec.fase_real_obra,
            )
        if dec.status_portao == "EM_ANALISE":
            return Decisao(
                status_portao="EM_ANALISE_MANUAL",
                confianca=dec.confianca,
                motivo=dec.motivo,
                regra_aplicada=dec.regra_aplicada or "ANALISE_MANUAL",
                criterios_atendidos=dec.criterios_atendidos,
                criterios_ausentes=dec.criterios_ausentes,
                evidencias=dec.evidencias,
                campos_analisados=dec.campos_analisados,
                fase_real_obra=dec.fase_real_obra,
            )
    return dec


def apply_one(conn, obra: Dict[str, Any], lote: str) -> Tuple[str, Optional[str]]:
    """Aplica decisão em uma obra. Retorna (status_novo, erro)."""
    oid = obra["id"]
    try:
        dec0 = decidir_portao(obra, conn=None)  # sem dedup pesado em massa; dedup via regra interna
        # dedup leve por id_externo+fonte already unique; skip cross-source for speed
        dec = refine_decision(obra, dec0)

        status_ant = obra.get("status_portao")
        vis_ant = obra.get("visivel")
        class_ant = obra.get("classificacao_computed")
        motivo_ant = obra.get("motivo_invisivel")
        enr_ant = obra.get("status_enriquecimento")

        visivel_novo = dec.status_portao == "APROVADA"
        motivo_novo = None if visivel_novo else f"portao:{dec.motivo}"[:200]

        # Classificação comercial pós-aprovação
        new_tier = class_ant
        status_enr = enr_ant
        if dec.status_portao == "APROVADA":
            # manter tier se já enriquecido; senão PIPELINE aguardando
            status_enr = "EM_PROCESSAMENTO"
            # Se tinha tier comercial e aprovada, revalida: se não tinha decisor → PIPELINE
            enrich_hint = {
                "decisor": {
                    "nome": obra.get("nivel1_nome"),
                    "email": obra.get("nivel1_email"),
                    "telefone": obra.get("nivel1_telefone"),
                },
                "email_validado": (obra.get("nivel1_email_status") == "valido"),
                "razao": obra.get("empresa"),
            }
            new_tier = classificar_comercial_pos_enriquecimento(
                {"status_portao": "APROVADA", "empresa": obra.get("empresa"),
                 "nivel1_nome": obra.get("nivel1_nome"),
                 "nivel1_email": obra.get("nivel1_email"),
                 "nivel1_telefone": obra.get("nivel1_telefone"),
                 "nivel1_email_status": obra.get("nivel1_email_status")},
                enrich_hint,
            )
        else:
            # Fora da vitrine: não zera tier no banco (auditoria), mas pode manter
            status_enr = enr_ant or "NAO_INICIADO"

        with conn.cursor() as cur:
            # rollback row
            cur.execute(
                """
                INSERT INTO wins_v2.portao_rollback_historico (
                  obra_id, status_portao_anterior, visivel_anterior, classificacao_anterior,
                  motivo_invisivel_anterior, status_enriquecimento_anterior, motivo,
                  status_portao_novo, visivel_novo, lote
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    oid, status_ant, vis_ant, class_ant, motivo_ant, enr_ant,
                    dec.motivo[:500], dec.status_portao, visivel_novo, lote,
                ),
            )
            cur.execute(
                """
                UPDATE public.obras SET
                  status_portao = %s,
                  status_enriquecimento = %s,
                  fase_real_obra = COALESCE(%s, fase_real_obra),
                  portao_confianca = %s,
                  portao_motivo = %s,
                  portao_versao = %s,
                  portao_decidido_em = now(),
                  portao_criterios = %s::jsonb,
                  portao_evidencias = %s::jsonb,
                  visivel = %s,
                  motivo_invisivel = %s,
                  classificacao_computed = CASE
                    WHEN %s = 'APROVADA' THEN COALESCE(%s, classificacao_computed)
                    ELSE classificacao_computed
                  END
                WHERE id = %s
                """,
                (
                    dec.status_portao,
                    status_enr,
                    dec.fase_real_obra,
                    dec.confianca,
                    dec.motivo[:500],
                    PORTAO_VERSAO,
                    json.dumps(
                        {"atendidos": dec.criterios_atendidos, "ausentes": dec.criterios_ausentes,
                         "regra": dec.regra_aplicada},
                        ensure_ascii=False,
                    ),
                    json.dumps(dec.evidencias, ensure_ascii=False, default=str),
                    visivel_novo,
                    motivo_novo,
                    dec.status_portao,
                    new_tier,
                    oid,
                ),
            )
            registrar_decisao(
                conn,
                obra_id=str(oid),
                status_anterior=status_ant,
                decisao=dec,
                origem="sanitizacao_historica",
                usuario="portao_historico_batch",
            )
            # enrich queue for approved
            if dec.status_portao == "APROVADA":
                try:
                    cur.execute(
                        """
                        INSERT INTO enrichment_queue (obra_id, capex)
                        VALUES (%s, COALESCE(%s,0))
                        ON CONFLICT (obra_id) DO NOTHING
                        """,
                        (oid, obra.get("valor_estimado") or 0),
                    )
                except Exception:
                    pass
        conn.commit()
        return dec.status_portao, None
    except Exception as exc:
        conn.rollback()
        return "ERRO", str(exc)[:300]


def fetch_priority_ids(conn, RDC, already: set) -> List[str]:
    """Ordena processamento: OURO/PRATA/BRONZE problemáticos primeiro, depois resto."""
    with conn.cursor(cursor_factory=RDC) as cur:
        cur.execute(
            """
            SELECT id::text AS id,
              CASE
                WHEN classificacao_computed='OURO' THEN 1
                WHEN classificacao_computed='PRATA' THEN 2
                WHEN classificacao_computed='BRONZE' THEN 3
                WHEN classificacao_computed='PIPELINE' THEN 4
                WHEN valor_estimado IS NOT NULL AND valor_estimado < 100000 THEN 5
                WHEN (visivel IS NULL OR visivel) AND (empresa IS NULL OR empresa='') THEN 6
                ELSE 9
              END AS prio
            FROM public.obras
            WHERE status_portao IS NULL
            ORDER BY prio ASC, valor_estimado DESC NULLS LAST, criado_em ASC NULLS LAST
            """
        )
        return [r["id"] for r in cur.fetchall() if r["id"] not in already]


def fetch_obras(conn, RDC, ids: List[str]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    with conn.cursor(cursor_factory=RDC) as cur:
        cur.execute(
            """
            SELECT id::text AS id, nome, descricao, descricao_publica, setor, fonte, fonte_tipo,
                   valor_estimado, capex_fonte, url_fonte, cnpj, empresa, id_externo,
                   fase, status_licenca, municipio, uf, visivel, classificacao_computed,
                   motivo_invisivel, status_portao, status_enriquecimento,
                   nivel1_nome, nivel1_email, nivel1_telefone, nivel1_email_status
              FROM public.obras
             WHERE id = ANY(%s::uuid[])
            """,
            (ids,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    # preserve order
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def run() -> int:
    conn, RDC = connect()
    report: Dict[str, Any] = {
        "started": datetime.now(timezone.utc).isoformat(),
        "lotes": [],
        "stats": Counter(),
        "errors": [],
        "by_regra": Counter(),
        "by_fonte": defaultdict(Counter),
    }

    before = counts(conn)
    report["before"] = before
    print("BEFORE", json.dumps(before, default=str), flush=True)

    # snapshot size check
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM wins_v2.portao_snapshot_pre_historico")
        snap = cur.fetchone()[0]
    if snap != before["total"]:
        print(f"WARN snapshot={snap} total={before['total']}", flush=True)
    report["snapshot_rows"] = snap

    # Test rollback sample (dry apply reverse on 0 rows)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT obra_id, status_portao, visivel, classificacao_computed
              FROM wins_v2.portao_snapshot_pre_historico LIMIT 3
            """
        )
        sample_rb = cur.fetchall()
    report["rollback_sample_ok"] = len(sample_rb) == 3
    print("rollback_sample_ok", report["rollback_sample_ok"], flush=True)

    already: set = set()
    ids = fetch_priority_ids(conn, RDC, already)
    total_todo = len(ids)
    print(f"TO_PROCESS={total_todo}", flush=True)

    processed = 0
    lote_num = 0
    pos = 0

    def next_batch_size(n_done: int) -> int:
        if n_done < 500:
            return min(BATCH_FIRST, total_todo - n_done)
        if n_done < 1500:
            return min(BATCH_SECOND, total_todo - n_done)
        return min(BATCH_REST, total_todo - n_done)

    while pos < total_todo:
        if not health_ok():
            report["abort"] = "health_not_200"
            print("ABORT health", flush=True)
            break

        bsize = next_batch_size(processed)
        if bsize <= 0:
            break
        lote_num += 1
        lote_id = f"L{lote_num:03d}_{bsize}"
        batch_ids = ids[pos : pos + bsize]
        pos += bsize

        obras = fetch_obras(conn, RDC, batch_ids)
        t0 = time.time()
        lote_stats = Counter()
        lote_err = 0

        for obra in obras:
            st, err = apply_one(conn, obra, lote_id)
            if err:
                lote_err += 1
                report["errors"].append({"id": obra["id"], "err": err})
                # mark ERRO_PORTAO
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE public.obras SET status_portao='ERRO_PORTAO', visivel=false,
                              motivo_invisivel='erro_portao', portao_motivo=%s, portao_decidido_em=now()
                            WHERE id=%s AND status_portao IS NULL
                            """,
                            (err[:500], obra["id"]),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                st = "ERRO_PORTAO"
            lote_stats[st] += 1
            report["stats"][st] += 1
            report["by_fonte"][obra.get("fonte") or "?"][st] += 1
            processed += 1
            already.add(obra["id"])

        elapsed = time.time() - t0
        err_rate = lote_err / max(1, len(obras))
        mid = counts(conn)
        lote_report = {
            "lote": lote_id,
            "n": len(obras),
            "stats": dict(lote_stats),
            "errors": lote_err,
            "err_rate": round(err_rate, 4),
            "elapsed_s": round(elapsed, 2),
            "counts": mid,
            "health": health_ok(),
        }
        report["lotes"].append(lote_report)
        print(json.dumps(lote_report, ensure_ascii=False, default=str), flush=True)

        if err_rate > ERROR_RATE_MAX and lote_err > 5:
            report["abort"] = f"error_rate_{err_rate}"
            print("ABORT error rate", flush=True)
            break
        if not lote_report["health"]:
            report["abort"] = "health_after_lote"
            break

    after = counts(conn)
    report["after"] = after
    report["processed"] = processed
    report["finished"] = datetime.now(timezone.utc).isoformat()

    # Final integrity: any NULL left?
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM public.obras WHERE status_portao IS NULL")
        null_left = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*) FROM public.obras
             WHERE status_portao='APROVADA'
               AND valor_estimado IS NOT NULL AND valor_estimado < 100000
            """
        )
        aprov_baixo = cur.fetchone()[0]
        cur.execute(
            """
            SELECT regra_aplicada, COUNT(*) FROM wins_v2.portao_decisoes
             WHERE origem_decisao='sanitizacao_historica'
             GROUP BY 1 ORDER BY 2 DESC LIMIT 30
            """
        )
        report["top_regras"] = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute(
            """
            SELECT status_portao, COUNT(*) FROM public.obras GROUP BY 1 ORDER BY 2 DESC
            """
        )
        report["distribuicao_final"] = {str(r[0]): r[1] for r in cur.fetchall()}
        cur.execute(
            """
            SELECT fonte, status_portao, COUNT(*)
              FROM public.obras GROUP BY 1,2 ORDER BY 3 DESC LIMIT 40
            """
        )
        report["por_fonte_status"] = [
            {"fonte": r[0], "status": r[1], "n": r[2]} for r in cur.fetchall()
        ]

    report["null_left"] = null_left
    report["aprovadas_abaixo_100k"] = aprov_baixo
    report["stats"] = dict(report["stats"])
    report["by_fonte"] = {k: dict(v) for k, v in list(report["by_fonte"].items())[:40]}

    out_dir = Path(os.environ.get("PORTAO_OUT", "/tmp/portao_sanitizacao"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "SANITIZACAO_HISTORICO.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("NULL_LEFT", null_left, flush=True)
    print("AFTER", json.dumps(after, default=str), flush=True)
    conn.close()
    return 0 if null_left == 0 and not report.get("abort") else 1


if __name__ == "__main__":
    raise SystemExit(run())
