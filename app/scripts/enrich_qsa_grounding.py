#!/usr/bin/env python3
"""
enrich_qsa_grounding.py — Busca contatos do QSA no BrasilAPI e resolve seus perfis LinkedIn via Gemini Search Grounding.
"""
import argparse
import json
import os
import re
import sys
import time
import httpx
import psycopg2

sys.path.insert(0, "/app")
from services.brasilapi import consultar_cnpj

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL = os.getenv("EX_GEMINI_MODEL", "gemini-2.5-flash")
DB = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "")
}

def get_linkedin_grounding(nome, cargo, empresa):
    q = (f'Qual a URL do perfil LinkedIn de {nome}, {cargo} da empresa/organização "{empresa}" no Brasil? '
         'Responda SOMENTE um objeto JSON: {"linkedin_url":"url ou null","cargo_confirmado":"cargo ou null",'
         '"mesma_pessoa":true/false}. Só retorne a URL se tiver certeza de que é o LinkedIn da mesma pessoa. '
         'NÃO invente.')
    try:
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_KEY}",
            json={
                "contents": [{"parts": [{"text": q}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 800,
                    "thinkingConfig": {"thinkingBudget": 0}
                }
            },
            timeout=50
        )
        if r.status_code != 200:
            print(f"   Gemini status: {r.status_code} - {r.text}", file=sys.stderr)
            return None
        txt = "".join(p.get("text", "") for p in r.json()["candidates"][0].get("content", {}).get("parts", []))
        txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        start = txt.find('{')
        end = txt.rfind('}')
        if start >= 0 and end >= 0:
            txt = txt[start:end+1]
        return json.loads(txt)
    except Exception as e:
        print(f"   grounding erro: {str(e)[:80]}", file=sys.stderr)
        return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()

    if not GEMINI_KEY:
        print("ERRO: GEMINI_API_KEY não configurada.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()

    cur.execute("""
        SELECT o.id, o.cnpj, o.empresa, o.nome FROM obras o
        WHERE o.visivel
          AND o.cnpj IS NOT NULL AND length(regexp_replace(o.cnpj, '\D', '', 'g')) = 14
          AND NOT EXISTS (SELECT 1 FROM decisores_obra d WHERE d.obra_id = o.id AND d.excluido_em IS NULL)
          AND o.classificacao_computed IS NULL
        ORDER BY o.valor_estimado DESC NULLS LAST
        LIMIT %s
    """, (a.limit,))
    rows = cur.fetchall()

    print(f"[{len(rows)} obras sem decisor qualificadas. Commit={a.commit}]\n")

    ok = skip = err = 0
    for oid, cnpj, empresa, nome in rows:
        print(f"Obra: '{nome[:40]}…' | Empresa: '{empresa}' | CNPJ: {cnpj}")
        cnpj_clean = re.sub(r'\D', '', cnpj)
        try:
            data = consultar_cnpj(cnpj_clean)
        except Exception as e:
            print(f"   -> Erro BrasilAPI: {e}")
            continue

        if not data or not data.get("qsa"):
            print("   -> QSA vazio/não encontrado.")
            continue

        qsa = data["qsa"]
        socios_processados = 0
        for socio in qsa:
            nome_socio = socio.get("nome_socio", "").strip()
            cargo_socio = socio.get("qualificacao_socio", "").strip()
            identificador = socio.get("identificador_de_socio")

            # Filtra apenas pessoas físicas (identificador 2)
            if identificador == 1 or len(re.sub(r'\D', '', socio.get("cnpj_cpf_do_socio", ""))) == 14:
                continue

            # Filtra cargos relevantes
            if not any(k in cargo_socio.lower() for k in ["administrador", "diretor", "socio", "presidente", "gerente", "procurador"]):
                continue

            print(f"   Sócio QSA: '{nome_socio}' ({cargo_socio})")
            socios_processados += 1

            # Busca LinkedIn via Gemini Grounding
            res = get_linkedin_grounding(nome_socio, cargo_socio, empresa)
            if not res:
                print("      -> Falha na resposta da API.")
                err += 1
                continue

            mesma = res.get("mesma_pessoa") is True
            url = (res.get("linkedin_url") or "").strip()
            cargo_real = (res.get("cargo_confirmado") or "").strip()
            tem_link = mesma and "linkedin.com/in/" in url

            if tem_link:
                print(f"      -> OK! LinkedIn: {url} | Cargo Confirmado: {cargo_real}")
                ok += 1
                if a.commit:
                    cur.execute("""
                        INSERT INTO decisores_obra (
                            obra_id, nome, cargo, linkedin_url, confianca_match,
                            registrado_por, criado_em, observacoes
                        ) VALUES (%s, %s, %s, %s, 85, 'gemini_qsa_grounding', now(), %s)
                    """, (oid, nome_socio, cargo_real or cargo_socio, url, f"QSA BrasilAPI: {cargo_socio}"))
                    # Recompute tier
                    cur.execute("SELECT recompute_classificacao_obra(%s)", (oid,))
                    conn.commit()
                    print("         [Salvo no banco de dados]")
            else:
                print(f"      -> SKIP (Mesma pessoa: {mesma} | URL: {url})")
                skip += 1

            time.sleep(15.0)  # rate limit safety for Gemini free tier

    conn.close()
    print(f"\n=== FIM: ok={ok} skip={skip} err={err} ===")

if __name__ == "__main__":
    main()
