#!/usr/bin/env python3
"""B2 — Análise de vocabulário: corpus 300-500 textos × Haiku.

1. Carrega CSV de fontes válidas (B1 output).
2. Pra cada fonte: feedparser → primeiros 30 entries → filtro keyword amplo.
3. Envia cada texto pro Haiku → JSON {tipo_obra, palavras_chave_indicadoras, capex_mencionado}.
4. Agrega: top palavras por tipo_obra + precision por keyword + distribuição fonte×tipo.
5. Output /tmp/keywords_otimizadas.yaml.

Custo estimado: 300-400 textos × $0.0017 = ~$0.50-0.70.
"""
import csv
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import feedparser
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("analisar_keywords")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
HAIKU = "claude-haiku-4-5-20251001"
KW_FILTRO = ["investimento", "fábrica", "planta", "obra", "construção",
             "expansão", "anuncia", "R$", "bilh", "milh"]

PROMPT = """Analise se este texto anuncia uma OBRA/INVESTIMENTO INDUSTRIAL REAL no Brasil.

TEXTO: {texto}

Retorne JSON puro (sem markdown):
{{
  "tipo_obra": "fabrica|data_center|logistica|mineracao|energia|agro|infraestrutura|outro|null",
  "palavras_chave_indicadoras": ["lista 3-5 palavras/frases do texto que indicam obra real, ou []"],
  "capex_mencionado": bool
}}

Se NÃO é obra real, tipo_obra=null."""


def filtro_amplo(texto):
    t = texto.lower()
    return any(k.lower() in t for k in KW_FILTRO)


def call_haiku(client, texto):
    try:
        msg = client.messages.create(
            model=HAIKU, max_tokens=400,
            messages=[{"role": "user", "content": PROMPT.format(texto=texto)}],
        )
    except Exception as e:
        log.warning(f"haiku falhou: {e}")
        return None, 0, 0
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text), msg.usage.input_tokens, msg.usage.output_tokens
    except Exception:
        return None, msg.usage.input_tokens, msg.usage.output_tokens


def main():
    fontes_csv = "/tmp/fontes_descobertas.csv"
    if not os.path.exists(fontes_csv):
        log.error(f"falta {fontes_csv} — rode B1 primeiro")
        sys.exit(2)

    with open(fontes_csv) as f:
        fontes = [r for r in csv.DictReader(f) if r["rss_valido"] == "True"]
    log.info(f"fontes válidas: {len(fontes)}")

    # coleta corpus
    corpus = []
    for f_row in fontes:
        rss = f_row["rss_url"]
        try:
            feed = feedparser.parse(rss, agent=UA, request_headers={"User-Agent": UA})
        except Exception as e:
            log.warning(f"  feedparser {f_row['portal']} falhou: {e}")
            continue
        entries = feed.entries or []
        n_filtradas = 0
        for e in entries[:30]:
            t = (e.get("title") or "") + " " + (e.get("summary") or "")[:600]
            if not filtro_amplo(t):
                continue
            corpus.append({"fonte": f_row["portal"], "texto": t[:1200],
                           "title": e.get("title", "")[:100]})
            n_filtradas += 1
        log.info(f"  {f_row['portal']}: {n_filtradas} textos filtrados (de {len(entries)})")

    log.info(f"\nTotal corpus filtrado: {len(corpus)}")

    # chamar Haiku
    from anthropic import Anthropic
    client = Anthropic()
    resultados = []
    tot_in = tot_out = 0
    for i, item in enumerate(corpus):
        if (i + 1) % 20 == 0:
            log.info(f"  Haiku {i+1}/{len(corpus)} (tok in={tot_in} out={tot_out} custo=${tot_in*1e-6 + tot_out*5e-6:.3f})")
        data, ti, to = call_haiku(client, item["texto"])
        tot_in += ti; tot_out += to
        if data:
            resultados.append({**item, "haiku": data})
        time.sleep(0.1)  # rate limit gentil

    log.info(f"\nHaiku calls: {len(resultados)} ok | total in={tot_in} out={tot_out} | custo ~${tot_in*1e-6 + tot_out*5e-6:.4f}")

    # agregação
    obras_reais = [r for r in resultados if r["haiku"].get("tipo_obra") and r["haiku"]["tipo_obra"] != "null"]
    ruido = [r for r in resultados if not (r["haiku"].get("tipo_obra") and r["haiku"]["tipo_obra"] != "null")]
    log.info(f"obras reais: {len(obras_reais)}  ruído: {len(ruido)}")

    # top palavras por tipo
    por_tipo = defaultdict(Counter)
    for r in obras_reais:
        tipo = r["haiku"]["tipo_obra"]
        for kw in (r["haiku"].get("palavras_chave_indicadoras") or []):
            por_tipo[tipo][kw.lower().strip()] += 1

    # precision: keyword aparece em obra real vs ruído
    em_obra = Counter()
    em_ruido = Counter()
    for r in obras_reais:
        for kw in (r["haiku"].get("palavras_chave_indicadoras") or []):
            em_obra[kw.lower().strip()] += 1
    for r in ruido:
        # extrai keywords do próprio texto que matched o filtro amplo
        t = r["texto"].lower()
        for kw in em_obra:
            if kw in t:
                em_ruido[kw] += 1
    precision = {}
    for kw, cnt_obra in em_obra.most_common(60):
        cnt_ruido = em_ruido.get(kw, 0)
        total = cnt_obra + cnt_ruido
        if total >= 2:
            precision[kw] = {"obra": cnt_obra, "ruido": cnt_ruido,
                             "precision": round(cnt_obra / total, 2)}

    # distribuição fonte × tipo
    fonte_tipo = defaultdict(Counter)
    for r in obras_reais:
        fonte_tipo[r["fonte"]][r["haiku"]["tipo_obra"]] += 1

    # YAML output
    output = {
        "stats": {
            "total_textos": len(corpus),
            "obras_reais": len(obras_reais),
            "ruido": len(ruido),
            "custo_usd": round(tot_in * 1e-6 + tot_out * 5e-6, 4),
        },
        "keywords_por_tipo_obra": {
            tipo: [{"kw": kw, "n": n} for kw, n in cnt.most_common(20)]
            for tipo, cnt in por_tipo.items()
        },
        "precision_por_keyword": {kw: p for kw, p in sorted(
            precision.items(), key=lambda x: -x[1]["precision"])[:40]},
        "distribuicao_fonte_tipo": {
            fonte: dict(cnt) for fonte, cnt in fonte_tipo.items()
        },
    }

    out_path = "/tmp/keywords_otimizadas.yaml"
    with open(out_path, "w") as f:
        yaml.safe_dump(output, f, allow_unicode=True, sort_keys=False, indent=2)
    log.info(f"YAML salvo: {out_path}")

    # sample
    print("\n=== TOP 5 keywords por tipo_obra ===")
    for tipo, cnt in por_tipo.items():
        print(f"  {tipo}: {[k for k, _ in cnt.most_common(5)]}")
    print(f"\n=== TOP 10 precision keywords ===")
    for kw, p in list(sorted(precision.items(), key=lambda x: -x[1]["precision"]))[:10]:
        print(f"  {kw}: precision={p['precision']} (obra={p['obra']} ruido={p['ruido']})")


if __name__ == "__main__":
    main()
