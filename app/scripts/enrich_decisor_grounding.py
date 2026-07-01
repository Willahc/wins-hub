#!/usr/bin/env python3
"""
enrich_decisor_grounding.py — Enriquece LinkedIn (e cargo real) de decisores via
Gemini grounding (busca Google REAL, free tier). Substituto GRÁTIS do web_search
pago, sem ferir zero-ruído: só aplica quando o modelo confirma mesma_pessoa
(mesma empresa/cargo) e a URL é linkedin.com/in/. Cargo real vai p/ observacoes
(não sobrescreve). DRY-RUN por padrão; --commit aplica.

Alvo: decisores ativos de obras visíveis OURO/PRATA sem linkedin_url, por valor.
"""
import argparse
import json
import os
import sys
import time

import httpx
import psycopg2

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL = os.getenv("EX_GEMINI_MODEL", "gemini-2.5-flash")
DB = {"host": os.getenv("DB_HOST", "db"), "port": int(os.getenv("DB_PORT", "5432")),
      "dbname": os.getenv("DB_NAME", "wins_hub"), "user": os.getenv("DB_USER", "postgres"),
      "password": os.getenv("DB_PASSWORD", "")}


def grounding(nome, cargo, empresa):
    q = (f'Qual a URL do perfil LinkedIn de {nome}, {cargo} da {empresa} no Brasil? '
         'Responda SOMENTE JSON: {"linkedin_url":"url ou null","cargo_confirmado":"cargo ou null",'
         '"mesma_pessoa":true/false}. Só retorne URL se tiver CERTEZA que é a mesma pessoa '
         '(mesma empresa/cargo). NÃO invente.')
    try:
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": q}]}], "tools": [{"google_search": {}}],
                  "generationConfig": {"temperature": 0, "maxOutputTokens": 800,
                                       "thinkingConfig": {"thinkingBudget": 0}}}, timeout=50)
        if r.status_code != 200:
            return None
        txt = "".join(p.get("text", "") for p in r.json()["candidates"][0].get("content", {}).get("parts", []))
        txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(txt)
    except Exception as e:
        print(f"   grounding erro: {str(e)[:80]}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--classe", default="OURO")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT d.id, d.nome, d.cargo, coalesce(o.empresa_executora, o.empresa)
        FROM decisores_obra d JOIN obras o ON o.id = d.obra_id
        WHERE d.excluido_em IS NULL AND o.visivel IS NOT FALSE
          AND o.classificacao_computed = %s
          AND (d.linkedin_url IS NULL OR d.linkedin_url = '')
          AND d.nome !~ '@' AND length(d.nome) > 6
          AND coalesce(o.empresa_executora, o.empresa) IS NOT NULL
        ORDER BY o.valor_estimado DESC NULLS LAST
        LIMIT %s
    """, (a.classe, a.limit))
    alvos = cur.fetchall()
    print(f"alvos: {len(alvos)} ({a.classe}, sem linkedin)  commit={a.commit}\n")

    achados = cargos = 0
    for did, nome, cargo, emp in alvos:
        d = grounding(nome, cargo, emp)
        if not d:
            print(f"·  {nome[:30]} ({emp[:25]}): sem resposta")
            continue
        mesma = d.get("mesma_pessoa") is True
        url = (d.get("linkedin_url") or "").strip()
        cargo_real = (d.get("cargo_confirmado") or "").strip()
        tem_link = mesma and "linkedin.com/in/" in url
        notas = []
        if tem_link:
            notas.append("linkedin via gemini_grounding 20260625")
        if mesma and cargo_real:
            notas.append(f"cargo web: {cargo_real}")
            cargos += 1
        if tem_link:
            achados += 1
            print(f"✓  {nome[:28]}: {url}  | cargo web: {cargo_real[:35] or '?'}")
        else:
            print(f"·  {nome[:28]}: s/ linkedin  | cargo web: {cargo_real[:35] or '?'}")
        if a.commit and (tem_link or (mesma and cargo_real)):
            obs = " ; ".join(notas)[:400]
            if tem_link:
                cur.execute("UPDATE decisores_obra SET linkedin_url=%s, "
                            "observacoes=coalesce(observacoes||' | ','')||%s WHERE id=%s", (url, obs, did))
            else:
                cur.execute("UPDATE decisores_obra SET "
                            "observacoes=coalesce(observacoes||' | ','')||%s WHERE id=%s", (obs, did))
        time.sleep(1.5)  # respeitar rate limit free tier

    if a.commit:
        conn.commit()
    print(f"\nlinkedin aplicados: {achados}/{len(alvos)}"
          f"{' (DRY-RUN, nada gravado)' if not a.commit else ''}")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
