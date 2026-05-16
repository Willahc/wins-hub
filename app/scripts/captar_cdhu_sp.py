#!/usr/bin/env python3
"""Captador CDHU — licitações habitacionais SP (SCAFFOLD).

Status: SCAFFOLD — wire-up pronto, parser HTML pendente.

URL alvo: https://www.cdhu.sp.gov.br/web/guest/licitacoes
Tipo técnico: html_scraper (site Liferay, talvez tabela paginada server-side)

TODO próxima rodada:
  1. Inspecionar HTML/JSON real
  2. Parser pra lista de licitações (objeto, valor, data, modalidade)
  3. Filtrar valor >= R$ 5mi (briefing) — construção habitacional pode ter ticket menor
  4. INSERT em obras com fonte='cdhu_sp', fonte_tipo='OFICIAL',
     uf='SP', setor='HABITACAO' (ou novo) / 'INFRAESTRUTURA' como fallback

Dedup sugerido: id_externo = 'CDHU-SP:<numero_edital>'.
"""
from __future__ import annotations

import atexit
import json as _json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_cdhu_sp")

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    log.warning(
        "captar_cdhu_sp — SCAFFOLD ATIVO. Parser HTML pendente "
        "(ver TODO no docstring). Não captura nada ainda."
    )


if __name__ == "__main__":
    main()
