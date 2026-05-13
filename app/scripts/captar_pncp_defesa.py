#!/usr/bin/env python3
"""Captador PNCP — Licitacoes de defesa (Marinha, Exercito, Aeronautica).

Reuso da API PNCP com filtro de orgao via razaoSocial. Varre concorrencias
+ pregoes (modalidades 4, 5, 6, 7) e filtra os orgaos militares.

Sem filtro de valor minimo aqui — defesa tem itens estrategicos abaixo de
R$ 1M (radar, comunicacao, modernizacao de armamento).

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
    fetch_pncp, get_conn, inserir_obra_pncp, is_orgao_defesa,
    record_para_dict_obra,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_pncp_defesa")

FONTE = "pncp_defesa"
DIAS_BACK = 1
MODALIDADES_DEFESA = (4, 5, 6, 7)  # Concorrencia (4,5) + Pregao (6,7)

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    hoje = date.today()
    inicio = hoje - timedelta(days=DIAS_BACK)
    log.info(f"PNCP defesa — janela {inicio} → {hoje} (filtro orgao militar)")

    conn = get_conn()
    try:
        for modalidade in MODALIDADES_DEFESA:
            for rec in fetch_pncp(modalidade=modalidade,
                                  data_inicial=inicio, data_final=hoje,
                                  log=log):
                _STATS["buscados"] += 1
                razao = (rec.get("orgaoEntidade") or {}).get("razaoSocial", "")
                if not is_orgao_defesa(razao):
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

    log.info(f"PNCP defesa done — {_STATS}")


if __name__ == "__main__":
    main()
