"""
validar_obras_ativas.py — Validação semanal de obras ativas (saída de OURO/PRATA
quando fonte oficial deslista a obra).

Roda domingos 04:00 BRT via cron host (07:00 UTC). Duração esperada ~2-3s.

Lógica em 3 estados (sequencial entre semanas):

  ESTADO 1 (dia 0):  obra_listada_na_fonte=false detectada pela 1ª vez
                    → marca flag 'deslistada_detectada_YYYY-MM-DD'
                    → mantém tier
  ESTADO 2 (dia 7):  ainda deslistada após 7d
                    → OURO/PRATA → PIPELINE
                    → flag 'rebaixada_deslistada_7d_YYYY-MM-DD'
  ESTADO 3 (dia 14): ainda deslistada após 14d
                    → visivel=false, classificacao_computed=REJEITADO
                    → motivo_invisivel='auto_deslistada_14d_YYYY-MM-DD'

Proteções:
  - Só atua em obras de fontes oficiais reguladas (FONTES_OFICIAIS abaixo)
  - Se captador da fonte FALHOU no último ciclo orchestrator → SKIP (não rebaixa)
  - --dry-run mostra plano sem mexer; --commit aplica
  - Só rebaixa se NÃO existir outra obra OURO listada=true do mesmo CNPJ

Uso:
    docker exec wins_hub-api-1 python /app/scripts/validar_obras_ativas.py            # dry-run (default)
    docker exec wins_hub-api-1 python /app/scripts/validar_obras_ativas.py --commit   # aplica

STATS_JSON na última linha (parseado pelo orchestrator/log_captacao).
"""
from __future__ import annotations

import argparse
import atexit as _atexit
import json as _stats_json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2


BRT = ZoneInfo("America/Sao_Paulo")

# Mapeia fonte da obra (obras.fonte) → nome do captador (log_captacao.fonte).
# Validação pula a fonte se o captador correspondente falhou no último ciclo.
FONTE_OBRA_TO_CAPTADOR = {
    "aneel_siga":          "captar_aneel",
    "aneel_transmissao":   "captar_aneel_transmissao",
    "antaq_tup":           "captar_antaq",
    "antt_ferro_pic":      "captar_antt_ferro_pic",
    "bndes_financiamento": "captar_bndes",
    "pncp_obras":          "captar_pncp_obras",
    "pncp_consulta":       "captar_pncp_consulta",
}
FONTES_OFICIAIS = tuple(FONTE_OBRA_TO_CAPTADOR.keys())

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("validar_obras_ativas")

_STATS = {
    "fontes_protegidas": 0,
    "estado1_detectada":  0,
    "estado2_rebaixada":  0,
    "estado3_invisivel":  0,
    "obras_processadas":  0,
    "erros":              0,
}


def _emit_stats_json():
    try:
        print(f"STATS_JSON: {_stats_json.dumps(_STATS)}", flush=True)
    except Exception:
        pass


_atexit.register(_emit_stats_json)


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def fontes_ok_no_ciclo(cur) -> list[str]:
    """
    Retorna lista de fontes oficiais cujo captador rodou SEM erro no último
    ciclo orchestrator (últimas 24h). Captador que falhou → pula validação
    daquela fonte pra evitar falso-positivo por dataset corrompido/offline.
    """
    cur.execute("""
        WITH ultimas AS (
            SELECT fonte,
                   status,
                   ROW_NUMBER() OVER (PARTITION BY fonte ORDER BY criado_em DESC) AS rk
            FROM log_captacao
            WHERE criado_em >= NOW() - INTERVAL '24 hours'
              AND fonte = ANY(%s)
        )
        SELECT fonte FROM ultimas WHERE rk = 1 AND status = 'sucesso'
    """, ([f"captar_{f}".replace("captar_pncp_", "captar_pncp_")
           if not f.startswith("captar_") else f
           for f in FONTES_OFICIAIS] + list(FONTES_OFICIAIS),))
    return [r[0] for r in cur.fetchall()]


def log_captacao(conn, status: str, novos: int = 0, buscados: int = 0,
                 erro: str | None = None, duracao_ms: int = 0, *, dry_run: bool = False) -> None:
    if dry_run:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO log_captacao (fonte, status, buscados, novos, erro, duracao_ms)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, ("VALIDAR_OBRAS_ATIVAS", status, buscados, novos, erro, duracao_ms))
        conn.commit()
    except Exception as e:
        log.warning(f"falha gravando log_captacao: {e}")


def main():
    ap = argparse.ArgumentParser(description="Validação semanal de obras ativas")
    ap.add_argument("--commit", action="store_true",
                    help="Aplica mudanças. Sem essa flag = dry-run (default).")
    args = ap.parse_args()
    commit = args.commit

    log.info(f"=== VALIDAR_OBRAS_ATIVAS INICIO {'(COMMIT)' if commit else '(DRY-RUN)'} ===")
    log.info(f"hora atual BRT: {datetime.now(BRT).strftime('%Y-%m-%d %H:%M:%S %Z')}")

    t0 = time.time()
    conn = get_conn()
    cur = conn.cursor()

    # ── 1. Proteção: identificar fontes cujo captador rodou OK no último ciclo
    captadores = list(FONTE_OBRA_TO_CAPTADOR.values())
    cur.execute("""
        WITH ultimas AS (
            SELECT fonte, status,
                   ROW_NUMBER() OVER (PARTITION BY fonte ORDER BY criado_em DESC) AS rk
            FROM log_captacao
            WHERE criado_em >= NOW() - INTERVAL '24 hours'
              AND fonte = ANY(%s)
        )
        SELECT fonte, status FROM ultimas WHERE rk = 1
    """, (captadores,))
    status_captador = {r[0]: r[1] for r in cur.fetchall()}

    fontes_ativas = [
        fonte for fonte, captador in FONTE_OBRA_TO_CAPTADOR.items()
        if status_captador.get(captador) == 'sucesso'
    ]
    fontes_protegidas = [f for f in FONTES_OFICIAIS if f not in fontes_ativas]
    _STATS["fontes_protegidas"] = len(fontes_protegidas)
    log.info(f"fontes oficiais com captador OK: {fontes_ativas}")
    if fontes_protegidas:
        log.warning(f"fontes PROTEGIDAS (captador falhou/sem-log): {fontes_protegidas}")

    if not fontes_ativas:
        log.error("nenhuma fonte oficial OK no último ciclo — abortando")
        log_captacao(conn, "pulado", erro="todas_fontes_oficiais_falharam",
                     duracao_ms=int((time.time()-t0)*1000), dry_run=not commit)
        return 0

    # ── 2. ESTADO 1: detectar deslistadas novas (flag + mantém tier) ─────────
    cur.execute("""
        SELECT id, classificacao_computed, fonte, empresa, valor_estimado
        FROM obras
        WHERE obra_listada_na_fonte = false
          AND classificacao_computed IN ('OURO', 'PRATA')
          AND visivel = true
          AND fonte = ANY(%s)
          AND (observacoes_validacao IS NULL
               OR observacoes_validacao NOT LIKE '%%deslistada_detectada%%')
    """, (fontes_ativas,))
    estado1 = cur.fetchall()
    _STATS["estado1_detectada"] = len(estado1)
    log.info(f"ESTADO 1 — detectadas pela 1ª vez: {len(estado1)} obras")

    if estado1 and commit:
        cur.execute("""
            UPDATE obras SET observacoes_validacao =
                COALESCE(observacoes_validacao || ' | ', '')
                || 'deslistada_detectada_' || TO_CHAR(NOW() AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD')
            WHERE id = ANY(%s::uuid[])
        """, ([r[0] for r in estado1],))
        conn.commit()

    # ── 3. ESTADO 2: deslistadas 7+ dias → rebaixar OURO/PRATA pra PIPELINE ──
    cur.execute("""
        SELECT id, classificacao_computed, fonte, empresa, valor_estimado,
            SUBSTRING(observacoes_validacao FROM 'deslistada_detectada_(\\d{4}-\\d{2}-\\d{2})')::date AS desde
        FROM obras
        WHERE obra_listada_na_fonte = false
          AND classificacao_computed IN ('OURO', 'PRATA')
          AND visivel = true
          AND fonte = ANY(%s)
          AND observacoes_validacao LIKE '%%deslistada_detectada%%'
          AND observacoes_validacao NOT LIKE '%%rebaixada_deslistada_7d%%'
          AND SUBSTRING(observacoes_validacao FROM 'deslistada_detectada_(\\d{4}-\\d{2}-\\d{2})')::date
              <= (NOW() AT TIME ZONE 'America/Sao_Paulo')::date - INTERVAL '7 days'
          -- proteção: não rebaixar se a mesma empresa (CNPJ) tem outra OURO listada=true
          AND NOT EXISTS (
              SELECT 1 FROM obras o2
              WHERE o2.cnpj = obras.cnpj
                AND o2.classificacao_computed = 'OURO'
                AND o2.obra_listada_na_fonte = true
                AND o2.id <> obras.id
          )
    """, (fontes_ativas,))
    estado2 = cur.fetchall()
    _STATS["estado2_rebaixada"] = len(estado2)
    log.info(f"ESTADO 2 — rebaixar OURO/PRATA → PIPELINE (≥7d deslistada): {len(estado2)} obras")
    for oid, tier, fonte, emp, capex, desde in estado2[:5]:
        log.info(f"  → {emp[:40] if emp else '?'} | {tier} | fonte={fonte} | desde={desde}")

    if estado2 and commit:
        cur.execute("""
            UPDATE obras SET
                classificacao_computed = 'PIPELINE',
                observacoes_validacao = COALESCE(observacoes_validacao || ' | ', '')
                    || 'rebaixada_deslistada_7d_' || TO_CHAR(NOW() AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD')
            WHERE id = ANY(%s::uuid[])
        """, ([r[0] for r in estado2],))
        conn.commit()

    # ── 4. ESTADO 3: deslistadas 14+ dias → invisibilizar + REJEITADO ────────
    cur.execute("""
        SELECT id, classificacao_computed, fonte, empresa, valor_estimado
        FROM obras
        WHERE obra_listada_na_fonte = false
          AND classificacao_computed = 'PIPELINE'
          AND visivel = true
          AND fonte = ANY(%s)
          AND observacoes_validacao LIKE '%%rebaixada_deslistada_7d%%'
          AND SUBSTRING(observacoes_validacao FROM 'rebaixada_deslistada_7d_(\\d{4}-\\d{2}-\\d{2})')::date
              <= (NOW() AT TIME ZONE 'America/Sao_Paulo')::date - INTERVAL '7 days'
          AND (motivo_invisivel IS NULL OR motivo_invisivel NOT LIKE 'auto_deslistada_14d%%')
    """, (fontes_ativas,))
    estado3 = cur.fetchall()
    _STATS["estado3_invisivel"] = len(estado3)
    log.info(f"ESTADO 3 — invisibilizar PIPELINE (≥14d deslistada): {len(estado3)} obras")
    for oid, tier, fonte, emp, capex in estado3[:5]:
        log.info(f"  → {emp[:40] if emp else '?'} | fonte={fonte}")

    if estado3 and commit:
        cur.execute("""
            UPDATE obras SET
                visivel = false,
                classificacao_computed = 'REJEITADO',
                motivo_invisivel = 'auto_deslistada_14d_' || TO_CHAR(NOW() AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD')
            WHERE id = ANY(%s::uuid[])
        """, ([r[0] for r in estado3],))
        conn.commit()

    # ── 5. Stats finais ─────────────────────────────────────────────────────
    _STATS["obras_processadas"] = (
        _STATS["estado1_detectada"]
        + _STATS["estado2_rebaixada"]
        + _STATS["estado3_invisivel"]
    )

    dur_ms = int((time.time() - t0) * 1000)
    log.info(f"=== FIM em {dur_ms}ms ===")
    log.info(f"resumo: detectadas={_STATS['estado1_detectada']} "
             f"rebaixadas={_STATS['estado2_rebaixada']} "
             f"invisibilizadas={_STATS['estado3_invisivel']} "
             f"fontes_protegidas={_STATS['fontes_protegidas']}")

    log_captacao(
        conn,
        "sucesso" if not commit else "sucesso",
        novos=_STATS["estado2_rebaixada"] + _STATS["estado3_invisivel"],
        buscados=_STATS["obras_processadas"],
        duracao_ms=dur_ms,
        dry_run=not commit,
    )

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        _STATS["erros"] = 1
        log.exception(f"erro fatal: {e}")
        sys.exit(1)
