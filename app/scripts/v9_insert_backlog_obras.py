#!/usr/bin/env python3
"""T3 V9 — insert 7 obras from noticias_backlog_manual (chat validated)."""
import os
import psycopg2

# (empresa, cnpj_or_None_for_lookup, lookup_pattern, dominio, capex_brl, uf, descricao, backlog_fonte_nome)
OBRAS = [
    ("Schomäcker Federnwerk", None, "SCHOMAECKER", "schomaecker.de",          205_000_000, "SC", "Primeira fábrica Schomäcker no Brasil — molas para veículos comerciais; Perini Business Park, Joinville SC", "Correio24h"),
    ("Cooperativa Agraria Agroindustrial", "77890846000155", None, "agraria.com.br", 49_800_000, "PR", "Ampliação maltaria Agrária em Guarapuava PR; BNDES R$49,8mi; aumento de 25% da capacidade", "BNDES Agraria"),
    ("Galvanotek Embalagens Ltda", "94319589000139", None, "galvanotek.com.br", 70_000_000, "RS", "Nova fábrica de embalagens de papel Galvanotek em Barão RS; 26.000m²", "Galvanotek"),
    ("Atvos S/A", "08811643000129", None, "atvos.com",                            1_000_000_000, "MS", "Primeira usina de etanol de milho Atvos; 642 mil ton/ano; Mato Grosso do Sul", "Atvos"),
    ("Mahindra Brasil", None, "MAHINDRA", "mahindrabrasil.com.br",                  100_000_000, "RS", "Nova fábrica de tratores Mahindra em Dois Irmãos RS (BR-116); triplicar produção 3k→9k/ano", "Mahindra"),
    ("New Wave", None, "NEW WAVE", "newwave.com.br",                                250_000_000, "PA", "Planta de ferro verde a partir de resíduo de bauxita em Barcarena PA; parceria Hydro Alunorte; BNDES R$221mi", "New Wave"),
    ("Multilog Operador Logistico", None, "MULTILOG", "multilog.com.br",            900_000_000, "SC", "Expansão logística Multilog R$900mi 2026-2028; porto seco Foz do Iguaçu + expansão nacional", "Multilog"),
]

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "db"), port=5432,
    user=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASSWORD"),
    dbname=os.environ.get("DB_NAME", "wins_hub"),
)
cur = conn.cursor()

inserted = 0
skipped_existing = 0
lookups_failed = []
for empresa, cnpj_fixed, lookup, dominio, capex, uf, desc, backlog_nome in OBRAS:
    cnpj = cnpj_fixed
    if cnpj is None and lookup:
        cur.execute("""
            SELECT cnpj FROM fornecedores
            WHERE razao_social ILIKE %s AND situacao='ATIVA'
            ORDER BY LENGTH(razao_social) ASC LIMIT 1
        """, (f"%{lookup}%",))
        row = cur.fetchone()
        cnpj = row[0] if row else None
        if cnpj is None:
            lookups_failed.append(empresa)
            print(f"LOOKUP FAIL: {empresa} (pattern {lookup}) — inserting without CNPJ", flush=True)

    cur.execute("""
        SELECT id FROM obras
        WHERE empresa = %s AND fonte = %s
        LIMIT 1
    """, (empresa, "noticias_backlog_v9"))
    if cur.fetchone():
        skipped_existing += 1
        print(f"SKIP EXISTS: {empresa}", flush=True)
        continue

    classificacao = "OURO" if capex >= 500_000_000 else "PRATA"
    nome_obra = desc.split(";")[0][:200]

    cur.execute("""
        INSERT INTO obras (
            nome, empresa, cnpj, valor_estimado, uf,
            fonte, fonte_tipo, descricao, classificacao_computed,
            status, data_anuncio
        ) VALUES (%s,%s,%s,%s,%s,%s,'NOTICIA',%s,%s,'anunciado',CURRENT_DATE)
        RETURNING id
    """, (nome_obra, empresa, cnpj, capex, uf, "noticias_backlog_v9", desc, classificacao))
    oid = cur.fetchone()[0]
    inserted += 1
    print(f"INSERT {classificacao}: {empresa} id={oid} cnpj={cnpj} R${capex/1e6:.1f}mi", flush=True)

    if cnpj and dominio:
        cur.execute("""
            INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, validacao_metodo, validacao_data)
            VALUES (%s,%s,%s,'manual_chat_v9_backlog',NOW())
            ON CONFLICT (cnpj) DO NOTHING
        """, (cnpj, empresa, dominio))

    cur.execute("""
        UPDATE noticias_backlog_manual
        SET status='processado', processado_em=NOW()
        WHERE fonte_nome=%s
    """, (backlog_nome,))

conn.commit()
print(f"\n=== T3 RESULT === inserted={inserted} skipped_existing={skipped_existing} lookup_failed={len(lookups_failed)}")
if lookups_failed:
    print("Lookup failed:", lookups_failed)
cur.close()
conn.close()
