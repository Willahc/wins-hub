#!/usr/bin/env python3
"""V8 — Chromium Resolver via FlareSolverr.

Resolve as 120 empresas pendentes: CNPJ.biz + Google → CNPJ + UF + dominio oficial.
Zero Hunter, zero Claude tokens. Persiste em v8_chromium_results.

Lê /tmp/outputs/empresas_validar_dominio_20260513.csv (120 linhas).
"""
from __future__ import annotations
import atexit, csv, json, os, re, sys, time, traceback
sys.path.insert(0, "/app")
import psycopg2
import requests

DB = dict(host="db", port=5432, dbname="wins_hub", user="postgres",
          password=os.environ["DB_PASSWORD"])
FLARE = "http://wins_hub-flaresolverr:8191/v1"
INPUT_CSV = "/tmp/empresas_validar_dominio_20260513.csv"

AGREGADORES = {
    "google", "googleusercontent", "googleadservices", "gstatic",
    "wikipedia", "wikimedia", "youtube", "youtu.be",
    "linkedin", "facebook", "instagram", "twitter", "x.com",
    "tiktok", "cnpj.biz", "cnpj.", "casadosdados", "econodata",
    "consultas-cnpj", "guiamais", "telelistas", "reclameaqui",
    "jusbrasil", "bing", "duckduckgo", "yahoo", "britannica",
    "w3.org", "schema.org", "ogp.me", "apontador", "amazon",
}

# TLDs aceitos pra dominio oficial corporativo
TLDS_OK = (".com.br", ".com", ".org.br", ".ind.br", ".net.br", ".net", ".co.br")

STATS = {
    "total": 0, "ok_dominio": 0, "ok_cnpj_descoberto": 0,
    "ja_tinha_cnpj_ok": 0, "revisao_manual": 0,
    "flare_falhou": 0, "tempo_seg": 0,
}
T0 = time.time()


@atexit.register
def _emit():
    STATS["tempo_seg"] = round(time.time() - T0, 1)
    print(f"\nSTATS_JSON: {json.dumps(STATS)}", flush=True)


def flare_get(url: str, max_ms: int = 60000):
    """Retorna HTML ou None."""
    try:
        r = requests.post(FLARE, json={"cmd": "request.get", "url": url,
                                       "maxTimeout": max_ms},
                          timeout=max_ms / 1000 + 60)
        if r.status_code != 200:
            return None
        d = r.json()
        if d.get("status") != "ok":
            return None
        return (d.get("solution") or {}).get("response") or None
    except Exception as e:
        print(f"  flare erro {url[:60]}: {e}", flush=True)
        return None


def _extrair_dominio(url: str) -> str | None:
    m = re.match(r"https?://(?:www\.)?([a-zA-Z0-9.-]+)", url.strip())
    if not m:
        return None
    h = m.group(1).lower()
    return h


def _is_aggregator(d: str) -> bool:
    return any(a in d for a in AGREGADORES)


def buscar_cnpj_biz(razao: str) -> dict:
    """Retorna {cnpj, razao_oficial, uf} ou {}."""
    q = re.sub(r"\s+", "+", razao.strip())
    url = f"https://cnpj.biz/buscar?q={q}"
    html = flare_get(url)
    if not html:
        return {}
    # primeira ocorrência de /[\d]+ apontando empresa
    m = re.search(r'href="(/\d{2,14}(?:[/-][\w-]+)?)"', html)
    if not m:
        return {}
    empresa_url = "https://cnpj.biz" + m.group(1)
    time.sleep(2)
    html_emp = flare_get(empresa_url)
    if not html_emp:
        return {}
    out = {}
    cnpj_m = re.search(r'(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})', html_emp)
    if cnpj_m:
        out["cnpj"] = re.sub(r"[^\d]", "", cnpj_m.group(1))
    # razão social - geralmente em <h1> ou primeiro <strong>
    rs_m = re.search(r'<h1[^>]*>([^<]+)</h1>', html_emp)
    if rs_m:
        out["razao_oficial"] = rs_m.group(1).strip()
    uf_m = re.search(r'\b(?:UF|Estado)[:\s]*[<>/\w" ]*?([A-Z]{2})\b', html_emp)
    if uf_m:
        out["uf"] = uf_m.group(1)
    return out


def buscar_dominio_google(razao: str) -> str | None:
    q = re.sub(r"\s+", "+", razao.strip())
    url = f"https://www.google.com/search?q={q}+site+oficial"
    html = flare_get(url)
    if not html:
        return None
    # extrair URLs http
    urls = re.findall(r'https?://[a-zA-Z0-9._/-]+', html)
    seen = []
    for u in urls[:200]:
        d = _extrair_dominio(u)
        if not d:
            continue
        if _is_aggregator(d):
            continue
        if not any(d.endswith(t) for t in TLDS_OK):
            continue
        # filtrar subdomínios "/url?q=", paths internos
        if d in seen:
            continue
        seen.append(d)
        if len(seen) >= 3:
            break
    return seen[0] if seen else None


def resolver(razao: str, cnpj_input: str | None) -> dict:
    res = {
        "razao_social_input": razao,
        "cnpj_input": cnpj_input,
        "cnpj_descoberto": cnpj_input,
        "razao_social_oficial": None,
        "uf": None,
        "dominio_oficial": None,
        "fonte_dominio": None,
        "confidence": "baixa",
        "obs": "",
        "precisa_revisao_manual": False,
    }
    # 1) se sem CNPJ, buscar via cnpj.biz
    if not cnpj_input:
        info = buscar_cnpj_biz(razao)
        if info.get("cnpj"):
            res["cnpj_descoberto"] = info["cnpj"]
            res["razao_social_oficial"] = info.get("razao_oficial")
            res["uf"] = info.get("uf")
            STATS["ok_cnpj_descoberto"] += 1
    else:
        STATS["ja_tinha_cnpj_ok"] += 1

    # 2) buscar dominio via google
    dom = buscar_dominio_google(razao)
    if dom:
        res["dominio_oficial"] = dom
        res["fonte_dominio"] = "google_top1"
        res["confidence"] = "media"
        STATS["ok_dominio"] += 1
    else:
        res["obs"] += "google_sem_dominio;"
        res["precisa_revisao_manual"] = True
        STATS["flare_falhou"] += 1
    return res


def main():
    conn = psycopg2.connect(**DB)
    # ler CSV
    with open(INPUT_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    # skipar já processados (idempotente)
    with conn.cursor() as cur:
        cur.execute("SELECT razao_social_input FROM v8_chromium_results")
        ja_feitos = {r[0] for r in cur.fetchall()}
    print(f"V8 chromium: total={len(rows)} ja_feitos={len(ja_feitos)} pendentes={len(rows)-len(ja_feitos)}", flush=True)
    STATS["total"] = len(rows)

    for i, row in enumerate(rows, 1):
        razao = (row.get("razao_social") or "").strip()
        if razao in ja_feitos:
            continue
        cnpj = (row.get("cnpj") or "").strip()
        if cnpj == "(SEM CNPJ)" or not cnpj:
            cnpj = None
        try:
            res = resolver(razao, cnpj)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_chromium_results (
                        razao_social_input, cnpj_input, cnpj_descoberto,
                        razao_social_oficial, uf, dominio_oficial, fonte_dominio,
                        confidence, obs, precisa_revisao_manual
                    ) VALUES (%(razao_social_input)s, %(cnpj_input)s, %(cnpj_descoberto)s,
                              %(razao_social_oficial)s, %(uf)s, %(dominio_oficial)s,
                              %(fonte_dominio)s, %(confidence)s, %(obs)s,
                              %(precisa_revisao_manual)s)
                """, res)
            conn.commit()
            tag = "✓" if res["dominio_oficial"] else "✗"
            print(f"[{i}/{len(rows)}] {tag} {razao[:40]} → dom={res['dominio_oficial']} cnpj={res['cnpj_descoberto']}", flush=True)
        except Exception as e:
            print(f"[{i}/{len(rows)}] ERRO {razao[:40]}: {e}", flush=True)
            STATS["flare_falhou"] += 1
        if i % 20 == 0:
            print(f"--- progress {i}/{len(rows)} stats={STATS} ---", flush=True)
        time.sleep(3)
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException as e:
        print(f"UNCAUGHT: {type(e).__name__}: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        rc = 1
    os._exit(rc or 0)
