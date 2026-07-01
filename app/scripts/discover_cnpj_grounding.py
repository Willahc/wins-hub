#!/usr/bin/env python3
"""
discover_cnpj_grounding.py — Descobre CNPJ via Gemini Search Grounding para obras sem CNPJ.
"""
import argparse
import json
import os
import re
import sys
import time
import httpx
import psycopg2

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL = os.getenv("EX_GEMINI_MODEL", "gemini-2.5-flash")
DB = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "")
}

def valida_cnpj(c):
    c = re.sub(r"\D", "", c or "")
    if len(c) != 14 or c == c[0]*14:
        return False
    def dv(b, p):
        s = sum(int(b[i])*p[i] for i in range(len(p)))
        r = s % 11
        return '0' if r < 2 else str(11 - r)
    return dv(c, [5,4,3,2,9,8,7,6,5,4,3,2]) == c[12] and dv(c, [6,5,4,3,2,9,8,7,6,5,4,3,2]) == c[13]

def get_cnpj_grounding(empresa, nome_obra):
    q = (f'Qual é o CNPJ (Cadastro Nacional da Pessoa Jurídica) da empresa ou ente público "{empresa}" no Brasil? '
         f'Contexto do empreendimento/obra: "{nome_obra}". '
         'Responda SOMENTE JSON: {"cnpj":"14 digitos apenas ou null","razao_social":"razao social ou null",'
         '"confianca":0-100,"motivo":"breve"}. Só preencha o cnpj se a confiança for >=75. '
         'Use null no cnpj se não tiver certeza.')
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
        # Find first '{' and last '}'
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
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()

    if not GEMINI_KEY:
        print("ERRO: GEMINI_API_KEY não configurada.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()

    cur.execute("""
        SELECT id, empresa, nome FROM obras
        WHERE motivo_invisivel IS NULL
          AND classificacao_computed IS NULL
          AND empresa IS NOT NULL AND empresa <> ''
          AND (cnpj IS NULL OR cnpj = '' OR NOT cnpj_valido(cnpj))
        ORDER BY criado_em ASC
        LIMIT %s
    """, (a.limit,))
    rows = cur.fetchall()

    print(f"[{len(rows)} obras sem tier/CNPJ identificadas. Commit={a.commit}]\n")

    ok = skip = err = 0
    for oid, empresa, nome in rows:
        print(f"Processando: '{empresa}' (Obra: '{nome[:40]}…')")
        res = get_cnpj_grounding(empresa, nome)
        if not res:
            print("   -> Falha na resposta da API.")
            err += 1
            continue

        cnpj = (res.get("cnpj") or "").strip()
        cnpj = re.sub(r"\D", "", cnpj)
        conf = int(res.get("confianca") or 0)
        motivo = res.get("motivo") or "sem motivo"
        razao = res.get("razao_social") or "não informada"

        if not cnpj or conf < 75:
            print(f"   -> SKIP (Confiança: {conf} | Motivo: {motivo})")
            skip += 1
            continue

        if not valida_cnpj(cnpj):
            print(f"   -> SKIP (CNPJ inválido no dígito verificador: {cnpj})")
            skip += 1
            continue

        print(f"   -> OK! CNPJ: {cnpj} | Razão Social: {razao} | Confiança: {conf}")
        ok += 1

        if a.commit:
            cur.execute("""
                UPDATE obras
                SET cnpj = %s,
                    cnpj_status = 'resolvido_gemini_grounding',
                    observacoes_validacao = COALESCE(observacoes_validacao || ' | ', '') || %s
                WHERE id = %s
            """, (cnpj, f"cnpj_discover_gemini_grounding_20260701: {razao}", oid))
            # Recompute tier
            cur.execute("SELECT recompute_classificacao_obra(%s)", (oid,))
            conn.commit()

        time.sleep(15.0)  # rate limit safety for Gemini free tier

    conn.close()
    print(f"\n=== FIM: ok={ok} skip={skip} err={err} ===")

if __name__ == "__main__":
    main()
