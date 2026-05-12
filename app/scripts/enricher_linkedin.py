"""
enricher_linkedin.py — Enriquece LinkedIn URLs de decisores Anderson via Claude API + web_search.

WHAT IT DOES:
- Busca decisores anderson_csv (tipo_cargo classificado != OUTRO, sem linkedin_url) no Postgres.
- Para cada um, faz UMA chamada à Claude API com a tool web_search_20250305 habilitada.
- Modelo procura URL canônica linkedin.com/in/<slug> que case com <nome> @ <empresa>.
- Se encontrar, UPDATE decisores_obra.linkedin_url + INSERT enriquecimento_log
  (fonte='WEBSEARCH_ROUTINE_LINKEDIN').

REQUIRED ENV VARS (em /root/wins_hub/.env):
- ANTHROPIC_API_KEY    — chave da Anthropic API (https://console.anthropic.com)
- DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD — conexão Postgres

OPTIONAL ENV VARS:
- ENRICHER_LIMIT       — quantos decisores processar (default 200)

INSTALL (uma vez, no container):
    docker exec wins_hub-api-1 pip install 'anthropic>=0.40'

RUN:
    docker exec wins_hub-api-1 python /app/scripts/enricher_linkedin.py
    # ou com limite custom:
    docker exec -e ENRICHER_LIMIT=50 wins_hub-api-1 python /app/scripts/enricher_linkedin.py

COST ESTIMATE (200 lookups, modelo claude-sonnet-4-6):
- web_search: $10 / 1.000 searches × ~200 = ~$2.00
- tokens (input+output, com cache se prefix >= 2048 tk): ~$1-2
- Total: ~$3-4 por run de 200.
"""

import json
import os
import re
import sys
import time

import psycopg2
from psycopg2.extras import RealDictCursor

try:
    import anthropic
except ImportError:
    sys.exit(
        "ERRO: anthropic SDK não instalado.\n"
        "Rode: docker exec wins_hub-api-1 pip install 'anthropic>=0.40'"
    )


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    sys.exit("ERRO: ANTHROPIC_API_KEY não configurada no .env")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}
LIMIT = int(os.getenv("ENRICHER_LIMIT", "200"))
MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Você é um especialista em prospecção B2B brasileira. Sua tarefa é encontrar a URL canônica do perfil LinkedIn de um decisor específico em uma empresa brasileira específica.

REGRAS RÍGIDAS:
1. Use a tool web_search para buscar o perfil. Faça queries do tipo:
   "<nome>" "<empresa>" site:linkedin.com/in
   Se a primeira query não retornar nada plausível, tente variações (sem aspas, com cargo, com UF).
2. Aceite APENAS URLs no formato canônico: https://www.linkedin.com/in/<slug>
   (ou linkedin.com/in/<slug>, br.linkedin.com/in/<slug>).
3. REJEITE absolutamente: /pub/, /company/, /school/, /jobs/, /posts/, /pulse/, /feed/, /sales/.
4. O perfil deve plausivelmente corresponder ao nome E à empresa fornecidos. Se houver dúvida razoável (homônimos, empresa diferente, perfil de outro setor), retorne null.
5. Se não encontrar com confiança, retorne null. NÃO INVENTE URLs.
6. Normalize a URL para o formato: https://www.linkedin.com/in/<slug>
   - sem query params, sem trailing slash, https obrigatório, www. obrigatório.

CONTEXTO IMPORTANTE:
- O nome fornecido pode ser parcial (só primeiro nome). Use o cargo + empresa para desambiguar.
- A empresa pode ser razão social longa (ex: "VALE S.A.") — é a mesma empresa que aparece no LinkedIn como "Vale".
- Profissional pode ter saído da empresa recentemente. Aceite se o perfil ainda lista a empresa como atual ou recente (último ano).

FORMATO DE RESPOSTA (sempre, no último bloco de texto, sem markdown, sem comentários):
{"linkedin_url": "https://www.linkedin.com/in/<slug>"}
ou
{"linkedin_url": null}
"""

LINKEDIN_IN_RE = re.compile(
    r"^https://www\.linkedin\.com/in/[A-Za-z0-9\-_%\.]+$"
)
JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)
BLACKLIST = ("/pub/", "/company/", "/school/", "/jobs/", "/posts/", "/pulse/", "/feed/", "/sales/")


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def fetch_lote(limit):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT d.id::text AS decisor_id, d.nome, d.tipo_cargo,
                       d.obra_id::text AS obra_id,
                       o.nome AS obra_nome, o.empresa, o.uf
                FROM decisores_obra d
                JOIN obras o ON o.id = d.obra_id
                WHERE d.excluido_em IS NULL
                  AND d.fonte LIKE 'anderson_csv%%'
                  AND d.tipo_cargo IS NOT NULL
                  AND d.tipo_cargo != 'OUTRO'
                  AND COALESCE(d.linkedin_url, '') = ''
                  AND (o.visivel IS NULL OR o.visivel = true)
                ORDER BY o.lead_score DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def normalize_linkedin(url):
    if not url or not isinstance(url, str):
        return None
    url = url.strip().rstrip("/")
    if "?" in url:
        url = url.split("?", 1)[0]
    if "#" in url:
        url = url.split("#", 1)[0]
    lower = url.lower()
    if any(bad in lower for bad in BLACKLIST):
        return None
    url = re.sub(r"^http://", "https://", url)
    url = re.sub(r"^https://(?:[a-z]{2}\.)?linkedin\.com", "https://www.linkedin.com", url)
    if not url.startswith("https://www.linkedin.com/in/"):
        return None
    if not LINKEDIN_IN_RE.match(url):
        return None
    return url


def parse_json_response(text):
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    for candidate in reversed(JSON_OBJECT_RE.findall(text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def extract_linkedin(client, decisor):
    user_msg = (
        f"Encontre o LinkedIn canônico de:\n"
        f"Nome: {decisor['nome']}\n"
        f"Empresa: {decisor['empresa']}\n"
        f"Cargo conhecido: {decisor['tipo_cargo']}\n"
        f"UF: {decisor.get('uf') or 'BR'}\n"
        f"Obra: {decisor['obra_nome']}"
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text = block.text
    parsed = parse_json_response(text)
    if not isinstance(parsed, dict):
        return None
    return normalize_linkedin(parsed.get("linkedin_url"))


def update_decisor(decisor_id, obra_id, decisor_nome, linkedin_url):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE decisores_obra SET linkedin_url = %s WHERE id = %s",
                (linkedin_url, decisor_id),
            )
            cur.execute(
                """
                INSERT INTO enriquecimento_log
                    (obra_id, decisor_id, decisor_nome, campo, valor_anterior, valor_novo, fonte)
                VALUES (%s, %s, %s, 'linkedin_url', NULL, %s, 'WEBSEARCH_ROUTINE_LINKEDIN')
                """,
                (obra_id, decisor_id, decisor_nome, linkedin_url),
            )
        conn.commit()
    finally:
        conn.close()


def main():
    print(f"[enricher_linkedin] modelo={MODEL} limit={LIMIT}")
    decisores = fetch_lote(LIMIT)
    if not decisores:
        print("Nada a processar — fila vazia.")
        return
    print(f"Lote: {len(decisores)} decisores")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    encontrados = nao_encontrados = erros = 0

    for i, d in enumerate(decisores, 1):
        tag = f"{d['nome']} @ {d['empresa']}"
        try:
            url = extract_linkedin(client, d)
        except anthropic.APIError as e:
            erros += 1
            print(f"[{i}/{len(decisores)}] ERR Claude: {tag} → {e}")
            time.sleep(2.0)
            continue
        except Exception as e:
            erros += 1
            print(f"[{i}/{len(decisores)}] ERR: {tag} → {e!r}")
            continue

        if url:
            try:
                update_decisor(d["decisor_id"], d["obra_id"], d["nome"], url)
                encontrados += 1
                print(f"[{i}/{len(decisores)}] OK  {tag} → {url}")
            except Exception as e:
                erros += 1
                print(f"[{i}/{len(decisores)}] ERR DB: {tag} → {e!r}")
        else:
            nao_encontrados += 1
            print(f"[{i}/{len(decisores)}] —   {tag}")

        time.sleep(0.5)

    print()
    print("=" * 60)
    print(f"Processados: {len(decisores)}")
    print(f"Encontrados: {encontrados}")
    print(f"Não encontrados: {nao_encontrados}")
    print(f"Erros: {erros}")
    print("=" * 60)


if __name__ == "__main__":
    main()
