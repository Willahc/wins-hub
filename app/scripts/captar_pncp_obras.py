#!/usr/bin/env python3
"""Captador PNCP — Concorrencias (obras civis).

Modalidades 4 (Concorrencia Eletronica) e 5 (Concorrencia Presencial).
Filtra: keyword obra em objetoCompra + valor >= R$ 1M.

Idempotente via id_externo = "PNCP:<numeroControlePNCP>".
STATS_JSON na ultima linha pra orchestrator parsear.
"""
from __future__ import annotations

import atexit
import json as _json
import logging
import sys
from datetime import date, timedelta

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

from _pncp_common import (  # noqa: E402
    MOD_CONCORRENCIA_ELETRONICA, MOD_CONCORRENCIA_PRESENCIAL,
    fetch_pncp, get_conn, inserir_obra_pncp, is_obra, record_para_dict_obra,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_pncp_obras")

FONTE = "pncp_obras"
DIAS_BACK = 1  # cobrir D-1 e D pra dar sobreposicao

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    hoje = date.today()
    inicio = hoje - timedelta(days=DIAS_BACK)
    log.info(f"PNCP obras — janela {inicio} → {hoje} (concorrencias 4+5, valor >= R$ 1M)")

    conn = get_conn()
    try:
        for modalidade in (MOD_CONCORRENCIA_ELETRONICA, MOD_CONCORRENCIA_PRESENCIAL):
            for rec in fetch_pncp(modalidade=modalidade,
                                  data_inicial=inicio, data_final=hoje,
                                  log=log):
                _STATS["buscados"] += 1
                if not is_obra(rec.get("objetoCompra"), modalidade,
                               rec.get("valorTotalEstimado")):
                    continue
                dados = record_para_dict_obra(rec, fonte=FONTE)
                if not dados:
                    continue
                try:
                    oid = inserir_obra_pncp(conn, dados)
                    if oid:
                        _STATS["novos"] += 1
                except Exception as e:
                    log.warning(f"insert erro {dados.get('id_externo')}: {e}")
                    _STATS["erros"] += 1
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    finally:
        conn.close()

    log.info(f"PNCP obras done — {_STATS}")


if __name__ == "__main__":
    main()
