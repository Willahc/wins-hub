#!/usr/bin/env python3
"""Enrichment V5 — E1 re-finder + E2 dominio agressivo + E3 domain search bulk.

Roda E1 → E2 → E3 sequencial num único processo.
Schema real: obras.empresa (não razao_social), empresa_dominios.dominio (não dominio_principal).

Pools Hunter:
  - 'searches' = Domain Search + Email Finder
  - 'verifications' = Email Verifier
Floors: searches < 500 → para; verifications < 1000 → para verifier
"""
from __future__ import annotations
import atexit
import json as _json
import logging
import os
import re
import sys
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, "/app")

import psycopg2
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("enrich_v5")

DB = dict(host=os.getenv("DB_HOST", "db"), port=int(os.getenv("DB_PORT", "5432")),
          dbname=os.getenv("DB_NAME", "wins_hub"), user=os.getenv("DB_USER", "postgres"),
          password=os.getenv("DB_PASSWORD", ""))
HUNTER_KEY = os.getenv("HUNTER_API_KEY", "")
SERPER_KEY = os.getenv("SERPER_API_KEY", "")
SEARCH_FLOOR = 500
VERIFY_FLOOR = 1000
DEADLINE = time.time() + 4 * 3600

STATS = {
    "e1": {"candidatos": 0, "tentados": 0, "recuperados": 0, "iguais": 0, "score_baixo": 0,
           "verify_falhou": 0, "hunter_usado_search": 0, "hunter_usado_verify": 0},
    "e2": {"candidatos": 0, "tentados": 0, "dominio_achado": 0, "dominio_falhou": 0,
           "email_achado": 0, "email_validado": 0, "hunter_usado_search": 0,
           "hunter_usado_verify": 0},
    "e3": {"dominios": 0, "contatos_inseridos": 0, "validos": 0, "hunter_usado_search": 0},
    "tempo_seg": 0,
}
T_START = time.time()


@atexit.register
def _emit_stats():
    STATS["tempo_seg"] = round(time.time() - T_START, 1)
    print(f"STATS_JSON: {_json.dumps(STATS)}", flush=True)


def hunter_saldo(tipo: str = "verifications") -> int:
    if not HUNTER_KEY:
        return 0
    try:
        r = requests.get(f"https://api.hunter.io/v2/account?api_key={HUNTER_KEY}", timeout=10)
        reqs = r.json().get("data", {}).get("requests", {})
        bucket = reqs.get(tipo, {})
        return int(bucket.get("available", 0)) - int(bucket.get("used", 0))
    except Exception:
        return -1


def hunter_email_finder(domain: str, full_name: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get("https://api.hunter.io/v2/email-finder",
                         params={"domain": domain, "full_name": full_name, "api_key": HUNTER_KEY},
                         timeout=20)
        if r.status_code == 200:
            return r.json().get("data") or None
    except Exception as e:
        log.warning(f"  finder erro {domain}/{full_name}: {e}")
    return None


def hunter_email_verifier(email: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get("https://api.hunter.io/v2/email-verifier",
                         params={"email": email, "api_key": HUNTER_KEY}, timeout=20)
        if r.status_code in (200, 222):
            return r.json().get("data") or None
    except Exception as e:
        log.warning(f"  verifier erro {email}: {e}")
    return None


def hunter_domain_search(domain: str, limit: int = 25) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get("https://api.hunter.io/v2/domain-search",
                         params={"domain": domain, "limit": limit, "type": "personal",
                                 "api_key": HUNTER_KEY}, timeout=30)
        if r.status_code == 200:
            return r.json().get("data") or None
    except Exception as e:
        log.warning(f"  domain-search erro {domain}: {e}")
    return None


def deadline_excedido() -> bool:
    if time.time() > DEADLINE:
        log.error("DEADLINE 4h excedido — abortando")
        return True
    return False


# ═══════════════════════════════ E1 ═══════════════════════════════
def processar_e1(conn) -> None:
    log.info("─" * 60)
    log.info("E1 — Re-Email-Finder dos inválidos (smtp_verified=false c/ dominio)")
    log.info("─" * 60)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT o.id, o.cnpj, o.empresa, o.nivel1_nome,
                   o.nivel1_email, o.classificacao_computed,
                   COALESCE(ed.dominio, ed.holding_dominio) AS dominio
            FROM obras o
            JOIN empresa_dominios ed ON ed.cnpj = o.cnpj
            WHERE o.classificacao_computed IN ('OURO','PRATA')
              AND o.nivel1_nome IS NOT NULL AND o.nivel1_nome != ''
              AND o.nivel1_email IS NOT NULL
              AND o.nivel1_email_smtp_verified = false
              AND COALESCE(ed.dominio, ed.holding_dominio) IS NOT NULL
              AND o.nivel1_nome NOT IN ('Contato Comercial','RH','Comercial','Atendimento','-','N/A')
            ORDER BY CASE o.classificacao_computed WHEN 'OURO' THEN 1 ELSE 2 END,
                     o.valor_estimado DESC NULLS LAST
        """)
        rows = cur.fetchall()
    STATS["e1"]["candidatos"] = len(rows)
    log.info(f"E1: {len(rows)} candidatos")

    for i, (obra_id, cnpj, empresa, nome, email_antigo, classif, dominio) in enumerate(rows, 1):
        if deadline_excedido():
            return
        if i % 25 == 0:
            s = hunter_saldo("searches")
            log.info(f"  [E1 {i}/{len(rows)}] searches={s} stats={STATS['e1']}")
            if s < SEARCH_FLOOR:
                log.warning(f"  searches<{SEARCH_FLOOR} — parando E1")
                return

        STATS["e1"]["tentados"] += 1
        STATS["e1"]["hunter_usado_search"] += 1
        d = hunter_email_finder(dominio, nome)
        if not d or not d.get("email"):
            continue
        email_novo = d["email"].lower().strip()
        score = int(d.get("score") or 0)
        if email_novo == (email_antigo or "").lower().strip():
            STATS["e1"]["iguais"] += 1
            continue
        if score < 50:
            STATS["e1"]["score_baixo"] += 1
            continue
        # verifica novo email
        v_saldo = hunter_saldo("verifications")
        if v_saldo < VERIFY_FLOOR:
            log.warning(f"  verifications<{VERIFY_FLOOR} — pulando verifier E1")
            continue
        STATS["e1"]["hunter_usado_verify"] += 1
        v = hunter_email_verifier(email_novo)
        if not v:
            continue
        v_status = (v.get("status") or v.get("result") or "unknown").lower()
        v_score = int(v.get("score") or score)
        valido = v_status in ("valid", "accept_all") and v_score >= 60
        if not valido:
            STATS["e1"]["verify_falhou"] += 1
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE obras SET
                        nivel1_email=%s, nivel1_email_score=%s,
                        nivel1_email_smtp_verified=true,
                        nivel1_email_verified_at=NOW(),
                        nivel1_email_status=%s,
                        nivel1_origem_enrichment=COALESCE(nivel1_origem_enrichment,'')||'+E1_refinder',
                        nivel1_enrichment_data=NOW()
                    WHERE id=%s
                """, (email_novo, v_score, v_status, obra_id))
            conn.commit()
            STATS["e1"]["recuperados"] += 1
            log.info(f"  ✓ E1 {classif} {empresa[:40]}: {email_antigo} → {email_novo} score={v_score}")
        except Exception as e:
            log.warning(f"  E1 update {obra_id}: {e}")
            conn.rollback()
        time.sleep(0.8)


# ═══════════════════════════════ E2 ═══════════════════════════════
SUFFIX_LIMPAR = re.compile(
    r"\b(s/?a|ltda|me|epp|eireli|s\.a\.|s\.a|sa|holding|grupo|cia|companhia|"
    r"engenharia|construções|construcao|construcoes|empreendimentos|"
    r"concessionaria|concessionária|consorcio|consórcio|participacoes|participações)\b",
    re.IGNORECASE,
)


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9 ]", " ", s).lower()
    s = SUFFIX_LIMPAR.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _validar_dominio(d: str) -> bool:
    """GET com User-Agent, aceita 200-499, rejeita 5xx/000."""
    for proto in ("https", "http"):
        try:
            r = requests.head(f"{proto}://{d}", timeout=8, allow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/147"})
            if 200 <= r.status_code < 500:
                return True
        except requests.RequestException:
            continue
    # fallback GET (HEAD pode ser bloqueado)
    for proto in ("https", "http"):
        try:
            r = requests.get(f"{proto}://{d}", timeout=10, allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/147"})
            if 200 <= r.status_code < 500:
                return True
        except requests.RequestException:
            continue
    return False


def _serper_search(query: str, num: int = 10) -> List[Dict[str, Any]]:
    if not SERPER_KEY:
        return []
    try:
        r = requests.post("https://google.serper.dev/search",
                          headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
                          json={"q": query, "num": num, "gl": "br", "hl": "pt"},
                          timeout=15)
        if r.status_code == 200:
            return (r.json().get("organic") or [])
    except Exception as e:
        log.warning(f"  serper erro: {e}")
    return []


AGREGADORES = {"wikipedia.org", "linkedin.com", "facebook.com", "instagram.com",
               "twitter.com", "x.com", "youtube.com", "tiktok.com", "gov.br",
               "cnpj.biz", "casadosdados.com.br", "econodata.com.br",
               "consultas-cnpj.com.br", "guiamais.com.br", "telelistas.net",
               "reclameaqui.com.br", "jusbrasil.com.br", "google.com",
               "yahoo.com", "bing.com", "blogspot.com", "wordpress.com"}


def _dom_from_url(url: str) -> Optional[str]:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return None


def _is_aggregator(d: str) -> bool:
    return any(d == a or d.endswith("." + a) for a in AGREGADORES)


def descobrir_dominio_agressivo(cnpj: str, razao: str, cargo: Optional[str]) -> Optional[str]:
    razao = razao or ""
    cargo = cargo or ""

    # estratégia 1: extrair empresa do cargo (ex: "Diretor AENA Brasil")
    if cargo:
        # pegar palavras MAIÚSCULAS de 3+ letras do cargo
        empresas_no_cargo = re.findall(r"\b[A-Z]{3,}(?:\s+[A-Z][a-zA-Z]+)?\b", cargo)
        for emp in empresas_no_cargo[:3]:
            slug_emp = _slug(emp).replace(" ", "")
            if slug_emp and len(slug_emp) >= 3:
                for tld in (".com.br", ".com"):
                    cand = f"{slug_emp}{tld}"
                    if _validar_dominio(cand):
                        log.info(f"  achei via cargo: {cand}")
                        return cand

    # estratégia 2: slug da razao + primeira palavra
    slug = _slug(razao)
    if slug:
        # razao concatenada
        for variant in (slug.replace(" ", ""), slug.split(" ")[0] if slug else "",
                        f"grupo{slug.split(' ')[0]}" if slug else ""):
            if not variant or len(variant) < 3:
                continue
            for tld in (".com.br", ".com"):
                cand = f"{variant}{tld}"
                if _validar_dominio(cand):
                    log.info(f"  achei via slug: {cand}")
                    return cand

    # estratégia 3: SearchChain via serper - site oficial
    queries = [f'"{razao}" site oficial']
    if cargo:
        # extrair "empresa" do cargo: depois de palavras tipo "—", "em", "da", etc
        m = re.search(r"(?:—|–|-|em|na|no|d[aoe]s?)\s+([A-Z][\w\s&]{2,40})", cargo)
        if m:
            queries.append(f'"{m.group(1).strip()}" site oficial')
    for q in queries[:2]:
        results = _serper_search(q, num=15)
        seen = set()
        for r in results:
            link = r.get("link") or ""
            d = _dom_from_url(link)
            if not d or d in seen or _is_aggregator(d):
                continue
            seen.add(d)
            if not any(d.endswith(t) for t in (".com.br", ".com", ".org.br", ".ind.br",
                                                ".net.br", ".net")):
                continue
            slug_concat = slug.replace(" ", "")
            core = re.sub(r"\.[a-z\.]+$", "", d).replace(".", "")
            if slug_concat and core and (slug_concat in core or core in slug_concat
                                          or slug.split(" ")[0] in core if slug else False):
                if _validar_dominio(d):
                    log.info(f"  achei via serper: {d}")
                    return d

    # estratégia 4: SearchChain via serper - LinkedIn company
    if SERPER_KEY:
        q_li = f'"{razao}" site:linkedin.com/company'
        results = _serper_search(q_li, num=10)
        for r in results[:5]:
            snippet = (r.get("snippet") or "") + " " + (r.get("title") or "")
            urls = re.findall(r"https?://[^\s\"'<>]+", snippet)
            for u in urls:
                d = _dom_from_url(u)
                if d and not _is_aggregator(d) and _validar_dominio(d):
                    log.info(f"  achei via linkedin: {d}")
                    return d
    return None


def processar_e2(conn) -> None:
    log.info("─" * 60)
    log.info("E2 — Camada 2 agressiva (sem dominio + sem email)")
    log.info("─" * 60)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT o.id, o.cnpj, o.empresa, o.nivel1_nome, o.nivel1_cargo,
                   o.classificacao_computed, o.valor_estimado
            FROM (
              SELECT DISTINCT ON (cnpj) id, cnpj, empresa, nivel1_nome, nivel1_cargo,
                     classificacao_computed, valor_estimado
              FROM obras
              WHERE classificacao_computed IN ('OURO','PRATA')
                AND nivel1_nome IS NOT NULL AND nivel1_nome != ''
                AND nivel1_email IS NULL
                AND nivel1_nome NOT IN ('Contato Comercial','RH','Comercial','Atendimento','-','N/A')
                AND cnpj_valido(cnpj) = TRUE
              ORDER BY cnpj, valor_estimado DESC NULLS LAST
            ) o
            LEFT JOIN empresa_dominios ed ON ed.cnpj = o.cnpj
            WHERE (ed.dominio IS NULL OR ed.dominio = '')
              AND (ed.holding_dominio IS NULL OR ed.holding_dominio = '')
            ORDER BY CASE o.classificacao_computed WHEN 'OURO' THEN 1 ELSE 2 END,
                     o.valor_estimado DESC NULLS LAST
        """)
        rows = cur.fetchall()
    STATS["e2"]["candidatos"] = len(rows)
    log.info(f"E2: {len(rows)} CNPJs sem dominio")

    for i, (obra_id, cnpj, empresa, nome, cargo, classif, valor) in enumerate(rows, 1):
        if deadline_excedido():
            return
        if i % 10 == 0:
            s = hunter_saldo("searches")
            log.info(f"  [E2 {i}/{len(rows)}] searches={s} stats={STATS['e2']}")
            if s < SEARCH_FLOOR:
                log.warning(f"  searches<{SEARCH_FLOOR} — parando E2")
                return
        STATS["e2"]["tentados"] += 1
        log.info(f"  --- E2 [{i}/{len(rows)}] {classif} {cnpj} {(empresa or '')[:45]}")
        log.info(f"      decisor: {nome} | {cargo}")

        try:
            dominio = descobrir_dominio_agressivo(cnpj, empresa, cargo)
        except Exception as e:
            log.warning(f"      descobrir erro: {e}")
            dominio = None
        if not dominio:
            STATS["e2"]["dominio_falhou"] += 1
            continue
        STATS["e2"]["dominio_achado"] += 1

        # persistir
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO empresa_dominios (
                        cnpj, empresa_nome, dominio, fonte, confianca, dominio_status,
                        validacao_metodo, validacao_data
                    ) VALUES (%s, %s, %s, 'E2_agressivo', 3, 'ok',
                              'E2_agressivo', NOW()::date)
                    ON CONFLICT (cnpj) DO UPDATE SET
                        dominio=COALESCE(empresa_dominios.dominio, EXCLUDED.dominio),
                        validacao_metodo=EXCLUDED.validacao_metodo,
                        validacao_data=EXCLUDED.validacao_data,
                        atualizado_em=NOW()
                """, (cnpj, (empresa or "")[:255], dominio))
            conn.commit()
        except Exception as e:
            log.warning(f"      persist dominio erro: {e}")
            conn.rollback()

        # Email Finder
        STATS["e2"]["hunter_usado_search"] += 1
        d_finder = hunter_email_finder(dominio, nome)
        if not d_finder or not d_finder.get("email"):
            time.sleep(2)
            continue
        email = d_finder["email"]
        score = int(d_finder.get("score") or 0)
        if score < 50:
            time.sleep(2)
            continue
        STATS["e2"]["email_achado"] += 1

        # Verifier
        v_saldo = hunter_saldo("verifications")
        if v_saldo < VERIFY_FLOOR:
            log.warning(f"      verifications<{VERIFY_FLOOR} — gravar sem verifier")
            valido = False
            v_status = "unverified"
            v_score = score
        else:
            STATS["e2"]["hunter_usado_verify"] += 1
            v = hunter_email_verifier(email)
            v_status = "unknown"
            v_score = score
            if v:
                v_status = (v.get("status") or v.get("result") or "unknown").lower()
                v_score = int(v.get("score") or score)
            valido = v_status in ("valid", "accept_all") and v_score >= 60

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE obras SET
                        nivel1_email=%s, nivel1_email_score=%s,
                        nivel1_email_smtp_verified=%s,
                        nivel1_email_verified_at=NOW(),
                        nivel1_email_status=%s,
                        nivel1_origem_enrichment=COALESCE(nivel1_origem_enrichment,'')||'+E2_complete',
                        nivel1_enrichment_data=NOW()
                    WHERE cnpj=%s AND nivel1_nome=%s AND nivel1_email IS NULL
                """, (email, v_score, valido, v_status, cnpj, nome))
            conn.commit()
            if valido:
                STATS["e2"]["email_validado"] += 1
                log.info(f"      ✓ {classif} {email} score={v_score}")
        except Exception as e:
            log.warning(f"      update obras erro: {e}")
            conn.rollback()
        time.sleep(2)


# ═══════════════════════════════ E3 ═══════════════════════════════
def processar_e3(conn) -> None:
    log.info("─" * 60)
    log.info("E3 — Domain Search bulk top 50 dominios")
    log.info("─" * 60)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contatos_alternativos (
              id SERIAL PRIMARY KEY,
              cnpj TEXT, empresa_dominio TEXT, email TEXT UNIQUE,
              nome TEXT, cargo TEXT, departamento TEXT, linkedin_url TEXT,
              hunter_score INT, hunter_status TEXT,
              origem TEXT DEFAULT 'E3_domain_search',
              descoberto_em TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_contatos_alt_dominio ON contatos_alternativos(empresa_dominio);
            CREATE INDEX IF NOT EXISTS idx_contatos_alt_cnpj ON contatos_alternativos(cnpj);
        """)
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(ed.dominio, ed.holding_dominio) AS dom,
                   COUNT(o.id) AS qtd,
                   SUM(o.valor_estimado) AS capex,
                   STRING_AGG(DISTINCT o.cnpj, ',') AS cnpjs
            FROM obras o
            JOIN empresa_dominios ed ON ed.cnpj = o.cnpj
            WHERE o.classificacao_computed IN ('OURO','PRATA')
              AND COALESCE(ed.dominio, ed.holding_dominio) IS NOT NULL
              AND COALESCE(ed.dominio, ed.holding_dominio) NOT IN
                  ('gmail.com','hotmail.com','outlook.com','yahoo.com.br','yahoo.com',
                   'uol.com.br','bol.com.br','terra.com.br','ig.com.br','live.com')
            GROUP BY COALESCE(ed.dominio, ed.holding_dominio)
            ORDER BY capex DESC NULLS LAST
            LIMIT 50
        """)
        domains = cur.fetchall()
    STATS["e3"]["dominios"] = len(domains)
    log.info(f"E3: {len(domains)} dominios top capex")

    for i, (dom, qtd, capex, cnpjs_csv) in enumerate(domains, 1):
        if deadline_excedido():
            return
        if i % 10 == 0:
            s = hunter_saldo("searches")
            log.info(f"  [E3 {i}/{len(domains)}] searches={s} stats={STATS['e3']}")
            if s < SEARCH_FLOOR:
                log.warning(f"  searches<{SEARCH_FLOOR} — parando E3")
                return
        STATS["e3"]["hunter_usado_search"] += 1
        log.info(f"  [E3 {i}/{len(domains)}] {dom} qtd={qtd} capex={capex}")
        data = hunter_domain_search(dom, limit=25)
        if not data:
            time.sleep(1.5)
            continue
        emails = data.get("emails") or []
        first_cnpj = (cnpjs_csv or "").split(",")[0] if cnpjs_csv else None
        for em in emails:
            email = (em.get("value") or "").strip().lower()
            if not email:
                continue
            nome_full = " ".join(filter(None, [em.get("first_name"), em.get("last_name")])).strip() or None
            cargo = em.get("position") or None
            depto = em.get("department") or None
            li = em.get("linkedin") or None
            score = int(em.get("confidence") or 0)
            status = (em.get("verification") or {}).get("status") or em.get("type") or None
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO contatos_alternativos (
                            cnpj, empresa_dominio, email, nome, cargo, departamento,
                            linkedin_url, hunter_score, hunter_status, origem
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'E3_domain_search')
                        ON CONFLICT (email) DO NOTHING
                        RETURNING id
                    """, (first_cnpj, dom, email, nome_full, cargo, depto, li, score, status))
                    inserted = cur.fetchone()
                conn.commit()
                if inserted:
                    STATS["e3"]["contatos_inseridos"] += 1
                    if status and status.lower() in ("valid", "accept_all"):
                        STATS["e3"]["validos"] += 1
            except Exception as e:
                log.warning(f"  insert contato erro {email}: {e}")
                conn.rollback()
        time.sleep(1.5)


# ═══════════════════════════════ MAIN ═══════════════════════════════
def main() -> int:
    s_search = hunter_saldo("searches")
    s_verify = hunter_saldo("verifications")
    log.info(f"Hunter inicial: searches={s_search} verifications={s_verify}")
    if s_search < SEARCH_FLOOR:
        log.error(f"searches={s_search} < floor {SEARCH_FLOOR} — abort")
        return 2
    if s_verify < VERIFY_FLOOR:
        log.warning(f"verifications={s_verify} < floor {VERIFY_FLOOR} — verifier limitado")

    conn = psycopg2.connect(**DB)
    try:
        processar_e1(conn)
        if not deadline_excedido():
            processar_e2(conn)
        if not deadline_excedido():
            processar_e3(conn)
    finally:
        conn.close()

    s_search_fim = hunter_saldo("searches")
    s_verify_fim = hunter_saldo("verifications")
    log.info(f"FIM: searches={s_search_fim} (delta {s_search - s_search_fim}) "
             f"verifications={s_verify_fim} (delta {s_verify - s_verify_fim})")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException as e:
        import traceback as tb
        log.error(f"V5 UNCAUGHT {type(e).__name__}: {e}")
        log.error(tb.format_exc())
        rc = 1
    os._exit(rc or 0)
