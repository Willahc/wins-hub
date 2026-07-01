#!/usr/bin/env python3
"""
resolve_domains_deterministic.py — Resolve domínios extraindo do e-mail de fornecedores locais.
"""
import sys, re
sys.path.insert(0, "/app")
import psycopg2, psycopg2.extras
from services.brasilapi import DB_CONFIG

GENERIC_DOMAINS = {
    'gmail.com', 'hotmail.com', 'yahoo.com.br', 'yahoo.com', 'outlook.com',
    'bol.com.br', 'uol.com.br', 'terra.com.br', 'ig.com.br', 'globo.com',
    'icloud.com', 'aol.com', 'protonmail.com', 'live.com', 'outlook.com.br',
    'hotmail.com.br', 'yahoo.ca', 'ymail.com', 'msn.com', 'live.com.br'
}

def clean_cnpj(c):
    return re.sub(r"\D", "", c or "")

def main():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    # 1. Obter todas as obras visíveis que estão sem domínio na tabela empresa_dominios
    cur.execute("""
        SELECT DISTINCT o.cnpj, o.empresa
        FROM obras o
        WHERE o.visivel AND o.cnpj IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM empresa_dominios ed
              WHERE ed.cnpj = o.cnpj OR LEFT(ed.cnpj, 8) = LEFT(o.cnpj, 8)
          )
    """)
    rows = cur.fetchall()
    print(f"Obras qualificadas sem domínio: {len(rows)}\n")
    
    hit = 0
    for r in rows:
        cnpj_clean = clean_cnpj(r["cnpj"])
        if len(cnpj_clean) != 14:
            continue
            
        # 2. Buscar e-mail na tabela fornecedores usando o índice primary key (instantâneo)
        cur.execute("SELECT email, razao_social FROM fornecedores WHERE cnpj = %s LIMIT 1", (cnpj_clean,))
        f_row = cur.fetchone()
        if not f_row or not f_row["email"]:
            continue
            
        email = f_row["email"].strip().lower()
        if '@' not in email:
            continue
            
        domain = email.split('@')[1].strip()
        if domain in GENERIC_DOMAINS or '.' not in domain or len(domain) < 4:
            continue
            
        # 3. Registrar o domínio resolvido no banco
        hit += 1
        empresa_nome = r["empresa"] or f_row["razao_social"] or "Empresa do CNPJ " + cnpj_clean
        print(f"  ✓ [{empresa_nome[:25]}] -> Domain: {domain} (CNPJ: {cnpj_clean})")
        
        cur.execute("""
            INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, confianca, fonte, criado_em, atualizado_em)
            VALUES (%s, %s, %s, 5, 'agente_grounding_email_parse', now(), now())
            ON CONFLICT (cnpj) DO UPDATE SET dominio = EXCLUDED.dominio, atualizado_em = now()
        """, (cnpj_clean, empresa_nome, domain))
        conn.commit()
        
    print(f"\nTotal domínios resolvidos e salvos: {hit} de {len(rows)}")
    conn.close()

if __name__ == "__main__":
    main()
