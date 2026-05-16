#!/usr/bin/env python3
"""Captador PAC Saneamento — contratos MDR (Ministério das Cidades) (SCAFFOLD).

Status: SCAFFOLD — requer cadastro gratuito em https://portaldatransparencia.gov.br/api
pra obter API key. Setar `PORTAL_TRANSPARENCIA_API_KEY` no .env quando disponível.

API: GET https://api.portaldatransparencia.gov.br/api-de-dados/contratos
     header chave-api-dados: <KEY>
     filtros: orgaoSuperior=52000 (Ministério das Cidades)
              dataInicio últimos 60 dias
              valorInicialCompra >= 10000000

TODO próxima rodada:
  1. Cadastrar e armazenar PORTAL_TRANSPARENCIA_API_KEY
  2. Paginação (Portal Transparência usa offset/limit)
  3. Filtrar objeto: ILIKE '%esgoto%' / '%água%' / '%saneamento%' / '%adutora%' / '%ETE%' / '%ETA%'
  4. INSERT em obras com fonte='pac_saneamento', fonte_tipo='OFICIAL',
     setor='SANEAMENTO'

Dedup sugerido: id_externo = 'PAC-SAN:<numero_contrato>'.
"""
from __future__ import annotations

import atexit
import json as _json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_saneamento")

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    if not os.getenv("PORTAL_TRANSPARENCIA_API_KEY"):
        log.warning(
            "PORTAL_TRANSPARENCIA_API_KEY não setada — cadastre gratuitamente em "
            "https://portaldatransparencia.gov.br/api e exporte no .env. SCAFFOLD only."
        )
        return
    log.warning(
        "captar_saneamento — SCAFFOLD ATIVO mesmo com API key. Implementação real pendente."
    )


if __name__ == "__main__":
    main()
