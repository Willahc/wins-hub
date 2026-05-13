#!/usr/bin/env python3
"""Captador PNCP — Licitacoes de defesa (Marinha, Exercito, Aeronautica).

OTIMIZADO 2026-05-13 (sprint dia 2):
  - Apenas modalidades de Concorrencia (4, 5). Pregao (6, 7) raramente e defesa
    estrategica e tem volume ~70x maior (1300/dia vs 230/dia).
  - max_paginas reduzido pra 8 (defesa raramente passa de 100 records/janela).
  - Early-stop: para a iteracao da modalidade apos N=3 paginas consecutivas
    sem record de orgao militar. Usa fetch_pncp_pages (yield por pagina).
  - Idempotente. STATS_JSON na ultima linha.
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
    fetch_pncp_pages, get_conn, inserir_obra_pncp, is_orgao_defesa,
    record_para_dict_obra,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_pncp_defesa")

FONTE = "pncp_defesa"
DIAS_BACK = 1
MODALIDADES_DEFESA = (4, 5)
MAX_PAGINAS_POR_MOD = 8
EARLY_STOP_PAGINAS_SEM_HIT = 3

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    hoje = date.today()
    inicio = hoje - timedelta(days=DIAS_BACK)
    log.info(f"PNCP defesa — janela {inicio} → {hoje} "
             f"(mod {MODALIDADES_DEFESA}, max_pag={MAX_PAGINAS_POR_MOD}, "
             f"early_stop_sem_hit={EARLY_STOP_PAGINAS_SEM_HIT})")

    conn = get_conn()
    try:
        for modalidade in MODALIDADES_DEFESA:
            paginas_sem_hit = 0
            for page in fetch_pncp_pages(modalidade=modalidade,
                                         data_inicial=inicio, data_final=hoje,
                                         max_paginas=MAX_PAGINAS_POR_MOD,
                                         log=log):
                _STATS["buscados"] += len(page)
                hits_pagina = 0
                for rec in page:
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
                        hits_pagina += 1
                    except Exception as e:
                        log.warning(f"insert erro {dados.get('id_externo')}: {e}")
                        _STATS["erros"] += 1
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                if hits_pagina == 0:
                    paginas_sem_hit += 1
                else:
                    paginas_sem_hit = 0
                if paginas_sem_hit >= EARLY_STOP_PAGINAS_SEM_HIT:
                    log.info(f"  mod={modalidade}: early-stop apos {paginas_sem_hit} "
                             f"paginas sem hit militar")
                    break
    finally:
        conn.close()

    log.info(f"PNCP defesa done — {_STATS}")


if __name__ == "__main__":
    main()
