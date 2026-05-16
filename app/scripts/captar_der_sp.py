#!/usr/bin/env python3
"""Captador DER-SP — licitações rodovias estaduais SP (SCAFFOLD).

Status: SCAFFOLD — wire-up pronto (orchestrator + botão admin), parser HTML pendente.

URL alvo: https://der.sp.gov.br/website/Licitacoes/ListarLicitacoes.aspx
Tipo técnico: html_scraper (provavelmente requer Playwright — site .aspx com viewstate)

TODO próxima rodada:
  1. Inspecionar HTML real (curl + grep estrutura tabela)
  2. Identificar paginação (postback ViewState ou query param)
  3. Parser BeautifulSoup ou Playwright pra tabela de licitações
  4. Extrair: objeto, trecho/rodovia, valor estimado, data abertura, modalidade
  5. Filtrar valor >= R$ 10mi
  6. INSERT em obras com fonte='der_sp', fonte_tipo='OFICIAL',
     uf='SP', setor='INFRAESTRUTURA', classificação automática

Dedup sugerido: id_externo = 'DER-SP:<numero_processo>' (UNIQUE em obras).
"""
from __future__ import annotations

import atexit
import json as _json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_der_sp")

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    log.warning(
        "captar_der_sp — SCAFFOLD ATIVO. Parser HTML pendente "
        "(ver TODO no docstring do arquivo). Não captura nada ainda."
    )
    log.info("Wired ao orchestrator + RUN_CAPTADORES_MANUAL para evolução incremental.")
    # _STATS já 0/0/0 — orchestrator registra a execução sem ruído.


if __name__ == "__main__":
    main()
