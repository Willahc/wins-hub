#!/usr/bin/env python3
"""Captador de notícias industriais via Serper /news → noticias_backlog_manual.

V2 (16/05/2026): RSS Google Alerts substituído por Serper.dev /news API. As mesmas
queries que estavam configuradas como Google Alerts são rodadas via Serper com
`tbs=qdr:w` (últimos 7 dias), `gl=br`, `hl=pt`, 10 results/query.

Pipeline:
  1. Para cada query em QUERIES: POST https://google.serper.dev/news
  2. Para cada item retornado: hash MD5(link) → dedup vs noticias_backlog_manual.fonte_nome
     (prefixo 'google_alerts:' mantido pra coabitar com qualquer dedup pré-existente)
  3. Haiku claude-haiku-4-5-20251001 extrai JSON {empresa, cnpj_hint, descricao,
     valor_estimado, uf, municipio, setor} OU retorna null se notícia for irrelevante
     (não-industrial / capex < R$50mi / opinião / repost).
  4. INSERT em noticias_backlog_manual com status='pending_url' (fila human review).

Schema noticias_backlog_manual:
  - fonte_nome TEXT NOT NULL UNIQUE  (dedup key: 'google_alerts:<md5_link_16>')
  - url, titulo, descricao TEXT
  - status TEXT DEFAULT 'pending_url'
  - criado_em, processado_em TIMESTAMPTZ

Uso:
    python /app/scripts/captar_google_alerts.py            # roda todas as queries
    python /app/scripts/captar_google_alerts.py --dry      # não persiste

Env requerida:
    SERPER_API_KEY        chave da Serper.dev
    ANTHROPIC_API_KEY     chave Anthropic
    DB_HOST/PORT/...      conexão postgres (defaults: db:5432 wins_hub postgres)
"""
import argparse
import sys as _scompat
if "/app" not in _scompat.path: _scompat.path.insert(0, "/app")
from services.llm_haiku_compat import _haiku_client, _haiku_async_client  # free-first 25/06
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic
import psycopg2
import requests

LOG_DIR = Path("/app/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"captar_google_alerts_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("captar_google_alerts")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

HAIKU_MODEL = "claude-haiku-4-5-20251001"
CAPEX_MIN = 50_000_000  # R$50mi
SERPER_URL = "https://google.serper.dev/news"
SERPER_TIMEOUT = 20
RESULTS_PER_QUERY = 10

# 15 queries — espelham os Google Alerts originais (greenfield/expansões/financiamento/EPC)
# + 5 setoriais (automotivo/data center/H2/semicondutor/agências invest) — cobertura de setores cegos
QUERIES = [
    '"ampliação" fábrica bilhões Brasil',
    '"anuncia investimento" fábrica Brasil',
    '"expande produção" Brasil investimento',
    '"greenfield" investimento indústria Brasil',
    '"nova fábrica" "investimento" Brasil',
    '"nova planta" "investimento" milhões Brasil',
    '"nova unidade industrial" Brasil',
    '("BNDES" OR "SUDENE") AND ("aprova financiamento" OR "linha de crédito")',
    '("Licença Prévia" OR "EIA/RIMA") AND ("complexo industrial" OR "nova planta")',
    '("Promon Engenharia" OR "AFRY") AND ("vence contrato" OR "novo projeto")',
    '("nova fábrica" OR "nova planta") pneu OR automotivo OR autopeças Brasil',
    '(montadora OR "data center" OR "centro de dados") investimento bilhões Brasil',
    '("hidrogênio verde" OR "energia limpa") nova planta Brasil milhões',
    '(semicondutor OR bateria OR "veículo elétrico") fábrica Brasil investimento',
    '("Invest Paraná" OR "InvestSP" OR "Invest Minas" OR "Apex") investimento fábrica',
]

HAIKU_PROMPT = """Você é um extrator de dados de obras industriais brasileiras.

Analise esta notícia e extraia APENAS se for sobre investimento industrial real no Brasil (fábrica, planta, refinaria, porto, ferrovia, energia, mineração, logística, data center) com capex >= R$ 50 milhões.

REJEITAR (retornar null):
- Opinião/análise/coluna sem investimento concreto
- Inauguração de produto, lançamento de campanha
- Capex < R$ 50 milhões (ou não mensurável)
- Notícia internacional sem operação no Brasil
- Resultado financeiro, balanço, M&A puro (sem obra física)

Retornar APENAS JSON válido (sem ```json fences) ou a string null:

{
  "empresa": "Razão social ou marca",
  "cnpj_hint": "CNPJ se mencionado no texto (string só dígitos) ou null",
  "descricao": "Resumo em 1 frase (max 200 chars)",
  "valor_estimado": 200000000,
  "uf": "MT",
  "municipio": "Várzea Grande",
  "setor": "Alimentos e Bebidas",
  "capex_suspeito": false
}

Setores válidos: Alimentos e Bebidas, Automotivo e Autopeças, Energia, Infraestrutura, Logística, Mineração, Petróleo e Gás, Química, Siderurgia e Metalurgia, Papel e Celulose, Tecnologia, Outros.

Regras:
- capex_suspeito=true se valor parecer investimento GLOBAL, PROGRAMA plurianual, ou cobrir MÚLTIPLAS plantas/países (ex: R$11bi Toyota Brasil 2030, R$37bi Petrobras SP 2026-30). Extrair só capex da OBRA ESPECÍFICA; se vier valor inflado/agregado, valor_estimado=null + capex_suspeito=true.
- Setor: usar exatamente a grafia da lista acima (case-sensitive).

Notícia:
"""

# Port industrial_priv 31052026 — normalização setor canonical
_SETOR_MAP_PROMPT_TO_DB = {
    "alimentos e bebidas": "ALIMENTOS_E_BEBIDAS",
    "automotivo e autopecas": "AUTOMOTIVO_E_AUTOPECAS",
    "automotivo e autopeças": "AUTOMOTIVO_E_AUTOPECAS",
    "automotivo": "AUTOMOTIVO_E_AUTOPECAS",
    "energia": "ENERGIA",
    "infraestrutura": "INFRAESTRUTURA",
    "logistica": "LOGISTICO",
    "logística": "LOGISTICO",
    "mineracao": "MINERACAO",
    "mineração": "MINERACAO",
    "petroleo e gas": "PETROLEO_GAS",
    "petróleo e gás": "PETROLEO_GAS",
    "quimica": "QUIMICA",
    "química": "QUIMICA",
    "papel e celulose": "PAPEL_E_CELULOSE",
    "tecnologia": "TECNOLOGIA",
    "siderurgia e metalurgia": "SIDERURGIA_METALURGIA",
    "outros": None,
    "outro": None,
}


def normalizar_setor(raw):
    if not raw: return None
    s = raw.strip().lower()
    return _SETOR_MAP_PROMPT_TO_DB.get(s)


def hash_link(link: str) -> str:
    return hashlib.md5(link.encode("utf-8", "ignore")).hexdigest()[:16]


def chamar_serper(query: str) -> list[dict]:
    headers = {
        "X-API-KEY": os.environ["SERPER_API_KEY"],
        "Content-Type": "application/json",
    }
    body = {
        "q": query,
        "gl": "br",
        "hl": "pt",
        "num": RESULTS_PER_QUERY,
        "tbs": "qdr:w",  # últimos 7 dias
    }
    r = requests.post(SERPER_URL, headers=headers, json=body, timeout=SERPER_TIMEOUT)
    r.raise_for_status()
    return r.json().get("news", []) or []


def chamar_haiku(client, titulo: str, conteudo: str) -> dict | None:
    payload = HAIKU_PROMPT + f"\nTítulo: {titulo}\nConteúdo: {conteudo[:1500]}"
    resp = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": payload}],
    )
    texto = resp.content[0].text.strip()
    # Haiku às vezes envolve em ```json fences mesmo com instrução explícita
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*", "", texto)
        texto = re.sub(r"\s*```$", "", texto)
        texto = texto.strip()
    if texto.lower() == "null" or not texto.startswith("{"):
        return None
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        log.warning("Haiku retornou JSON inválido: %s", texto[:200])
        return None


def _is_semantically_duplicate(dados: dict, conn) -> bool:
    """Checa se já existe entrada com mesmo cnpj_hint+capex+uf nos últimos 30d.

    Fallback (sem cnpj_hint): empresa_nome+capex+30d.
    Evita inserir mesma notícia republicada em portais distintos (URLs distintas).
    """
    cnpj = (dados.get("cnpj_hint") or "")
    cnpj = "".join(c for c in cnpj if c.isdigit())
    capex = dados.get("valor_estimado") or 0
    try:
        capex = int(capex)
    except (TypeError, ValueError):
        capex = 0
    uf = (dados.get("uf") or "").strip()[:2].upper()
    empresa = (dados.get("empresa") or "").strip()
    if capex <= 0:
        return False  # sem capex, dedup só por link

    with conn.cursor() as cur:
        if cnpj and len(cnpj) == 14:
            cur.execute(
                """
                SELECT 1
                FROM noticias_backlog_manual
                WHERE descricao::jsonb->>'cnpj_hint' = %s
                  AND (descricao::jsonb->>'valor_estimado')::bigint = %s
                  AND COALESCE(descricao::jsonb->>'uf','') = %s
                  AND criado_em >= NOW() - INTERVAL '30 days'
                LIMIT 1
                """,
                (cnpj, capex, uf),
            )
            if cur.fetchone():
                return True
        if empresa:
            cur.execute(
                """
                SELECT 1
                FROM noticias_backlog_manual
                WHERE LOWER(descricao::jsonb->>'empresa') = LOWER(%s)
                  AND (descricao::jsonb->>'valor_estimado')::bigint = %s
                  AND criado_em >= NOW() - INTERVAL '30 days'
                LIMIT 1
                """,
                (empresa, capex),
            )
            if cur.fetchone():
                return True
    return False


def processar_query(query: str, conn, client, dry: bool) -> tuple[int, int, int, int]:
    log.info("Query: %s", query)
    try:
        items = chamar_serper(query)
    except requests.HTTPError as e:
        log.error("  Serper HTTP %s: %s", e.response.status_code, e.response.text[:200])
        return 0, 0, 0, 0
    except Exception as e:
        log.exception("  Serper falhou: %s", e)
        return 0, 0, 0, 0
    log.info("  %d resultados Serper", len(items))

    encontrados = len(items)
    novos = pulados = rejeitados = 0
    cur = conn.cursor()
    for item in items:
        titulo = (item.get("title") or "").strip()
        link = (item.get("link") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        if not link or not titulo:
            continue

        fonte_nome = f"google_alerts:{hash_link(link)}"
        cur.execute("SELECT 1 FROM noticias_backlog_manual WHERE fonte_nome=%s", (fonte_nome,))
        if cur.fetchone():
            pulados += 1
            continue

        dados = chamar_haiku(client, titulo, snippet or titulo)
        if not dados:
            rejeitados += 1
            log.info("  REJEITADO Haiku: %s", titulo[:80])
            continue

        # Port industrial_priv 31052026: capex_suspeito (programa/global) → capex=0+flag
        if dados.get("capex_suspeito"):
            dados["valor_estimado"] = None
            dados["_flag_capex_suspeito"] = True

        # Port industrial_priv 31052026: normalizar setor pra DB canônico já na fila
        _setor_raw = dados.get("setor")
        _setor_db = normalizar_setor(_setor_raw)
        if _setor_db is None:
            rejeitados += 1
            log.info("  REJEITADO setor invalido/sem cobertura SCC (%r): %s", _setor_raw, titulo[:80])
            continue
        dados["setor_raw"] = _setor_raw
        dados["setor"] = _setor_db

        capex = dados.get("valor_estimado") or 0
        try:
            capex = int(capex)
        except (TypeError, ValueError):
            capex = 0
        if capex < CAPEX_MIN and not dados.get("_flag_capex_suspeito"):
            rejeitados += 1
            log.info("  REJEITADO capex<R$50mi (%s): %s", capex, titulo[:80])
            continue

        # Dedup semântico: mesma empresa+capex+UF nos últimos 30d (notícia republicada)
        if _is_semantically_duplicate(dados, conn):
            pulados += 1
            log.info("  DEDUP semântico (já visto em 30d): %s — R$%.0fmi", dados.get("empresa"), capex / 1e6)
            continue

        descricao_struct = json.dumps(dados, ensure_ascii=False)
        if dry:
            novos += 1
            log.info("  [DRY] INSERIR %s — R$%.0fmi", dados.get("empresa"), capex / 1e6)
            continue

        cur.execute(
            """
            INSERT INTO noticias_backlog_manual (fonte_nome, url, titulo, descricao, status)
            VALUES (%s, %s, %s, %s, 'pending_url')
            ON CONFLICT (fonte_nome) DO NOTHING
            RETURNING id
            """,
            (fonte_nome, link, titulo[:500], descricao_struct),
        )
        hit = cur.fetchone()
        if hit:
            novos += 1
            noticia_id = hit[0]
            log.info("  INSERIDO %s — R$%.0fmi (%s) -> id=%d", dados.get("empresa"), capex / 1e6, link, noticia_id)
            # Sonnet analysis inline (best-effort: nao bloqueia captador se falhar)
            try:
                from analisar_noticia_sonnet import analisar_e_persistir
                analisar_e_persistir(noticia_id, cur, client)
            except Exception as e:
                log.warning("  Sonnet analysis falhou (notica fica sem analysis, cron --todas pode reprocessar): %s: %s",
                            type(e).__name__, str(e)[:100])
        conn.commit()
        time.sleep(0.5)

    cur.close()
    return encontrados, novos, pulados, rejeitados


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="Não persiste, só loga")
    args = parser.parse_args()

    if not os.getenv("SERPER_API_KEY"):
        log.error("SERPER_API_KEY não setada")
        sys.exit(2)
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY não setada")
        sys.exit(3)

    client = _haiku_client()
    conn = psycopg2.connect(**DB_CONFIG)
    total_encontrados = total_novos = total_pulados = total_rejeitados = 0
    try:
        for query in QUERIES:
            try:
                e, n, p, r = processar_query(query, conn, client, args.dry)
                total_encontrados += e
                total_novos += n
                total_pulados += p
                total_rejeitados += r
            except Exception as e:
                log.exception("Erro na query %r: %s", query, e)
            time.sleep(0.5)  # respeito ao rate do Serper
    finally:
        conn.close()

    log.info(
        "Concluído. encontrados=%d novos=%d pulados=%d rejeitados=%d",
        total_encontrados, total_novos, total_pulados, total_rejeitados,
    )
    print(f"STATS_JSON: {json.dumps({'buscados': total_encontrados, 'novos': total_novos, 'pulados': total_pulados, 'rejeitados': total_rejeitados})}")


if __name__ == "__main__":
    main()
