#!/usr/bin/env python3
"""Captador PNCP — cobertura ampla (30 dias, modalidades de obra, valor >= R$ 10mi).

Diferenças vs `captar_pncp_obras.py` (janela 1d, R$ 1mi):
- Janela 30 dias (rede de segurança para o que escapou do daily)
- Threshold R$ 10mi (corta licitações pequenas, foca obras médio-grande)
- Inclui modalidades 4 (Concorrência Eletrônica), 5 (Concorrência Presencial)
  e 10 (Manifestação de Interesse) — antecipação de edital

Idempotência: ON CONFLICT (id_externo) DO NOTHING — quase tudo já vem do daily;
o full pega só o que daily perdeu por erro ou janela.

STATS_JSON na última linha pra orchestrator.
"""
from __future__ import annotations

import argparse
import atexit
import json as _json
import logging
import sys
import time
from datetime import date, timedelta
from typing import Any, Dict, Iterator, List

import requests

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

from _pncp_common import (  # noqa: E402
    MOD_CONCORRENCIA_ELETRONICA, MOD_CONCORRENCIA_PRESENCIAL,
    MOD_MANIFESTACAO_INTERESSE,
    get_conn, inserir_obra_pncp, is_obra, record_para_dict_obra,
)


# Override: endpoint /publicacao (vs /proposta usado pelo daily) — `dataPublicacaoPncp`
# filtra pela data em que o edital foi publicado no PNCP, cobrindo licitações cuja
# proposta já está aberta hoje mas foram publicadas dias antes.
PNCP_BASE_PUB = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"


def fetch_pncp_publicacao(*, modalidade: int, data_inicial: date, data_final: date,
                          max_paginas: int = 40,
                          log: logging.Logger | None = None) -> Iterator[Dict[str, Any]]:
    log = log or logging.getLogger("pncp_full")
    sess = requests.Session()
    pagina = 1
    while pagina <= max_paginas:
        url = (f"{PNCP_BASE_PUB}"
               f"?dataInicial={data_inicial:%Y%m%d}"
               f"&dataFinal={data_final:%Y%m%d}"
               f"&codigoModalidadeContratacao={modalidade}"
               f"&pagina={pagina}")
        try:
            r = sess.get(url, timeout=60)
            r.raise_for_status()
        except Exception as e:
            log.warning("PNCP fetch falhou mod=%s pag=%s: %s", modalidade, pagina, e)
            return
        if r.status_code == 204 or not r.content:
            log.info("  mod=%s pag=%s sem dados (204/empty)", modalidade, pagina)
            return
        try:
            d = r.json()
        except ValueError:
            log.warning("PNCP body inválido mod=%s pag=%s", modalidade, pagina)
            return
        data = (d.get("data") or []) if isinstance(d, dict) else []
        for rec in data:
            yield rec
        total_paginas = (d.get("totalPaginas") or 0) if isinstance(d, dict) else 0
        if pagina >= total_paginas:
            break
        pagina += 1
        time.sleep(0.3)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_pncp_full")

FONTE = "pncp_full"
DIAS_BACK = 30
VALOR_MIN_FULL = 10_000_000  # R$ 10mi
MODALIDADES = (
    MOD_CONCORRENCIA_ELETRONICA,
    MOD_CONCORRENCIA_PRESENCIAL,
    MOD_MANIFESTACAO_INTERESSE,
)

_STATS = {"buscados": 0, "novos": 0, "filtrados_capex": 0, "filtrados_obra": 0, "erros": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def _passa_filtro_full(rec: dict, modalidade: int) -> bool:
    """is_obra do _pncp_common usa threshold de R$ 1mi. Aqui aplicamos R$ 10mi.

    Manifestação de Interesse (10) é sinal antecipado de obra grande — valor
    declarado costuma ser estimativa ampla; mantém-se passando se >= R$ 10mi.
    """
    valor = rec.get("valorTotalEstimado") or 0
    if valor < VALOR_MIN_FULL:
        _STATS["filtrados_capex"] += 1
        return False
    if not is_obra(rec.get("objetoCompra"), modalidade, valor):
        _STATS["filtrados_obra"] += 1
        return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="Não persiste, só conta + amostra")
    parser.add_argument("--days", type=int, default=DIAS_BACK)
    args = parser.parse_args()

    hoje = date.today()
    inicio = hoje - timedelta(days=args.days)
    log.info(
        "PNCP full — janela %s → %s (modalidades %s, valor >= R$ %.0fmi)%s",
        inicio, hoje, MODALIDADES, VALOR_MIN_FULL / 1e6,
        " [DRY]" if args.dry else "",
    )

    amostra = []
    conn = None if args.dry else get_conn()
    try:
        for modalidade in MODALIDADES:
            for rec in fetch_pncp_publicacao(
                modalidade=modalidade,
                data_inicial=inicio, data_final=hoje,
                max_paginas=40, log=log,
            ):
                _STATS["buscados"] += 1
                if not _passa_filtro_full(rec, modalidade):
                    continue
                dados = record_para_dict_obra(rec, fonte=FONTE)
                if not dados:
                    continue

                if args.dry:
                    if len(amostra) < 10:
                        amostra.append({
                            "id_externo": dados["id_externo"],
                            "nome": dados["nome"][:80],
                            "valor_mi": round((dados["valor_estimado"] or 0) / 1e6, 1),
                            "uf": dados["uf"],
                            "modalidade": modalidade,
                        })
                    _STATS["novos"] += 1
                    continue

                try:
                    oid = inserir_obra_pncp(conn, dados)
                    if oid:
                        _STATS["novos"] += 1
                except Exception as e:
                    log.warning("insert erro %s: %s", dados.get("id_externo"), e)
                    _STATS["erros"] += 1
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    finally:
        if conn:
            conn.close()

    if amostra:
        log.info("Amostra dos primeiros %d:", len(amostra))
        for a in amostra:
            log.info("  [mod=%s] %s — %s — R$ %.1fmi — %s",
                     a["modalidade"], a["id_externo"], a["nome"], a["valor_mi"], a["uf"])

    log.info("PNCP full done — %s", _STATS)


if __name__ == "__main__":
    main()
