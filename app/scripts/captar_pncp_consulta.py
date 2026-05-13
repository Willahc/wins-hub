#!/usr/bin/env python3
"""Captador PNCP — Manifestacao de Interesse + Credenciamento.

Modalidades 10 (Manifestacao de Interesse) e 12 (Credenciamento).
ALTA antecipacao: estes instrumentos precedem o edital formal em 4-26 semanas.

Sem filtro de valor minimo — sinal antecipado vale mesmo se valor ainda nao
estiver carimbado. Sem filtro de keyword de obra por mesma razao (alguns
credenciamentos sao bens/servicos mas o sinal de intencao vale).

Idempotente. STATS_JSON na ultima linha.
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
    MOD_CREDENCIAMENTO, MOD_MANIFESTACAO_INTERESSE,
    fetch_pncp, get_conn, inserir_obra_pncp, record_para_dict_obra,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_pncp_consulta")

FONTE = "pncp_consulta"
DIAS_BACK = 1

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    hoje = date.today()
    inicio = hoje - timedelta(days=DIAS_BACK)
    log.info(f"PNCP consulta publica — janela {inicio} → {hoje} (manifestacao 10 + credenciamento 12)")

    conn = get_conn()
    try:
        for modalidade in (MOD_MANIFESTACAO_INTERESSE, MOD_CREDENCIAMENTO):
            for rec in fetch_pncp(modalidade=modalidade,
                                  data_inicial=inicio, data_final=hoje,
                                  log=log):
                _STATS["buscados"] += 1
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

    log.info(f"PNCP consulta done — {_STATS}")


if __name__ == "__main__":
    main()
