#!/usr/bin/env python3
"""
enrich_prata_emails.py — Enriquecimento grátis de e-mails de decisores de obras PRATA
via adivinhação de padrão + verificação SMTP direta.
"""
import argparse
import os
import re
import sys
import time
import random
import smtplib
import socket
import unicodedata
import dns.resolver
import psycopg2
import psycopg2.extras
from unidecode import unidecode

sys.path.insert(0, "/app")
from services.brasilapi import DB_CONFIG

HELO = "mail.winshub.com.br"
MAILFROM = "verificacao@winshub.com.br"
socket.setdefaulttimeout(8)

_mx_cache = {}
_catchall_cache = {}
_probe_cache = {}

def get_mx(dom):
    if dom in _mx_cache:
        return _mx_cache[dom]
    try:
        ans = dns.resolver.resolve(dom, "MX", lifetime=5)
        hosts = [str(r.exchange).rstrip(".") for r in sorted(ans, key=lambda r: r.preference)]
    except Exception:
        try:
            dns.resolver.resolve(dom, "A", lifetime=3)
            hosts = [dom]
        except Exception:
            hosts = []
    _mx_cache[dom] = hosts
    return hosts

def probe_patterns(dom, first, last):
    key = (dom, first, last)
    if key in _probe_cache:
        return _probe_cache[key]
        
    mx = get_mx(dom)
    if not mx:
        _probe_cache[key] = None
        return None
        
    patterns = [
        f"{first}.{last}@{dom}",
        f"{first}{last}@{dom}",
        f"{first[0]}{last}@{dom}",
        f"{first}@{dom}"
    ]
    
    try:
        srv = smtplib.SMTP(timeout=8)
        srv.connect(mx[0], 25)
        srv.helo(HELO)
        srv.mail(MAILFROM)
        
        if dom not in _catchall_cache:
            rnd = "nx-%d-probe" % random.randint(10000, 99999)
            try:
                code, _ = srv.rcpt(f"{rnd}@{dom}")
                _catchall_cache[dom] = code in (250, 251)
            except Exception:
                _catchall_cache[dom] = False
                
        if _catchall_cache[dom]:
            srv.quit()
            _probe_cache[key] = None
            return None
            
        valid_email = None
        for email in patterns:
            try:
                code, _ = srv.rcpt(email)
                if code in (250, 251):
                    valid_email = email
                    break
            except Exception:
                continue
                
        try:
            srv.quit()
        except Exception:
            pass
            
        _probe_cache[key] = valid_email
        return valid_email
    except Exception:
        _probe_cache[key] = None
        return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    cur.execute("""
        SELECT o.id AS obra_id, o.empresa, o.nome AS obra_nome,
               d.id AS decisor_id, d.nome AS decisor_nome, d.cargo,
               (SELECT ed.dominio FROM empresa_dominios ed 
                WHERE ed.cnpj = o.cnpj OR LEFT(ed.cnpj, 8) = LEFT(o.cnpj, 8) LIMIT 1) AS dominio
        FROM obras o
        JOIN decisores_obra d ON o.id = d.obra_id
        WHERE o.visivel AND o.classificacao_computed = 'PRATA'
          AND d.excluido_em IS NULL
          AND (d.email IS NULL OR d.email = '')
          AND o.cnpj IS NOT NULL
        ORDER BY o.valor_estimado DESC NULLS LAST
        LIMIT %s
    """, (a.limit,))
    rows = cur.fetchall()
    
    print(f"Qualificadas para enriquecimento SMTP: {len(rows)} (Commit={a.commit})\n")
    
    success = 0
    for r in rows:
        dom = r["dominio"]
        if not dom:
            continue
            
        nome_completo = unidecode(r["decisor_nome"].strip().lower())
        parts = [p for p in re.split(r"\s+", nome_completo) if p.isalpha() and len(p) >= 2]
        if len(parts) < 2:
            continue
            
        first, last = parts[0], parts[-1]
        
        # Check cache before printing log to keep output clean for duplicates
        key = (dom, first, last)
        is_cached = key in _probe_cache
        
        if is_cached:
            valid_email = _probe_cache[key]
        else:
            print(f"Testando: '{r['decisor_nome']}' ({r['empresa']}) -> domínio: {dom}", flush=True)
            valid_email = probe_patterns(dom, first, last)
            
        if valid_email:
            if not is_cached:
                print(f"  ✓ ENCONTRADO E-MAIL VÁLIDO: {valid_email}", flush=True)
            success += 1
            if a.commit:
                cur.execute("""
                    UPDATE decisores_obra 
                    SET email = %s, email_status = 'verificado_smtp_gratis',
                        observacoes = COALESCE(observacoes || ' | ', '') || 'E-mail validado via probe SMTP direto'
                    WHERE id = %s
                """, (valid_email, r["decisor_id"]))
                cur.execute("SELECT recompute_classificacao_obra(%s)", (r["obra_id"],))
                conn.commit()
        else:
            if not is_cached:
                print("  ✗ Nenhum e-mail válido ou catch-all detectado.", flush=True)
            
    conn.close()
    print(f"\n=== FIM: {success} e-mails válidos encontrados ===")

if __name__ == "__main__":
    main()
