#!/usr/bin/env python3
"""Captador SABESP — licitações saneamento SP (SCAFFOLD).

Status: SCAFFOLD — wire-up pronto, parser HTML pendente.

URL alvo: https://www.sabesp.com.br/Calandraweb/CalandraRedirect/ (portal licitações
da SABESP — caminho exato a confirmar; provavelmente sub-página acessível pelo menu
"Fornecedores").
Tipo técnico: html_scraper, eventualmente Playwright se houver JS pesado.

TODO próxima rodada:
  1. Localizar URL pública estável das licitações em aberto
  2. Parser pra tabela: objeto, valor estimado, data abertura, modalidade
  3. Filtrar valor >= R$ 10mi
  4. INSERT em obras com fonte='sabesp_sp', fonte_tipo='OFICIAL',
     uf='SP', setor='SANEAMENTO'

Dedup sugerido: id_externo = 'SABESP-SP:<numero_processo>'.
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
