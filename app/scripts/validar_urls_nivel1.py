#!/usr/bin/env python3
"""
Validador de Obras — Nivel 1: Existencia de URL
Escopo: obras com classificacao_computed IN ('OURO','PRATA','PIPELINE')
Arquitetura URL-centric: valida URLs unicas, propaga validacao_obra_at para as obras.
Idempotente. --dry-run simula sem salvar.
"""
import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import asyncpg
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "host":     os.getenv("DB_HOST", "db"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "database": os.getenv("DB_NAME", "wins_hub"),
}

REVALIDACAO_DIAS = {
    "dado_aberto_csv":  14,
    "dado_aberto_api":  14,
    "portal_licitacao":  7,
    "noticia":          30,
    "portal_html":      14,
}

# HEAD retorna 405 nesses dominios -> usar GET
FORCE_GET_DOMAINS = {
    "dados.agricultura.gov.br",
    "aquarela.antaq.gov.br",
    "agenciainfra.com",
}

# Requerem Playwright (Nivel 3 futuro) -> marcar como nao_validavel
PLAYWRIGHT_REQUIRED = {
    "bllcompras.com",
    "portaldecompras.recife.pe.gov.br",
}

# Dominios cujo WAF bloqueia httpx mas flaresolverr passa (validado 17/05/2026)
# Usa endswith - captura subdominios automaticamente
FLARESOLVERR_DOMAINS = {
    "bol.uol.com.br",
    "capitalnews.com.br",
    "canalrural.com.br",
    "gmconline.com.br",
    "clickpetroleoegas.com.br",
    "imaq.diretriz.net",
    "licitanet.com.br",
}

FLARESOLVERR_URL = "http://wins_hub-flaresolverr:8191/v1"

# Sites com sessao/token visitante que expiram - tratar como nao_validavel no Nivel 1
# Usa endswith para capturar todas as variantes: licitanet.com.br, portal.licitanet.com.br, app2.licitardigital.com.br
SESSION_REQUIRED_SUFFIXES = {"unica.com.br", "aquarela.antaq.gov.br", 
    "licitardigital.com.br",
}

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def classificar_status(code, error=None):
    if error == "dns_fail": return "dns_fail"
    if error == "timeout":  return "timeout"
    if code is None:
        return "erro_conexao"
    if code in (200, 301, 302, 307, 308):
        return "ok"
    if code == 404:
        return "404"
    if code >= 500:
        return "500"
    return f"http_{code}"


async def checar_url(client, url, tipo):
    # Fix 1: prefixar schema ausente (URLs tipo "www.X.com.br/...")
    if url and not url.startswith("http"):
        url = "https://" + url

    domain = urlparse(url).netloc

    if domain in PLAYWRIGHT_REQUIRED:
        return {"status": "nao_validavel", "http_code": None}
    if any(domain.endswith(s) for s in SESSION_REQUIRED_SUFFIXES):
        return {"status": "nao_validavel", "http_code": None}

    # FlareSolverr para dominios com WAF anti-bot (validado 17/05/2026)
    if any(domain.endswith(d) for d in FLARESOLVERR_DOMAINS):
        try:
            r = await client.post(FLARESOLVERR_URL,
                                  json={"cmd":"request.get","url":url,"maxTimeout":30000},
                                  timeout=45)
            j = r.json()
            sol = j.get("solution", {}) or {}
            code = sol.get("status")
            if j.get("status") == "ok" and code == 200:
                return {"status": "ok", "http_code": 200}
            if code:
                return {"status": classificar_status(code, None), "http_code": code}
        except Exception:
            pass
        return {"status": "flare_erro", "http_code": None}

    method = "GET" if domain in FORCE_GET_DOMAINS else "HEAD"

    try:
        r = await client.request(method, url, timeout=TIMEOUT,
                                 headers=HEADERS, follow_redirects=True)
        # Fix 2: fallback GET se HEAD retornou 403 (WAFs costumam aceitar GET com UA browser)
        if r.status_code == 403 and method == "HEAD":
            r = await client.get(url, timeout=TIMEOUT, headers=HEADERS,
                                 follow_redirects=True)
        return {"status": classificar_status(r.status_code, None), "http_code": r.status_code}
    except httpx.ConnectError:
        return {"status": "dns_fail",     "http_code": None}
    except httpx.TimeoutException:
        return {"status": "timeout",      "http_code": None}
    except Exception:
        return {"status": "erro_conexao", "http_code": None}


async def main(batch, dry_run):
    now = datetime.now(timezone.utc)
    stats = {
        "total_urls": 0, "ok": 0, "falha": 0, "nao_validavel": 0,
        "obras_propagadas": 0, "circuit_breakers": [],
        "dry_run": dry_run, "inicio": now.isoformat(),
    }

    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        # Auto-evolutivo: garante que URLs novas do orchestrator entrem na fila
        novas = await conn.fetchval("""
            WITH ins AS (
                INSERT INTO urls_fonte_validacao (url_fonte, tipo_url)
                SELECT DISTINCT url_fonte,
                    CASE
                        WHEN url_fonte ILIKE '%.csv%'         THEN 'dado_aberto_csv'
                        WHEN url_fonte ILIKE '%dadosabertos%' THEN 'dado_aberto_api'
                        WHEN url_fonte ILIKE '%api.%'         THEN 'dado_aberto_api'
                        WHEN url_fonte ILIKE '%licitacao%' OR url_fonte ILIKE '%compras%' OR url_fonte ILIKE '%portal%' THEN 'portal_licitacao'
                        WHEN url_fonte ILIKE '%journal%'   OR url_fonte ILIKE '%news%'    OR url_fonte ILIKE '%agencia%' THEN 'noticia'
                        ELSE 'portal_html'
                    END
                FROM obras
                WHERE classificacao_computed IN ('OURO','PRATA','PIPELINE')
                  AND url_fonte IS NOT NULL
                ON CONFLICT (url_fonte) DO NOTHING
                RETURNING 1
            )
            SELECT COUNT(*) FROM ins
        """)
        if novas:
            log.info(f"+{novas} URLs novas adicionadas a fila (auto-evolutivo)")
            stats["urls_novas_adicionadas"] = novas

        rows = await conn.fetch("""
            SELECT url_fonte, tipo_url
            FROM urls_fonte_validacao
            WHERE existencia_status IS NULL
               OR proxima_revalidacao <= NOW()
            ORDER BY proxima_revalidacao ASC NULLS FIRST
            LIMIT $1
        """, batch)

        log.info(f"URLs a processar: {len(rows)} | dry_run={dry_run}")

        async with httpx.AsyncClient() as client:
            for row in rows:
                url, tipo = row["url_fonte"], row["tipo_url"]
                stats["total_urls"] += 1

                res = await checar_url(client, url, tipo)
                status = res["status"]
                log.info(f"[{status.upper():15}] {url[:90]}")

                if status == "ok":
                    stats["ok"] += 1
                elif status == "nao_validavel":
                    stats["nao_validavel"] += 1
                else:
                    stats["falha"] += 1

                if dry_run:
                    await asyncio.sleep(0.5)
                    continue

                dias = REVALIDACAO_DIAS.get(tipo, 14)
                proxima = now + timedelta(days=dias if status == "ok" else dias // 2)

                falhas_ant = await conn.fetchval(
                    "SELECT COALESCE(tentativas_consecutivas_falha,0) FROM urls_fonte_validacao WHERE url_fonte=$1",
                    url,
                )
                novas_falhas = 0 if status == "ok" else falhas_ant + 1

                await conn.execute("""
                    UPDATE urls_fonte_validacao SET
                        existencia_status             = $2,
                        existencia_http_code          = $3,
                        existencia_validada_at        = $4,
                        proxima_revalidacao           = $5,
                        tentativas_consecutivas_falha = $6,
                        updated_at                    = $4
                    WHERE url_fonte = $1
                """, url, status, res["http_code"], now, proxima, novas_falhas)

                # Conta obras afetadas no escopo antes de decidir propagacao
                n_obras_url = await conn.fetchval(
                    "SELECT COUNT(*) FROM obras "
                    "WHERE url_fonte=$1 AND classificacao_computed IN ('OURO','PRATA','PIPELINE')",
                    url,
                ) or 0

                # Circuit breaker: URL gigante com 3+ falhas seguidas nao propaga em massa
                if status != "ok" and novas_falhas >= 3 and n_obras_url > 500:
                    log.warning(
                        f"CIRCUIT BREAKER: {url[:60]} ({n_obras_url} obras protegidas, "
                        f"{novas_falhas} falhas consecutivas)"
                    )
                    stats["circuit_breakers"].append({
                        "url": url, "obras_protegidas": n_obras_url, "falhas": novas_falhas,
                    })
                else:
                    # Propaga: so validacao_obra_at; obra_listada_na_fonte fica NULL ate Nivel 2
                    await conn.execute("""
                        UPDATE obras SET validacao_obra_at = $2
                        WHERE url_fonte = $1
                          AND classificacao_computed IN ('OURO','PRATA','PIPELINE')
                    """, url, now)
                    stats["obras_propagadas"] += n_obras_url

                await asyncio.sleep(0.5)
    finally:
        await conn.close()

    stats["fim"] = datetime.now(timezone.utc).isoformat()
    print("\n=== STATS_JSON ===")
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=999)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.batch, args.dry_run))
