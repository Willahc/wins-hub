#!/usr/bin/env python3
"""Captador DOE-SP via Base dos Dados (basedosdados).

NOTA SOBRE ESCOPO (sprint dia 5 / Sessao 2):
  Brief original: usar basedosdados.org SDK pra puxar DOE-SP.
  Bloqueador: o SDK Python `basedosdados` requer Google Cloud project
  com billing ativo (`billing_project_id`). Sem GCP configurado, todas
  as queries falham com "We are not sure which Google Cloud project
  should be billed."

  Esta versao e SCAFFOLD: detecta o bloqueio, loga e sai com STATS
  zerado. INSERT obras pendente de configurar GCP project pra BD SDK.

  Alternativas pra Day 6+:
    - Configurar service account GCP + setar GOOGLE_APPLICATION_CREDENTIALS
      no .env do container.
    - Usar Imprensa Oficial SP (imprensaoficial.com.br) direto via
      Playwright — porem retornou 403 nos probes do sprint dia 3.

STATS_JSON na ultima linha.
"""
from __future__ import annotations

import atexit
import json as _json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_doe_sp")

FONTE = "doe_sp"

_STATS = {"buscados": 0, "novos": 0, "erros": 0, "bloqueio": 1}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main() -> int:
    log.info("DOE-SP scaffold (basedosdados) — verificando GCP credentials")
    if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS") and not os.getenv("BD_BILLING_PROJECT"):
        log.warning("Sem GCP project configurado (GOOGLE_APPLICATION_CREDENTIALS ou "
                    "BD_BILLING_PROJECT). DOE-SP via Base dos Dados requer GCP billing.")
        log.warning("DOE-SP captador NAO INSERE obras nesta versao. Trabalho dia 6+: "
                    "configurar GCP project pra billing do SDK BD.")
        return 0

    # Quando GCP estiver configurado, plug aqui:
    try:
        import basedosdados as bd
    except ImportError:
        log.error("basedosdados nao instalado")
        return 0
    log.info("basedosdados import OK. Implementacao da query SP pendente.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
