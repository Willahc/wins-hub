#!/usr/bin/env python3
"""Captador ANTT rodoviário V2 — concessões + investimentos previstos (SCAFFOLD).

Status: SCAFFOLD — wire-up pronto, parser pendente.

Já existe `captar_antt_rod.py` no repo (rotas rodoviárias gerais). Este V2 visa
o sub-conjunto de concessões com investimentos previstos por trecho — alto capex.

URL alvo: https://www.gov.br/antt/pt-br/assuntos/rodovias-concedidas
PNCP de concessionárias (CCR, Arteris, Ecorodovias, Autopista) é alternativa
mais limpa que scraping do portal ANTT.

TODO próxima rodada:
  1. Listar concessões ativas via portal ANTT
  2. Para cada concessionária: cruzar com fornecedores (CNPJ conhecido) +
     buscar contratos no PNCP onde orgaoEntidade.cnpj = concessionária
  3. Extrair: trecho, UF, capex previsto, prazo
  4. INSERT em obras com fonte='antt_rod_v2', fonte_tipo='OFICIAL',
     setor='INFRAESTRUTURA', cnpj=<concessionária>

Dedup sugerido: id_externo = 'ANTT-CONC:<concessao_id>:<trecho_id>'.
"""
from __future__ import annotations

import atexit
import json as _json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_antt_rod_v2")

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    log.warning(
        "captar_antt_rod_v2 — SCAFFOLD ATIVO. Listagem de concessões pendente. "
        "Captador legado (captar_antt_rod.py) continua rodando no orchestrator."
    )


if __name__ == "__main__":
    main()
