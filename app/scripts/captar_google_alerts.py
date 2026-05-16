#!/usr/bin/env python3
"""Captador Google Alerts → noticias_backlog_manual.

Pipeline:
  1. Lê URLs de RSS feeds do Google Alerts (env GOOGLE_ALERTS_FEEDS, separadas por vírgula,
     ou config padrão FEEDS abaixo).
  2. Para cada entry: hash MD5(link) → dedup vs noticias_backlog_manual.fonte_nome.
  3. Haiku claude-haiku-4-5-20251001 extrai JSON {empresa, cnpj_hint, descricao, valor_estimado, uf, municipio, setor}
     ou retorna null se notícia for irrelevante (não-industrial / capex < R$50mi / opinião / repost).
  4. INSERT em noticias_backlog_manual com status='pending_url' (fila pro human review).

Schema noticias_backlog_manual:
  - fonte_nome TEXT NOT NULL UNIQUE  (usamos como dedup key: 'google_alerts:<md5_link_16>')
  - url, titulo, descricao TEXT
  - status TEXT DEFAULT 'pending_url'
  - criado_em, processado_em TIMESTAMPTZ

Uso:
    python /app/scripts/captar_google_alerts.py            # roda todos os feeds
    python /app/scripts/captar_google_alerts.py --dry      # não persiste

Setup:
    Preencher FEEDS abaixo OU exportar GOOGLE_ALERTS_FEEDS="url1,url2,url3".
    URLs RSS são obtidas em google.com/alerts → ícone RSS de cada alerta.
"""
import argparse
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
import feedparser
import psycopg2
from psycopg2.extras import RealDictCursor

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

# Feeds RSS Google Alerts ativos (user 363dfb07715ec092 — williamvnvn@gmail.com).
# Cada URL tem formato: https://www.google.com/alerts/feeds/<USER_ID>/<ALERT_ID>
FEEDS_DEFAULT = [
    "https://www.google.com/alerts/feeds/363dfb07715ec092/9c38c892bba6b5ed",
    "https://www.google.com/alerts/feeds/363dfb07715ec092/05afdca59959484b",
    "https://www.google.com/alerts/feeds/363dfb07715ec092/c14f1237704d942c",
    "https://www.google.com/alerts/feeds/363dfb07715ec092/632fc563266c5d5a",
    "https://www.google.com/alerts/feeds/363dfb07715ec092/9ed122ecf3b1fbbc",
    "https://www.google.com/alerts/feeds/363dfb07715ec092/3343a306de573a01",
    "https://www.google.com/alerts/feeds/363dfb07715ec092/36b5faae7e25dab6",
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
  "setor": "Alimentos e Bebidas"
}

Setores válidos: Alimentos e Bebidas, Automotivo e Autopeças, Energia, Infraestrutura, Logística, Mineração, Petróleo e Gás, Química, Siderurgia e Metalurgia, Papel e Celulose, Tecnologia, Outros.

Notícia:
"""


def get_feeds():
    env = os.getenv("GOOGLE_ALERTS_FEEDS", "").strip()
    if env:
        return [u.strip() for u in env.split(",") if u.strip()]
    return list(FEEDS_DEFAULT)


def hash_link(link: str) -> str:
    return hashlib.md5(link.encode("utf-8", "ignore")).hexdigest()[:16]


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


def processar_feed(url: str, conn, client, dry: bool) -> tuple[int, int, int]:
    log.info("Feed: %s", url)
    feed = feedparser.parse(url)
    if feed.bozo:
        log.warning("Feed bozo (parse error suave): %s", getattr(feed, "bozo_exception", ""))
    entries = feed.entries or []
    log.info("  %d entries", len(entries))

    novos = pulados = rejeitados = 0
    cur = conn.cursor()
    for entry in entries:
        titulo = entry.get("title", "") or ""
        # Google Alerts envolve título em HTML; strip tags simples
        titulo = re.sub(r"<[^>]+>", "", titulo).strip()
        link = entry.get("link", "") or ""
        conteudo = entry.get("summary", "") or titulo
        conteudo = re.sub(r"<[^>]+>", " ", conteudo).strip()

        if not link:
            continue

        fonte_nome = f"google_alerts:{hash_link(link)}"
        cur.execute("SELECT 1 FROM noticias_backlog_manual WHERE fonte_nome=%s", (fonte_nome,))
        if cur.fetchone():
            pulados += 1
            continue

        dados = chamar_haiku(client, titulo, conteudo)
        if not dados:
            rejeitados += 1
            log.info("  REJEITADO Haiku: %s", titulo[:80])
            continue

        capex = dados.get("valor_estimado") or 0
        try:
            capex = int(capex)
        except (TypeError, ValueError):
            capex = 0
        if capex < CAPEX_MIN:
            rejeitados += 1
            log.info("  REJEITADO capex<R$50mi (%s): %s", capex, titulo[:80])
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
            """,
            (fonte_nome, link, titulo[:500], descricao_struct),
        )
        if cur.rowcount > 0:
            novos += 1
            log.info("  INSERIDO %s — R$%.0fmi (%s)", dados.get("empresa"), capex / 1e6, link)
        conn.commit()
        time.sleep(0.5)

    cur.close()
    return novos, pulados, rejeitados


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="Não persiste, só loga")
    args = parser.parse_args()

    feeds = get_feeds()
    if not feeds:
        log.error("Nenhum feed configurado. Preencha FEEDS_DEFAULT no script OU exporte GOOGLE_ALERTS_FEEDS=url1,url2")
        log.error("URLs RSS são obtidas em google.com/alerts → ícone ⋮ de cada alerta → 'Feed RSS'")
        sys.exit(2)

    if not os.getenv("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY não setada")
        sys.exit(3)

    client = anthropic.Anthropic()
    conn = psycopg2.connect(**DB_CONFIG)
    total_novos = total_pulados = total_rejeitados = 0
    try:
        for feed_url in feeds:
            try:
                n, p, r = processar_feed(feed_url, conn, client, args.dry)
                total_novos += n
                total_pulados += p
                total_rejeitados += r
            except Exception as e:
                log.exception("Erro no feed %s: %s", feed_url, e)
    finally:
        conn.close()

    log.info(
        "Concluído. novos=%d pulados=%d rejeitados=%d",
        total_novos, total_pulados, total_rejeitados,
    )
    print(f"STATS_JSON: {json.dumps({'novos': total_novos, 'pulados': total_pulados, 'rejeitados': total_rejeitados})}")


if __name__ == "__main__":
    main()
