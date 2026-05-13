#!/usr/bin/env python3
"""Enrichment V2 — qualidade-first. Processa CAT3 (sem email) + CAT2 (verifier).

Prioridade: CAT3 OURO desc valor → CAT3 PRATA desc valor → CAT2 OURO → CAT2 PRATA.
Para imediatamente se Hunter saldo < 200.

Hunter Email Finder: GET /v2/email-finder?domain=X&full_name=Y&api_key=Z
  Retorna { email, score, position, ... }
Hunter Verifier:     GET /v2/email-verifier?email=X&api_key=Y
  Retorna { result, score, status, regexp, gibberish, disposable, webmail }

STATS_JSON na ultima linha.
"""
from __future__ import annotations

import atexit
import json as _json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, Optional

sys.path.insert(0, "/app")

import psycopg2
import requests


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("enrich_v2")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}
HUNTER_KEY = os.getenv("HUNTER_API_KEY", "")
HUNTER_FLOOR = 200
SLEEP_S = 0.5

_STATS = {
    "buscados": 0, "novos": 0, "erros": 0,
    "cat3_processados": 0, "cat3_emails_encontrados": 0, "cat3_skip_sem_dominio": 0,
    "cat2_processados": 0, "cat2_validos": 0, "cat2_invalidos": 0, "cat2_webmail": 0,
    "hunter_usado_aprox": 0,
}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def hunter_saldo(tipo: str = "verifications") -> int:
    """Hunter API tem pools separados (a chave `calls` é deprecated/agregada).
    tipo='verifications' (Email Verifier) | 'searches' (Domain Search/Email Finder).
    Bug pré-V4: lia `calls.available - used` (deprecation_notice no payload).
    """
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
        r = requests.get(
            "https://api.hunter.io/v2/email-finder",
            params={"domain": domain, "full_name": full_name, "api_key": HUNTER_KEY},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("data") or None
    except Exception as e:
        log.warning(f"finder erro {domain}/{full_name}: {e}")
    return None


def hunter_email_verifier(email: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(
            "https://api.hunter.io/v2/email-verifier",
            params={"email": email, "api_key": HUNTER_KEY},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("data") or None
        if r.status_code == 222:  # Hunter retorna 222 pra alguns webmails
            return r.json().get("data") or {"status": "webmail", "score": 0}
    except Exception as e:
        log.warning(f"verifier erro {email}: {e}")
    return None


def dominio_de_obra(conn, cnpj: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(dominio, holding_dominio) FROM empresa_dominios
            WHERE cnpj=%s LIMIT 1
        """, (cnpj,))
        r = cur.fetchone()
    if r and r[0]:
        return r[0]
    return None


def processar_cat3(conn) -> None:
    """CAT3: tem decisor, sem email. Hunter Email Finder."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT o.id, o.cnpj, o.empresa, o.nivel1_nome, o.nivel1_cargo,
                   o.classificacao_computed, o.valor_estimado
            FROM obras o
            WHERE o.classificacao_computed IN ('OURO','PRATA')
              AND o.nivel1_nome IS NOT NULL AND o.nivel1_nome != ''
              AND o.nivel1_email IS NULL
              AND o.nivel1_nome NOT IN ('Contato Comercial','RH','Comercial','Atendimento','-','N/A')
            ORDER BY
              CASE o.classificacao_computed WHEN 'OURO' THEN 1 ELSE 2 END,
              o.valor_estimado DESC NULLS LAST
        """)
        rows = cur.fetchall()
    log.info(f"CAT3 (sem email): {len(rows)} obras")

    for i, (obra_id, cnpj, empresa, nome, cargo, classif, valor) in enumerate(rows, 1):
        _STATS["buscados"] += 1
        _STATS["cat3_processados"] += 1
        if i % 25 == 0:
            s = hunter_saldo("searches")  # Email Finder consome 'searches'
            log.info(f"  [CAT3 {i}/{len(rows)}] Hunter searches saldo: {s}")
            if s < HUNTER_FLOOR:
                log.warning(f"  Hunter abaixo do floor {HUNTER_FLOOR} — parando CAT3")
                return
        dom = dominio_de_obra(conn, cnpj)
        if not dom:
            _STATS["cat3_skip_sem_dominio"] += 1
            continue
        d = hunter_email_finder(dom, nome)
        _STATS["hunter_usado_aprox"] += 1
        if not d or not d.get("email"):
            continue
        email = d["email"]
        score = int(d.get("score") or 0)
        if score < 50:
            continue
        smtp_verified = score >= 80
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE obras SET
                        nivel1_email=%s,
                        nivel1_email_score=%s,
                        nivel1_email_smtp_verified=%s,
                        nivel1_email_verified_at=NOW(),
                        nivel1_email_status=%s,
                        nivel1_origem_enrichment='V2_email_finder',
                        nivel1_enrichment_data=NOW()
                    WHERE id=%s
                """, (email, score, smtp_verified,
                      ('valid' if smtp_verified else 'unverified'), obra_id))
            conn.commit()
            _STATS["cat3_emails_encontrados"] += 1
            _STATS["novos"] += 1
        except Exception as e:
            log.warning(f"  update {obra_id}: {e}")
            _STATS["erros"] += 1
            conn.rollback()
        time.sleep(SLEEP_S)


def processar_cat2(conn) -> None:
    """CAT2: tem email, sem verifier. Hunter Verifier (1 call cada)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT o.id, o.nivel1_email, o.classificacao_computed, o.valor_estimado
            FROM obras o
            WHERE o.classificacao_computed IN ('OURO','PRATA')
              AND o.nivel1_email IS NOT NULL
              AND (o.nivel1_email_smtp_verified IS NULL OR o.nivel1_email_smtp_verified = false)
              AND o.nivel1_email NOT LIKE '%@gmail.%'
              AND o.nivel1_email NOT LIKE '%@hotmail.%'
              AND o.nivel1_email NOT LIKE '%@outlook.%'
              AND o.nivel1_email NOT LIKE '%@yahoo.%'
            ORDER BY
              CASE o.classificacao_computed WHEN 'OURO' THEN 1 ELSE 2 END,
              o.valor_estimado DESC NULLS LAST
        """)
        rows = cur.fetchall()
    log.info(f"CAT2 (precisa verifier): {len(rows)} obras")

    for i, (obra_id, email, classif, valor) in enumerate(rows, 1):
        _STATS["buscados"] += 1
        _STATS["cat2_processados"] += 1
        if i % 50 == 0:
            s = hunter_saldo("verifications")  # Verifier consome 'verifications'
            log.info(f"  [CAT2 {i}/{len(rows)}] Hunter verifications saldo: {s}")
            if s < HUNTER_FLOOR:
                log.warning(f"  Hunter abaixo do floor {HUNTER_FLOOR} — parando CAT2")
                return
        d = hunter_email_verifier(email)
        _STATS["hunter_usado_aprox"] += 1
        if not d:
            continue
        status = (d.get("status") or d.get("result") or "unknown").lower()
        score = int(d.get("score") or 0)
        is_valid = status in ("valid", "accept_all") and score >= 60
        if status == "webmail":
            _STATS["cat2_webmail"] += 1
        elif is_valid:
            _STATS["cat2_validos"] += 1
        else:
            _STATS["cat2_invalidos"] += 1
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE obras SET
                        nivel1_email_smtp_verified=%s,
                        nivel1_email_status=%s,
                        nivel1_email_score=%s,
                        nivel1_email_verified_at=NOW(),
                        nivel1_origem_enrichment=COALESCE(nivel1_origem_enrichment,'V2_verifier')
                    WHERE id=%s
                """, (is_valid, status, score, obra_id))
            conn.commit()
            if is_valid:
                _STATS["novos"] += 1
        except Exception as e:
            log.warning(f"  update {obra_id}: {e}")
            _STATS["erros"] += 1
            conn.rollback()
        time.sleep(SLEEP_S)


def main() -> int:
    s_search = hunter_saldo("searches")
    s_verify = hunter_saldo("verifications")
    log.info(f"Hunter inicial: searches={s_search} verifications={s_verify}")
    if s_search < HUNTER_FLOOR and s_verify < HUNTER_FLOOR:
        log.error(f"Ambos pools abaixo floor {HUNTER_FLOOR}")
        return 2

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        if s_search >= HUNTER_FLOOR:
            processar_cat3(conn)
        else:
            log.warning(f"searches={s_search} < floor — skipping CAT3")
        s_verify_now = hunter_saldo("verifications")
        log.info(f"Pos-CAT3 Hunter verifications: {s_verify_now}")
        if s_verify_now >= HUNTER_FLOOR:
            processar_cat2(conn)
        else:
            log.warning(f"verifications={s_verify_now} < floor — skipping CAT2")
    finally:
        conn.close()

    s_search_fim = hunter_saldo("searches")
    s_verify_fim = hunter_saldo("verifications")
    log.info(f"FIM Hunter: searches={s_search_fim} verifications={s_verify_fim} "
             f"(delta searches={s_search - s_search_fim} verify={s_verify - s_verify_fim})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except SystemExit:
        raise
    except BaseException as e:
        import traceback as tb
        log.error(f"V2 UNCAUGHT {type(e).__name__}: {e}")
        log.error(tb.format_exc())
        sys.exit(1)
