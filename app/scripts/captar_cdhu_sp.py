#!/usr/bin/env python3
"""Captador CDHU — licitações habitacionais SP (SCAFFOLD + probe 16/05).

Status: SCAFFOLD — wire-up pronto, parser HTML pendente.

Probe 16/05:
  - https://www.cdhu.sp.gov.br/web/guest/licitacoes → 302 redirect para
    https://www.cdhu.sp.gov.br/cdhu (200, 144KB)
  - Página retornada é SPA Liferay sem tabelas/forms no HTML inicial — lista de
    editais provavelmente carregada via JS (REST endpoint ou portlet AJAX)

Alternativa: mesmo BEC-SP cobre CDHU (CDHU é UC — Unidade Compradora — dentro do BEC).
Pesquisar BEC-SP por orgao=CDHU é mais limpo que scraping da SPA Liferay.

TODO próxima rodada (atualizado):
  1. Browser devtools → identificar endpoint AJAX que popula a lista de editais
     (Network tab quando acessar /web/guest/licitacoes)
  2. OU usar BEC-SP filtrado por Unidade Compradora=CDHU
  3. Filtrar valor >= R$ 5mi (briefing) — construção habitacional pode ter ticket menor
  4. INSERT em obras com fonte='cdhu_sp', fonte_tipo='OFICIAL',
     uf='SP', setor='HABITACAO' (ou novo) / 'INFRAESTRUTURA' como fallback

Dedup sugerido: id_externo = 'CDHU-SP:<numero_edital>' ou 'BEC-CDHU:<oc_id>'.
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
