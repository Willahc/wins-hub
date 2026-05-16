#!/usr/bin/env python3
"""Captador SABESP — licitações saneamento SP (SCAFFOLD + probe 16/05).

Status: SCAFFOLD — wire-up pronto, parser HTML pendente.

Probe 16/05:
  - https://www.sabesp.com.br/ → connection failed (HTTP 000, possível bloqueio
    geográfico/TLS handshake). Domínio resolve, mas connect não completa do host.
  - Alternativas a investigar próxima rodada:
    - Tentar via FlareSolverr (Chrome real evita TLS fingerprint detection)
    - SABESP é UC do BEC-SP (Unidade Compradora) — usar BEC-SP filtrado é mais
      portável que scraping direto sabesp.com.br
    - Portal RI: https://ri.sabesp.com.br/ (subdomínio separado, pode permitir)

TODO próxima rodada (atualizado):
  1. Probe via FlareSolverr antes de tentar HTTP direto
  2. Se bloquear, ir via BEC-SP filtrando orgao_compras=SABESP
  3. Filtrar valor >= R$ 10mi (briefing) — obras de saneamento têm ticket alto
  4. INSERT em obras com fonte='sabesp_sp', fonte_tipo='OFICIAL',
     uf='SP', setor='SANEAMENTO'

Cobertura complementar já existente: `captar_bndes.py --saneamento` (38 obras
inseridas 16/05 noite) cobre o financiamento BNDES de obras de saneamento.

Dedup sugerido: id_externo = 'SABESP-SP:<numero_processo>' ou 'BEC-SABESP:<oc_id>'.
"""
from __future__ import annotations

import atexit
import json as _json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_sabesp_sp")

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    log.warning(
        "captar_sabesp_sp — SCAFFOLD ATIVO. Parser HTML pendente "
        "(ver TODO no docstring). Não captura nada ainda."
    )


if __name__ == "__main__":
    main()
