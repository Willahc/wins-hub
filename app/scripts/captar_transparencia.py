#!/usr/bin/env python3
"""Captador Portal Transparência Federal — contratos amplos (SCAFFOLD).

Status: SCAFFOLD — requer PORTAL_TRANSPARENCIA_API_KEY (compartilhada com
`captar_saneamento.py`).

API: GET https://api.portaldatransparencia.gov.br/api-de-dados/contratos
     header chave-api-dados: <KEY>
     filtros: valorInicialCompra >= 10000000
              modalidadeLicitacao IN (1,2,3) (concorrência, tomada de preços, convite)
              dataInicio últimos 60 dias

TODO próxima rodada:
  1. Reusar chave configurada para captar_saneamento
  2. Paginação Portal Transparência
  3. Filtrar objeto: ILIKE '%obra%' / '%construção%' / '%pavimentação%' / '%reforma%'
  4. INSERT em obras com fonte='transparencia_federal', fonte_tipo='OFICIAL',
     setor inferido por keyword (helper de _pncp_common.setor_de_objeto serve)

Dedup sugerido: id_externo = 'TRANSP-FED:<numero_contrato>'.

Prioridade: BAIXA — overlap esperado com captar_pncp_full (que cobre o universo
PNCP completo). Útil só pra contratos pré-Lei 14.133/2021 e órgãos não-PNCP.
"""
from __future__ import annotations

import atexit
import json as _json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_transparencia")

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    if not os.getenv("PORTAL_TRANSPARENCIA_API_KEY"):
        log.warning(
            "PORTAL_TRANSPARENCIA_API_KEY não setada — ver captar_saneamento.py."
        )
        return
    log.warning(
        "captar_transparencia — SCAFFOLD ATIVO mesmo com API key. Implementação real pendente."
    )


if __name__ == "__main__":
    main()
