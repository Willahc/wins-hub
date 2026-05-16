#!/usr/bin/env python3
"""Captador DER-SP — licitações rodovias estaduais SP (SCAFFOLD + probe 16/05).

Status: SCAFFOLD — wire-up pronto (orchestrator + botão admin), parser HTML pendente.

URLs investigadas em 16/05/2026:
  - https://der.sp.gov.br/...           → DNS sem registro (não resolve)
  - https://www.der.sp.gov.br/website/Licitacoes/ListarLicitacoes.aspx  → 404
  - https://www.der.sp.gov.br/WebSite/Licitacoes/LicitacoesGeral.aspx  → 200 mas é
    página de MENU navegacional (zero tabelas), lista real renderizada via JS postback
  - https://www.der.sp.gov.br/WebSite/Licitacoes/{Licitacao,PregaoEletro,
    PregaoPresencial,Editais,Manifestacao}.aspx → todas retornam menus, não listas

Conclusão: portal DER-SP não tem URL pública estável que liste editais. Editais reais
estão publicados em **BEC-SP** (Bolsa Eletrônica de Compras, centralizador SP) ou no
**e-Negócios da Imprensa Oficial SP** (links no menu do DER-SP).

Alternativas viáveis (próxima rodada):
  - **BEC-SP pregão eletrônico** (form ASP.NET com paginação):
    https://www.bec.sp.gov.br/bec_pregao_UI/OC/pesquisa_publica.aspx
  - **BEC-SP convite eletrônico**:
    https://www.bec.sp.gov.br/BEC_Convite_UI/ui/BEC_CV_Pesquisa.aspx
  - **e-Negócios IMESP**:
    https://www.imprensaoficial.com.br/ENegocios/BuscaENegocios_14_1.aspx

TODO próxima rodada (atualizado):
  1. Reorientar pra BEC-SP (centralizador) em vez de scraping direto do DER-SP
  2. Implementar fill do form ASP.NET (__VIEWSTATE + __EVENTVALIDATION postback)
  3. Filtrar objeto: ILIKE '%pavimenta%' OR '%rodovia%' OR '%trecho%' OR '%obra%'
     pra capturar editais de obras (BEC-SP cobre todas categorias, não só rodovias)
  4. UF=SP fixo, setor inferido por keyword
  5. INSERT com fonte='bec_sp_obras' (renomear pra refletir realidade)

Dedup sugerido: id_externo = 'BEC-SP:<oc_id>' onde oc_id = identificador interno BEC.
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
