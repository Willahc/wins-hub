#!/usr/bin/env python3
"""
poller_executor_pncp.py — Descobre a EMPRESA EXECUTORA de obras de governo
adjudicadas (vencedor da licitação) via PNCP /contratos e preenche
obras.empresa_executora/cnpj_executora/dominio_executora.

Princípio: em obra pública o governo é só o CONTRATANTE; o lead real (e o decisor
que interessa aos fornecedores) está na empresa EXECUTORA. Enquanto aberta, a obra
fica em PIPELINE com executora_status='aguardando_adjudicacao'. Quando o PNCP
publica o contrato (adjudicação), este poller casa numeroControlePncpCompra com
nosso id_externo, preenche a executora e marca 'adjudicada'.

Fonte: GET https://pncp.gov.br/api/consulta/v1/contratos
  (dataInicial,dataFinal yyyymmdd; codigoModalidadeContratacao; pagina; tamanhoPagina>=10)

Uso:
  python poller_executor_pncp.py                 # dry-run, janela 3 dias
  python poller_executor_pncp.py --commit --dias 30
STATS_JSON na ultima linha.
"""
from __future__ import annotations
import os, sys, argparse, logging, time, json as _json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import psycopg2
from psycopg2.extras import execute_values

PNCP_CONTRATOS = "https://pncp.gov.br/api/consulta/v1/contratos"
# Obras de construção são adjudicadas via Concorrência Eletrônica(4)/Presencial(5).
# (Pregão/Dispensa explodem volume no VPS 1vCPU sem ganho p/ obra.)
MODALIDADES = [4, 5]
PAGE = 50
MAX_PAGINAS = 200          # backstop por modalidade
BRT = ZoneInfo("America/Sao_Paulo")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("poller_executor_pncp")
_STATS = {"contratos_varridos": 0, "obras_pendentes": 0, "matches": 0, "atualizadas": 0, "erros": 0}


def _get(sess, params, tentativas=4):
    for i in range(tentativas):
        try:
            r = sess.get(PNCP_CONTRATOS, params=params, timeout=40)
            if r.status_code == 204:
                return None
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1)); continue
            return None
        except (requests.RequestException, ValueError):
            time.sleep(2 * (i + 1))
    _STATS["erros"] += 1
    return None


def coletar_vencedores(dias: int) -> dict:
    """Varre contratos PNCP na janela -> {numeroControlePncpCompra: (ni, razao)}."""
    fim = datetime.now(BRT).date()
    ini = fim - timedelta(days=dias)
    sess = requests.Session()
    sess.headers["User-Agent"] = "wins-hub-poller/1.0"
    winners = {}
    for mod in MODALIDADES:
        pagina = 1
        while pagina <= MAX_PAGINAS:
            params = {
                "dataInicial": ini.strftime("%Y%m%d"),
                "dataFinal": fim.strftime("%Y%m%d"),
                "codigoModalidadeContratacao": mod,
                "pagina": pagina, "tamanhoPagina": PAGE,
            }
            d = _get(sess, params)
            if not d:
                break
            data = d.get("data") or []
            for c in data:
                key = c.get("numeroControlePncpCompra")
                ni = (c.get("niFornecedor") or "").strip()
                raz = (c.get("nomeRazaoSocialFornecedor") or "").strip()
                if key and ni:
                    winners[key] = (ni, raz)
            _STATS["contratos_varridos"] += len(data)
            total_pag = d.get("totalPaginas") or 1
            if pagina >= total_pag:
                break
            pagina += 1
            time.sleep(0.3)
        log.info(f"  modalidade {mod}: varrido (acumulado {_STATS['contratos_varridos']} contratos)")
    return winners


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dias", type=int, default=3)
    args = ap.parse_args()
    log.info(f"=== POLLER EXECUTOR PNCP INICIO {'(COMMIT)' if args.commit else '(DRY-RUN)'} janela={args.dias}d ===")

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()
    # obras de governo aguardando adjudicacao, com id PNCP
    cur.execute("""
        SELECT id, replace(id_externo,'PNCP:','')
        FROM obras
        WHERE executora_status = 'aguardando_adjudicacao'
          AND id_externo LIKE 'PNCP:%'
    """)
    pend = {ncp: oid for (oid, ncp) in cur.fetchall()}
    _STATS["obras_pendentes"] = len(pend)
    log.info(f"obras pendentes (PNCP, aguardando_adjudicacao): {len(pend)}")
    if not pend:
        log.info("nada a fazer."); conn.close(); return

    winners = coletar_vencedores(args.dias)
    log.info(f"vencedores coletados na janela: {len(winners)}")

    updates = []
    for ncp, oid in pend.items():
        w = winners.get(ncp)
        if not w:
            continue
        ni, raz = w
        cnpj = ni if (ni.isdigit() and len(ni) == 14) else None  # CPF de MEI fica só na razao
        _STATS["matches"] += 1
        updates.append((raz[:300] if raz else None, cnpj, oid))
        log.info(f"  ADJUDICADA obra={str(oid)[:8]} -> executor={raz} (ni={ni})")

    if updates and args.commit:
        execute_values(cur, """
            UPDATE obras o SET
              empresa_executora = v.raz,
              cnpj_executora = v.cnpj,
              executora_status = 'adjudicada',
              executora_fonte = 'pncp_resultado',
              executora_atualizada_em = now()
            FROM (VALUES %s) AS v(raz, cnpj, oid)
            WHERE o.id = v.oid::uuid
        """, updates)
        _STATS["atualizadas"] = cur.rowcount or 0
        conn.commit()
        log.info(f"  {_STATS['atualizadas']} obras marcadas adjudicada (executor preenchido)")
    elif updates:
        log.info(f"  [DRY-RUN] {len(updates)} obras seriam atualizadas com executor")
    else:
        log.info("  nenhuma das pendentes foi adjudicada nesta janela")

    conn.close()
    log.info("=== FIM ===")
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


if __name__ == "__main__":
    main()
