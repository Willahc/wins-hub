#!/usr/bin/env python3
"""Captador BEC-SP — Bolsa Eletrônica de Compras do Estado de SP (SCAFFOLD bloqueado).

Status: SCAFFOLD — investigação 16/05/2026 noite v3 concluiu que BEC-SP NÃO é
viável pra scraping automatizado sem custo extra. Mantido aqui só pra registro.

Probe 16/05 (forms ASP.NET com __VIEWSTATE):

1. Pregão: https://www.bec.sp.gov.br/bec_pregao_UI/OC/pesquisa_publica.aspx
   - HTTP 200, __VIEWSTATE+__VIEWSTATEGENERATOR presentes
   - **TEM CAPTCHA** (`<input name="captcha">`) — bloqueia automação
   - __EVENTVALIDATION ausente nesse form
   - Filtros: oc (número), dt_ini, dt_fim

2. Convite: https://www.bec.sp.gov.br/BEC_Convite_UI/ui/BEC_CV_Pesquisa.aspx
   - HTTP 200, __VIEWSTATE+__EVENTVALIDATION presentes
   - **Tem campo `txtUsuario`** — indica login obrigatório (CAUFESP, Cadastro
     Unificado de Fornecedores do Estado de SP)
   - Filtros completos: atividade, situação, secretaria, UGE, município, tipoEdital

3. Dispensa: https://www.bec.sp.gov.br/BEC_Dispensa_UI/ui/BEC_DL_Pesquisa.aspx
   - Mesmo padrão do convite (txtUsuario, requer login)

Conclusão: BEC-SP não é caminho viável sem (a) quebrar captcha (2captcha/anti-captcha,
custo e complexidade) ou (b) ter conta CAUFESP cadastrada como fornecedor (requer
fornecedor real registrado no estado).

Caminhos alternativos pra obras SP (próximas rodadas):
  - PNCP `/contratacoes/publicacao` filtrado por UF=SP — já implementado em
    `captar_pncp_full.py`. Cobre as compras SP que entram no PNCP (Lei 14.133/2021).
  - e-Negócios IMESP: https://www.imprensaoficial.com.br/ENegocios/BuscaENegocios_14_1.aspx
    publica licitações do estado SP sem captcha. Vale probe focado.
  - Transparência SP: https://www.transparencia.sp.gov.br/Home/DespContratos
    contratos já assinados (não editais em aberto, mas histórico).

Sem captura ativa nesta versão — log emite WARNING explicando o status.
"""
from __future__ import annotations

import atexit
import json as _json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_bec_sp")

_STATS = {"buscados": 0, "novos": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def main():
    log.warning(
        "captar_bec_sp — BLOQUEADO. Pregão requer captcha, Convite/Dispensa requerem "
        "login CAUFESP. Ver docstring pra alternativas (PNCP já cobre SP via "
        "captar_pncp_full; e-Negócios IMESP a investigar)."
    )


if __name__ == "__main__":
    main()
