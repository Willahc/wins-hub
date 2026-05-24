"""populate_dominios.py — popula empresa_dominios para obras de alto CAPEX sem domínio cacheado.

Filtro: obras.valor_estimado >= --capex-floor (default R$1bi) AND
        (sem registro em empresa_dominios OR empresa_dominios.dominio IS NULL).
Ordem: capex DESC.

Fonte: SearchChain (Serper > Brave > Bing > DDG) com fallback pra Serper direto.
Marcador: fonte='serper_auto', validacao_metodo='populate_dominios_v1' (rollback fácil).

Uso (dentro do container wins_hub_v2-api-1):
    # dry-run: lista 50 alvos sem persistir
    python -m app.scripts.populate_dominios --limit 50
    # commit: persiste no banco
    python -m app.scripts.populate_dominios --commit --limit 50

Custo: ~$0.006 por busca Serper. 50 buscas ≈ $0.30.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import psycopg2
import requests

CAPEX_FLOOR_DEFAULT = 1_000_000_000  # R$1bi

SKIP_DOMAINS = {
    'linkedin.com', 'facebook.com', 'instagram.com', 'youtube.com', 'twitter.com', 'x.com',
    'wikipedia.org', 'wikimapia.org', 'tiktok.com', 'pinterest.com',
    'reclameaqui.com.br', 'cnpj.biz', 'econodata.com.br', 'cnpja.com', 'cnpj.info',
    'consultasocio.com', 'empresascnpj.com', 'empresas.serasaexperian.com.br',
    'serasaexperian.com.br', 'escavador.com', 'cadastronacional.com', 'panjiva.com',
    'zoominfo.com', 'crunchbase.com', 'dnb.com', 'dun-bradstreet.com',
    'b2brazil.com', 'buybrazil.com', 'emis.com', 'importgenius.com',
    'thetradevision.com', 'data.anbima.com.br', 'anbima.com.br',
    'bloomberg.com', 'infomoney.com.br', 'valor.com.br', 'noticias.r7.com', 'r7.com',
    'bnamericas.com', 'gazetasp.com.br',
    'gov.br', 'receita.fazenda.gov.br', 'jusbrasil.com.br', 'tj.jus.br', 'jus.br',
    'marinha.mil.br', 'ccee.org.br', 'b3.com.br',
    'glassdoor.com.br', 'vagas.com.br', 'indeed.com', 'catho.com.br',
    'infojobs.com.br', 'empregos.com.br',
    'ebay.com', 'mercadolivre.com.br', 'amazon.com', 'amazon.com.br',
    'maps.google.com', 'goo.gl', 'bit.ly', 'google.com',
}

GENERIC_TOKENS = {
    'ltda', 'sa', 's/a', 's.a', 'industria', 'comercio', 'industrial', 'indústria',
    'empreendimentos', 'do', 'de', 'da', 'e', '&', '-', '—', 'recuperacao', 'judicial',
    'em', 'manutencao', 'manutenção', 'opex', 'ampliacao', 'ampliação', 'fabril',
    'nova', 'fabrica', 'fábrica', 'retrofit', 'termico', 'térmico', '/', 'ind', 'es',
}


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        stream=sys.stdout,
    )
    return logging.getLogger('populate_dominios')


def clean_name(name: str) -> str:
    n = re.sub(r'[^a-zA-Z0-9\s]', ' ', (name or '').lower())
    return ' '.join(t for t in n.split() if t not in GENERIC_TOKENS and len(t) > 2)


def domain_matches_name(domain: str, name_clean: str) -> bool:
    if not name_clean:
        return False
    tokens = [t for t in name_clean.split() if len(t) >= 4]
    domain_clean = re.sub(r'[^a-z0-9]', '', domain.lower())
    return any(t in domain_clean for t in tokens[:3])


def _search_serper_direct(empresa_nome: str, serper_key: str, log: logging.Logger):
    name_clean = clean_name(empresa_nome)
    try:
        r = requests.post(
            'https://google.serper.dev/search',
            headers={'X-API-KEY': serper_key, 'Content-Type': 'application/json'},
            json={'q': f'"{empresa_nome}" site oficial', 'hl': 'pt', 'gl': 'br', 'num': 8},
            timeout=15,
        )
        results = r.json().get('organic', [])
    except Exception as e:
        log.warning(f"serper falhou empresa='{empresa_nome[:60]}': {e}")
        return None
    for res in results:
        url = res.get('link', '') or ''
        if not url:
            continue
        domain = urlparse(url).netloc.replace('www.', '').lower()
        if not domain:
            continue
        if any(skip in domain for skip in SKIP_DOMAINS):
            continue
        if not domain_matches_name(domain, name_clean):
            continue
        if domain.startswith('ri.') or domain.startswith('investidores.'):
            parts = domain.split('.', 1)
            domain = parts[1] if len(parts) > 1 else domain
        return domain
    return None


def search_domain(empresa_nome: str, serper_key: str, log: logging.Logger):
    # Tenta SearchChain primeiro (Serper > Brave > Bing > DDG + fuzzy >=0.3).
    try:
        from sales_intelligence.camada1_identificacao.descobrir_dominio import (
            descobrir_dominio_via_chain,
        )
        d = descobrir_dominio_via_chain(empresa_nome)
        if d:
            if d.startswith('ri.') or d.startswith('investidores.'):
                parts = d.split('.', 1)
                d = parts[1] if len(parts) > 1 else d
            return d
    except Exception as e:
        log.warning(f"chain falhou empresa='{empresa_nome[:60]}': {e}")
    # Fallback: Serper direto com filtro de tokens.
    return _search_serper_direct(empresa_nome, serper_key, log)


def fetch_candidates(cur, capex_floor: int, limit: int, cnpj_in: list | None = None):
    if cnpj_in:
        cur.execute(
            """
            SELECT cnpj, empresa, capex FROM (
              SELECT DISTINCT ON (o.cnpj)
                     o.cnpj, o.empresa, o.valor_estimado AS capex
              FROM obras o
              LEFT JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
              WHERE o.cnpj = ANY(%s)
                AND o.empresa IS NOT NULL AND o.empresa <> ''
                AND (ed.cnpj IS NULL OR ed.dominio IS NULL)
              ORDER BY o.cnpj, o.valor_estimado DESC NULLS LAST
            ) t
            ORDER BY capex DESC NULLS LAST
            LIMIT %s
            """,
            (cnpj_in, limit),
        )
        return cur.fetchall()
    cur.execute(
        """
        SELECT cnpj, empresa, capex FROM (
          SELECT DISTINCT ON (o.cnpj)
                 o.cnpj, o.empresa, o.valor_estimado AS capex
          FROM obras o
          LEFT JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
          WHERE o.cnpj IS NOT NULL
            AND o.empresa IS NOT NULL
            AND o.empresa <> ''
            AND o.valor_estimado >= %s
            AND (ed.cnpj IS NULL OR ed.dominio IS NULL)
          ORDER BY o.cnpj, o.valor_estimado DESC NULLS LAST
        ) t
        ORDER BY capex DESC NULLS LAST
        LIMIT %s
        """,
        (capex_floor, limit),
    )
    return cur.fetchall()


# ───────────────────────── Holding fallback (v1.4.7 pattern) ─────────────
# SPV/concessionária sem domínio próprio descoberto → BrasilAPI QSA → primeiro
# sócio PJ → lookup ou discover do domínio da holding → persiste holding_*.

def _brasilapi_qsa(cnpj: str, log: logging.Logger):
    cnpj_clean = re.sub(r'\D', '', cnpj or '')
    if len(cnpj_clean) != 14:
        return None
    try:
        req = Request(
            f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_clean}",
            headers={"User-Agent": "WiNS-Hub/populate_dominios"},
        )
        return json.loads(urlopen(req, timeout=10).read())
    except Exception as e:
        log.warning(f"brasilapi_qsa({cnpj_clean}) falhou: {e}")
        return None


def _socio_pj_controlador(qsa_data):
    if not qsa_data:
        return None
    for s in (qsa_data.get("qsa") or []):
        raw_id = (s.get("cnpj_cpf_do_socio") or "").strip()
        digits = re.sub(r'\D', '', raw_id)
        if "*" not in raw_id and len(digits) == 14:
            return digits, (s.get("nome_socio") or "").strip()
    return None


def fetch_candidates_holding(cur, capex_floor: int, limit: int):
    """Subsidiárias capex>=floor sem dominio próprio E sem holding_dominio cacheado."""
    cur.execute(
        """
        SELECT cnpj, empresa, capex FROM (
          SELECT DISTINCT ON (o.cnpj)
                 o.cnpj, o.empresa, o.valor_estimado AS capex
          FROM obras o
          LEFT JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
          WHERE o.cnpj IS NOT NULL
            AND o.empresa IS NOT NULL
            AND o.empresa <> ''
            AND o.valor_estimado >= %s
            AND (ed.cnpj IS NULL OR ed.dominio IS NULL)
            AND (ed.cnpj IS NULL OR ed.holding_dominio IS NULL)
          ORDER BY o.cnpj, o.valor_estimado DESC NULLS LAST
        ) t
        ORDER BY capex DESC NULLS LAST
        LIMIT %s
        """,
        (capex_floor, limit),
    )
    return cur.fetchall()


def lookup_holding_dominio(cur, holding_cnpj: str):
    """Já temos dominio cacheado pra essa holding?"""
    cur.execute(
        "SELECT dominio FROM empresa_dominios WHERE cnpj=%s AND dominio IS NOT NULL",
        (holding_cnpj,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def persist_holding_subsidiary(cur, cnpj: str, empresa: str,
                                holding_cnpj: str, holding_nome: str,
                                holding_dominio: str):
    """INSERT/UPDATE row da subsidiária com holding_* preenchido (dominio=NULL)."""
    cur.execute(
        """
        INSERT INTO empresa_dominios
            (cnpj, empresa_nome, dominio, fonte, confianca,
             validacao_metodo, validacao_data,
             holding_cnpj, holding_nome, holding_dominio)
        VALUES (%s, %s, NULL, 'brasilapi_qsa', 2,
                'populate_dominios_holding_v1', NOW(),
                %s, %s, %s)
        ON CONFLICT (cnpj) DO UPDATE
          SET empresa_nome      = COALESCE(empresa_dominios.empresa_nome, EXCLUDED.empresa_nome),
              holding_cnpj      = EXCLUDED.holding_cnpj,
              holding_nome      = EXCLUDED.holding_nome,
              holding_dominio   = EXCLUDED.holding_dominio,
              validacao_metodo  = EXCLUDED.validacao_metodo,
              validacao_data    = NOW()
          WHERE empresa_dominios.validacao_metodo NOT IN
            ('manual_chat_v6', 'manual_chat_v7_correcao', 'manual_chat_v9_dominios')
        """,
        (cnpj, empresa[:255], holding_cnpj, holding_nome[:255], holding_dominio),
    )


def persist_holding_own_row(cur, holding_cnpj: str, holding_nome: str, holding_dominio: str):
    """Garante que a holding tem row própria com dominio (cache pra próximas subs)."""
    cur.execute(
        """
        INSERT INTO empresa_dominios
            (cnpj, empresa_nome, dominio, fonte, confianca,
             validacao_metodo, validacao_data)
        VALUES (%s, %s, %s, 'serper_auto', 3,
                'populate_dominios_holding_seed_v1', NOW())
        ON CONFLICT (cnpj) DO UPDATE
          SET dominio = COALESCE(empresa_dominios.dominio, EXCLUDED.dominio),
              empresa_nome = COALESCE(empresa_dominios.empresa_nome, EXCLUDED.empresa_nome),
              validacao_data = NOW()
          WHERE empresa_dominios.validacao_metodo NOT IN
            ('manual_chat_v6', 'manual_chat_v7_correcao', 'manual_chat_v9_dominios')
        """,
        (holding_cnpj, holding_nome[:255], holding_dominio),
    )


def run_holding_fallback(cur, conn, serper_key: str, log: logging.Logger,
                          capex_floor: int, limit: int, sleep_s: float, commit: bool):
    cands = fetch_candidates_holding(cur, capex_floor, limit)
    log.info(f"holding-fallback candidatos={len(cands)}")
    if not cands:
        return

    resolved = 0
    skipped_motivos = {}
    for cnpj, empresa, capex in cands:
        capex_bi = (capex or 0) / 1_000_000_000
        qsa = _brasilapi_qsa(cnpj, log)
        if not qsa:
            skipped_motivos['brasilapi_falhou'] = skipped_motivos.get('brasilapi_falhou', 0) + 1
            log.info(f"SKIP cnpj={cnpj} capex=R${capex_bi:.2f}bi motivo=brasilapi_falhou empresa='{empresa[:60]}'")
            time.sleep(sleep_s)
            continue
        socio = _socio_pj_controlador(qsa)
        if not socio:
            skipped_motivos['qsa_sem_socio_pj'] = skipped_motivos.get('qsa_sem_socio_pj', 0) + 1
            log.info(f"SKIP cnpj={cnpj} capex=R${capex_bi:.2f}bi motivo=qsa_sem_socio_pj empresa='{empresa[:60]}'")
            time.sleep(sleep_s)
            continue
        holding_cnpj, holding_nome = socio
        holding_dom = lookup_holding_dominio(cur, holding_cnpj)
        motivo = 'cache_hit'
        if not holding_dom and holding_nome:
            holding_dom = search_domain(holding_nome, serper_key, log)
            motivo = 'discover' if holding_dom else 'discover_falhou'
        if not holding_dom:
            skipped_motivos[motivo] = skipped_motivos.get(motivo, 0) + 1
            log.info(f"SKIP cnpj={cnpj} capex=R${capex_bi:.2f}bi motivo={motivo} holding='{holding_nome[:50]}'")
            time.sleep(sleep_s)
            continue
        if commit:
            if motivo == 'discover':
                persist_holding_own_row(cur, holding_cnpj, holding_nome, holding_dom)
            persist_holding_subsidiary(cur, cnpj, empresa, holding_cnpj, holding_nome, holding_dom)
            conn.commit()
        resolved += 1
        log.info(
            f"RESOLVED cnpj={cnpj} capex=R${capex_bi:.2f}bi empresa='{empresa[:50]}' "
            f"-> holding={holding_nome[:40]} ({holding_cnpj}) dominio={holding_dom} via={motivo}"
        )
        time.sleep(sleep_s)

    log.info(f"holding-fallback done resolved={resolved}/{len(cands)} committed={commit} skipped={skipped_motivos}")


def persist_dominio(cur, cnpj: str, empresa: str, dominio: str):
    cur.execute(
        """
        INSERT INTO empresa_dominios
            (cnpj, empresa_nome, dominio, fonte, confianca, validacao_metodo, validacao_data)
        VALUES (%s, %s, %s, 'serper_auto', 3, 'populate_dominios_v1', NOW())
        ON CONFLICT (cnpj) DO UPDATE
          SET dominio = EXCLUDED.dominio,
              empresa_nome = EXCLUDED.empresa_nome,
              fonte = EXCLUDED.fonte,
              confianca = EXCLUDED.confianca,
              validacao_metodo = EXCLUDED.validacao_metodo,
              validacao_data = NOW()
          WHERE empresa_dominios.validacao_metodo NOT IN
            ('manual_chat_v6', 'manual_chat_v7_correcao', 'manual_chat_v9_dominios')
        """,
        (cnpj, empresa, dominio),
    )


def main():
    parser = argparse.ArgumentParser(description='Popula empresa_dominios para obras de alto CAPEX.')
    parser.add_argument('--commit', action='store_true', help='Persistir no banco (default: dry-run).')
    parser.add_argument('--limit', type=int, default=50, help='Máximo de empresas (default: 50).')
    parser.add_argument('--capex-floor', type=int, default=CAPEX_FLOOR_DEFAULT,
                        help=f'Piso de valor_estimado em reais (default: {CAPEX_FLOOR_DEFAULT:,}).')
    parser.add_argument('--sleep', type=float, default=1.5, help='Sleep entre buscas Serper (s).')
    parser.add_argument('--holding-fallback', action='store_true',
                        help='Modo alternativo: BrasilAPI QSA → holding dominio (v1.4.7 pattern).')
    parser.add_argument('--cnpj-in', type=str, default='',
                        help='CSV de CNPJs (sobrepõe filtro de capex_floor).')
    args = parser.parse_args()
    cnpj_in_list = [c.strip() for c in args.cnpj_in.split(',') if c.strip()] or None

    log = setup_logging()
    serper_key = os.environ.get('SERPER_API_KEY')
    if not serper_key:
        log.error('SERPER_API_KEY não definido')
        sys.exit(2)

    mode_str = 'HOLDING-FALLBACK' if args.holding_fallback else 'DISCOVER'
    log.info(
        f"start mode={mode_str}/{'COMMIT' if args.commit else 'DRY-RUN'} "
        f"limit={args.limit} capex_floor={args.capex_floor:,}"
    )

    conn = psycopg2.connect(
        host=os.environ['DB_HOST'],
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        dbname=os.environ['DB_NAME'],
    )
    cur = conn.cursor()

    if args.holding_fallback:
        run_holding_fallback(cur, conn, serper_key, log,
                              args.capex_floor, args.limit, args.sleep, args.commit)
        cur.close()
        conn.close()
        return

    candidatos = fetch_candidates(cur, args.capex_floor, args.limit, cnpj_in=cnpj_in_list)
    log.info(f"candidatos={len(candidatos)}")
    if not candidatos:
        log.info('nada a fazer')
        cur.close()
        conn.close()
        return

    found = 0
    not_found = []
    for cnpj, empresa, capex in candidatos:
        capex_bi = (capex or 0) / 1_000_000_000
        dominio = search_domain(empresa, serper_key, log)
        if dominio:
            if args.commit:
                persist_dominio(cur, cnpj, empresa, dominio)
                conn.commit()
            found += 1
            log.info(f"FOUND cnpj={cnpj} capex=R${capex_bi:.2f}bi empresa='{empresa[:60]}' -> {dominio}")
        else:
            not_found.append((cnpj, empresa, capex_bi))
            log.info(f"NOT_FOUND cnpj={cnpj} capex=R${capex_bi:.2f}bi empresa='{empresa[:60]}'")
        time.sleep(args.sleep)

    log.info(f"done found={found}/{len(candidatos)} not_found={len(not_found)} committed={args.commit}")
    if not_found:
        log.info('=== NOT_FOUND ===')
        for cnpj, empresa, capex_bi in not_found:
            log.info(f"  {cnpj} R${capex_bi:.2f}bi {empresa[:80]}")

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
