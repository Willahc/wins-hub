#!/usr/bin/env python3
"""
Repopulacao de CNPJs das obras antt_rod via DDG search + BrasilAPI lookup.

REGRAS DURAS (briefing 10/05/2026):
- NAO faz UPDATE no banco. Apenas gera JSON.
- Saida:
    /tmp/antt_rod_cnpjs_recuperados.json  (sucessos)
    /tmp/antt_rod_cnpjs_revisao.json      (revisao manual)
    /tmp/antt_rod_cnpjs_falhas.json       (sem hit)
- Log: /var/log/wins_hub/repopular_cnpj_antt_rod.log

Critérios:
  SUCESSO  = DV valido + BrasilAPI 200 + situacao ATIVA + score >= 0.6 + matriz
  REVISAO  = tem CNPJ DV valido mas algum check falha
  FALHA    = sem hit DDG / só filiais / BrasilAPI persistentemente 4xx-5xx

Anti-rate-limit:
  45s entre queries DDG, 2s entre BrasilAPI, 5min cooldown em 412/429.
"""
import os, re, sys, json, time, logging, subprocess
from difflib import SequenceMatcher
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

# ─── Config ────────────────────────────────────────────────────────────
CHROMIUM   = "/usr/bin/chromium-browser"
LAUNCH_ARGS = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
UA_DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"

DELAY_DDG_S          = 45
DELAY_BRASILAPI_S    = 2
COOLDOWN_RATE_LIMIT  = 300
DDG_TIMEOUT_MS       = 25000
BRASILAPI_TIMEOUT_MS = 15000
SCORE_MIN_SUCESSO    = 0.6

OUT_RECUPERADOS = "/tmp/antt_rod_cnpjs_recuperados.json"
OUT_REVISAO     = "/tmp/antt_rod_cnpjs_revisao.json"
OUT_FALHAS      = "/tmp/antt_rod_cnpjs_falhas.json"
LOG_PATH        = "/var/log/wins_hub/repopular_cnpj_antt_rod.log"

CNPJ_FMT_RE = re.compile(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})")
HTML_TAG_RE = re.compile(r"<[^>]+>")

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("repopular_antt_rod")


# ─── Utils ─────────────────────────────────────────────────────────────
def cnpj_valido(c):
    c = re.sub(r"\D", "", c or "")
    if len(c) != 14 or c == c[0]*14:
        return False
    p1 = [5,4,3,2,9,8,7,6,5,4,3,2]
    p2 = [6,5,4,3,2,9,8,7,6,5,4,3,2]
    s = sum(int(c[i])*p1[i] for i in range(12)); r = s % 11
    if int(c[12]) != (0 if r < 2 else 11 - r): return False
    s = sum(int(c[i])*p2[i] for i in range(13)); r = s % 11
    return int(c[13]) == (0 if r < 2 else 11 - r)


def normalizar(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ASCII", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9 ]", " ", s).lower()
    return re.sub(r"\s+", " ", s).strip()


def score_match(a, b):
    """Similaridade fuzzy normalizando termos comerciais comuns."""
    na, nb = normalizar(a), normalizar(b)
    sufixos = ["s a", "sa", "ltda", "limitada", "concessionaria", "concessionario",
               "rodoviaria", "rodoviario", "rodovias", "rodovia",
               "do sistema", "sistema", "br ", "br"]
    for sfx in sufixos:
        na = re.sub(r"\b" + re.escape(sfx) + r"\b", " ", na)
        nb = re.sub(r"\b" + re.escape(sfx) + r"\b", " ", nb)
    na = re.sub(r"\s+", " ", na).strip()
    nb = re.sub(r"\s+", " ", nb).strip()
    return SequenceMatcher(None, na, nb).ratio()


def carregar_alvos():
    """26 obras antt_rod sem CNPJ via docker exec psql."""
    sql = ("SELECT id_externo, empresa, COALESCE(valor_estimado,0) "
           "FROM obras WHERE fonte='antt_rod' AND cnpj IS NULL "
           "ORDER BY valor_estimado DESC NULLS LAST")
    r = subprocess.run(
        ["docker", "exec", "wins_hub-db-1", "psql", "-U", "postgres",
         "-d", "wins_hub", "-tAc", sql],
        capture_output=True, text=True, timeout=30, check=True,
    )
    alvos = []
    for line in r.stdout.strip().split("\n"):
        if not line: continue
        parts = line.split("|")
        if len(parts) < 3: continue
        id_ext = parts[0].strip()
        sigla = id_ext.replace("ANTT-ROD-", "").replace("_", " ").strip()
        alvos.append({
            "id_externo": id_ext,
            "sigla": sigla,
            "empresa": parts[1].strip(),
            "valor": float(parts[2].strip() or 0),
        })
    return alvos


def montar_query(sigla, empresa):
    """DDG query: sigla entre aspas + termos contextuais."""
    q = f'"{sigla}" rodovia CNPJ'
    return q.replace(" ", "+").replace('"', "%22")


def fetch_ddg(page, query):
    url = f"https://html.duckduckgo.com/html/?q={query}"
    log.info(f"  DDG: {url}")
    r = page.goto(url, timeout=DDG_TIMEOUT_MS, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    return (r.status if r else 0), page.content()


def extrair_cnpjs(raw):
    """Extrai CNPJs únicos válidos com contexto + flag matriz."""
    out = {}
    for m in CNPJ_FMT_RE.finditer(raw):
        cf = m.group(1); cd = re.sub(r"\D", "", cf)
        if cd in out or not cnpj_valido(cd): continue
        i = m.start()
        ctx = HTML_TAG_RE.sub(" ", raw[max(0, i-300): i+300])
        ctx = re.sub(r"\s+", " ", ctx).strip()
        out[cd] = {
            "cnpj": cd, "cnpj_formatado": cf,
            "matriz": cd[8:12] == "0001",
            "contexto_snippet": ctx[:300],
        }
    return list(out.values())


def buscar_brasilapi(page, cnpj):
    url = f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
    log.info(f"  BrasilAPI: {url}")
    try:
        r = page.goto(url, timeout=BRASILAPI_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        status = r.status if r else 0
        if status != 200:
            log.warning(f"  BrasilAPI HTTP {status}")
            return None, status
        return json.loads(page.inner_text("body")), 200
    except Exception as e:
        log.warning(f"  BrasilAPI exception: {str(e)[:140]}")
        return None, 0


def calcular_score(alvo, ba_data):
    razao = (ba_data or {}).get("razao_social", "") or ""
    s1 = score_match(alvo["sigla"], razao)
    empresa_nome = alvo["empresa"].split(" - ")[0] if " - " in alvo["empresa"] else alvo["empresa"]
    s2 = score_match(empresa_nome, razao)
    s3 = score_match(alvo["empresa"], razao)
    return max(s1, s2, s3)


def classificar(alvo, candidato, ba_data, score):
    if candidato is None:
        return "falha", "nenhum CNPJ matriz encontrado no DDG"
    if not candidato["matriz"]:
        return "falha", "apenas CNPJs de filial encontrados"
    if ba_data is None:
        return "revisao", "BrasilAPI nao retornou dados"
    sit = (ba_data.get("descricao_situacao_cadastral") or "").upper()
    if sit != "ATIVA":
        return "revisao", f"situacao={sit!r} (nao ATIVA)"
    if score < SCORE_MIN_SUCESSO:
        razao = ba_data.get("razao_social", "")
        return "revisao", f"score baixo {score:.2f} (razao_social={razao!r})"
    cnae_str = str(ba_data.get("cnae_fiscal") or "")
    cnae_desc = (ba_data.get("cnae_fiscal_descricao") or "").lower()
    cnae_4 = cnae_str[:4] if cnae_str else ""
    if cnae_4 != "5221":
        if not any(t in cnae_desc for t in ("concession", "rodovi", "holding", "infraestrut")):
            return "revisao", f"CNAE {cnae_str} ({cnae_desc!r}) fora de concessionaria"
    return "sucesso", f"score={score:.2f} sit=ATIVA cnae={cnae_4}"


def construir_record(alvo, hits, candidato, ba_data, score, query, classif, motivo):
    rec = {
        "nome_concessionaria": alvo["sigla"],
        "id_externo": alvo["id_externo"],
        "empresa_db": alvo["empresa"],
        "valor_estimado": alvo["valor"],
        "fonte_primaria_query": query,
        "fonte_primaria_hits_total": len(hits),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "classificacao": classif,
        "motivo": motivo,
    }
    if candidato:
        rec["cnpj"] = candidato["cnpj"]
        rec["cnpj_formatado"] = candidato["cnpj_formatado"]
        rec["matriz"] = candidato["matriz"]
        rec["validado_dv"] = True
        rec["fonte_secundaria_url"] = f"https://brasilapi.com.br/api/cnpj/v1/{candidato['cnpj']}"
        rec["snippet_primario"] = candidato["contexto_snippet"]
    if ba_data:
        rec["razao_social_brasilapi"] = ba_data.get("razao_social")
        rec["nome_fantasia"] = ba_data.get("nome_fantasia")
        rec["cnae_fiscal_codigo"] = ba_data.get("cnae_fiscal")
        rec["cnae_fiscal_descricao"] = ba_data.get("cnae_fiscal_descricao")
        rec["situacao_cadastral"] = ba_data.get("descricao_situacao_cadastral")
        rec["uf"] = ba_data.get("uf")
        rec["municipio"] = ba_data.get("municipio")
        rec["razao_match_score"] = round(score, 3)
    return rec


def salvar(recuperados, revisao, falhas):
    for path, data in [(OUT_RECUPERADOS, recuperados), (OUT_REVISAO, revisao), (OUT_FALHAS, falhas)]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    log.info("=" * 72)
    log.info("INICIO repopular_cnpj_antt_rod (apenas geracao de JSON, NAO update)")
    log.info("=" * 72)

    alvos = carregar_alvos()
    log.info(f"Alvos: {len(alvos)} concessionarias antt_rod sem CNPJ")
    if not alvos:
        log.info("Nada a processar.")
        salvar([], [], [])
        return 0

    recuperados, revisao, falhas = [], [], []
    t0 = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=LAUNCH_ARGS)
        ctx = browser.new_context(user_agent=UA_DESKTOP)
        page = ctx.new_page()

        for idx, alvo in enumerate(alvos, 1):
            log.info(f"\n[{idx}/{len(alvos)}] {alvo['sigla']} (R$ {alvo['valor']/1e6:.0f}M)")
            query = montar_query(alvo["sigla"], alvo["empresa"])

            html = ""; status_ddg = 0
            for tentativa in (1, 2):
                try:
                    status_ddg, html = fetch_ddg(page, query)
                    if status_ddg in (412, 429, 403):
                        log.warning(f"  Rate-limit DDG (HTTP {status_ddg}). Pausa {COOLDOWN_RATE_LIMIT}s")
                        time.sleep(COOLDOWN_RATE_LIMIT)
                        continue
                    break
                except Exception as e:
                    log.warning(f"  DDG exception (tent {tentativa}): {str(e)[:140]}")
                    time.sleep(10)

            hits = extrair_cnpjs(html)
            log.info(f"  CNPJs validos no resultado: {len(hits)}")
            matrizes = [h for h in hits if h["matriz"]]
            candidato = matrizes[0] if matrizes else None

            ba_data = None; score = 0.0
            if candidato:
                ba_data, _ = buscar_brasilapi(page, candidato["cnpj"])
                time.sleep(DELAY_BRASILAPI_S)
                if ba_data:
                    score = calcular_score(alvo, ba_data)

            classif, motivo = classificar(alvo, candidato, ba_data, score)
            rec = construir_record(alvo, hits, candidato, ba_data, score, query, classif, motivo)

            if classif == "sucesso":
                recuperados.append(rec)
                log.info(f"  -> SUCESSO  cnpj={candidato['cnpj_formatado']} score={score:.2f}  ({motivo})")
            elif classif == "revisao":
                revisao.append(rec)
                log.info(f"  -> REVISAO  ({motivo})")
            else:
                falhas.append(rec)
                log.info(f"  -> FALHA    ({motivo})")

            if idx % 5 == 0 or idx == len(alvos):
                salvar(recuperados, revisao, falhas)
                el = time.time() - t0
                log.info(f"  >>> Progresso: {idx}/{len(alvos)} | ok={len(recuperados)} rev={len(revisao)} fail={len(falhas)} | {el/60:.1f}min")

            if idx < len(alvos):
                log.info(f"  Aguardando {DELAY_DDG_S}s anti-rate-limit DDG...")
                time.sleep(DELAY_DDG_S)

        browser.close()

    salvar(recuperados, revisao, falhas)
    elapsed = time.time() - t0

    log.info("\n" + "=" * 72)
    log.info(f"FIM | tempo total: {elapsed/60:.1f} min")
    log.info(f"  Recuperados: {len(recuperados)}/{len(alvos)}")
    log.info(f"  Revisao:     {len(revisao)}/{len(alvos)}")
    log.info(f"  Falhas:      {len(falhas)}/{len(alvos)}")
    log.info("=" * 72)
    if revisao:
        log.info("\nPara revisao manual:")
        for r in revisao:
            log.info(f"  - {r['nome_concessionaria']}: {r['motivo']}")
    if falhas:
        log.info("\nFalhas:")
        for r in falhas:
            log.info(f"  - {r['nome_concessionaria']}: {r['motivo']}")
    tempo_manual = len(revisao) * 3 + len(falhas) * 5
    log.info(f"\nEstimativa tempo manual pra resolver pendentes: ~{tempo_manual} min")
    log.info(f"\nArquivos:")
    log.info(f"  {OUT_RECUPERADOS}")
    log.info(f"  {OUT_REVISAO}")
    log.info(f"  {OUT_FALHAS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
