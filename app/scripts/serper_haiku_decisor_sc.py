#!/usr/bin/env python3
"""
serper_haiku_decisor_sc.py — Frente 1 Supply Chain: descobre decisor comercial de
fornecedores de insumo (CNAE 26/20/29) via Serper + Haiku, com gate anti-FP.

Fluxo por empresa (priorizadas por capital_social DESC):
  1) Serper organic: "{razao} diretor OR CEO OR gerente comercial site:linkedin.com"
  2) Haiku extrai do snippet: {nome, cargo, empresa_no_perfil, confianca}
  3) gate anti-FP: empresa_no_perfil bate com a buscada E confianca>=70
     -> infere e-mail {first}.{last}@dominio + INSERT PRATA em empresa_decisores_cache
     <70 ou empresa não bate -> loga 'candidato'/'fp' (NÃO insere)
  4) tudo logado em sc_decisor_fase1_log (anti-reprocesso)

Uso: python serper_haiku_decisor_sc.py --limit 100   (default 100)
"""
import os, sys, json, re, time, unicodedata, logging
import requests
sys.path.insert(0, "/app"); sys.path.insert(0, "/app/scripts")
import psycopg2, psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sc_fase1")
SERPER_KEY = os.getenv("SERPER_API_KEY")
DIVISOES = ("26", "20", "29")
LINKEDIN_OK = re.compile(r"linkedin\.com/in/", re.I)
_BAD_DOM = ("linkedin.", "facebook.", "instagram.", "google.", "youtube.", "wikipedia.", "gov.br", "jusbrasil", "cnpj")


def conn():
    return psycopg2.connect(host=os.getenv("DB_HOST", "db"), dbname=os.getenv("DB_NAME", "wins_hub"),
                            user=os.getenv("DB_USER", "postgres"), password=os.getenv("DB_PASSWORD", ""))


def _norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def serper(q):
    try:
        r = requests.post("https://google.serper.dev/search",
                          headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
                          json={"q": q, "gl": "br", "hl": "pt", "num": 6}, timeout=15)
        return r.json().get("organic", []) if r.ok else []
    except Exception as e:
        log.warning(f"serper erro: {e!r}"); return []


HAIKU_PROMPT = """Você recebe o NOME DE UMA EMPRESA e resultados de busca (LinkedIn). Extraia o decisor comercial/executivo MAIS provável que TRABALHA NESTA empresa.
Empresa buscada: {empresa}
Resultados:
{snippets}

Responda SÓ com JSON: {{"nome": "...", "cargo": "...", "empresa_no_perfil": "...", "confianca": 0-100}}
Regras: confianca alta só se a pessoa claramente trabalha NA empresa buscada (empresa_no_perfil bate). Se nenhum resultado for de pessoa desta empresa, confianca<=30. nome vazio se não houver pessoa."""


def haiku_extrai(client, MODEL, empresa, organic):
    snips = "\n".join(f"- {o.get('title','')} | {o.get('snippet','')} | {o.get('link','')}"
                      for o in organic[:6] if LINKEDIN_OK.search(o.get("link", "")) or "linkedin" in o.get("link", ""))
    if not snips:
        snips = "\n".join(f"- {o.get('title','')} | {o.get('snippet','')}" for o in organic[:4])
    if not snips.strip():
        return None
    try:
        msg = client.messages.create(model=MODEL, max_tokens=250,
              messages=[{"role": "user", "content": HAIKU_PROMPT.format(empresa=empresa, snippets=snips[:2500])}])
        txt = msg.content[0].text.strip()
        txt = re.sub(r"^```(json)?|```$", "", txt, flags=re.M).strip()
        return json.loads(txt)
    except Exception as e:
        log.warning(f"haiku erro {empresa[:30]!r}: {e!r}"); return None


_CARGO_OK = re.compile(r"compras|suprimento|procurement|comercial|commercial|vendas|sales|supply\s*chain|\bceo\b|diretor[ -]geral|general manager|managing director", re.I)
_CARGO_BAD = re.compile(r"recursos humanos|human resources|\brh\b|\bti\b|tecnologia da inf|information technology|engenh|engineer|jur[i\u00ed]dic|\blegal\b|advog", re.I)


def cargo_comercial(cargo):
    """Aprova só cargo comercial/compras/CEO; rejeita RH/TI/Engenharia/Jurídico."""
    c = cargo or ""
    return bool(_CARGO_OK.search(c)) and not _CARGO_BAD.search(c)


def empresa_bate(buscada, perfil):
    b = _norm(buscada); p = _norm(perfil or "")
    if not p:
        return False
    toks = [t for t in re.split(r"[^a-z0-9]+", b) if len(t) > 3
            and t not in ("ltda", "industria", "comercio", "servicos", "brasil", "do", "de", "da", "industrial")]
    return any(t in p for t in toks[:3]) if toks else False


def dominio_de(cur, cnpj, organic):
    raiz = re.sub(r"\D", "", cnpj or "")[:8]
    cur.execute("""SELECT dominio FROM empresa_dominios
                   WHERE (cnpj=%s OR substring(regexp_replace(coalesce(cnpj,''),'\\D','','g'),1,8)=%s)
                     AND coalesce(dominio,'')<>'' LIMIT 1""", (cnpj, raiz))
    r = cur.fetchone()
    if r:
        return r[0]
    for o in organic:  # fallback: site próprio nos resultados
        link = o.get("link", "")
        m = re.search(r"https?://([^/]+)", link)
        if m and not any(b in m.group(1) for b in _BAD_DOM):
            return m.group(1).replace("www.", "")
    return None


def monta_email(nome, dominio):
    if not dominio or not nome:
        return None
    parts = [p for p in _norm(nome).split() if p.isalpha()]
    if len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[-1]}@{dominio}"


_LOG = """INSERT INTO sc_decisor_fase1_log (cnpj, empresa, status, nome, cargo, confianca, snippet, processado_em)
  VALUES (%s,%s,%s,%s,%s,%s,%s, now()) ON CONFLICT (cnpj) DO UPDATE SET
  status=EXCLUDED.status, nome=EXCLUDED.nome, cargo=EXCLUDED.cargo, confianca=EXCLUDED.confianca, processado_em=now()"""
_CACHE = """INSERT INTO empresa_decisores_cache
  (cnpj, nome_pessoa, cargo_raw, confianca, email, email_status, fonte_descoberta, snippet_origem, url_origem, linkedin_slug, score_relevancia, descoberto_em, filtro_llm_confianca, filtro_llm_em)
  VALUES (%s,%s,%s,%s,%s,%s,'serper_haiku_sc_fase1',%s,%s,%s,%s, now(), %s, now())"""


def main():
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--limit", type=int, default=100); args = ap.parse_args()
    from sales_intelligence.llm_enricher.client import get_client, MODEL_HAIKU
    client = get_client()
    c = conn(); cur = c.cursor(); cur2 = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur2.execute("""
      SELECT f.cnpj, f.razao_social AS empresa, f.divisao_cnae AS div, f.capital_social
      FROM fornecedores f
      WHERE f.divisao_cnae IN ('26','20','29') AND f.situacao_cadastral='02' AND coalesce(f.razao_social,'')<>''
        AND NOT EXISTS (SELECT 1 FROM sc_decisor_fase1_log l WHERE l.cnpj=f.cnpj)
      ORDER BY f.capital_social DESC NULLS LAST LIMIT %s""", (args.limit,))
    alvos = cur2.fetchall()
    stats = {"aprovado": 0, "candidato": 0, "fp": 0, "sem_resultado": 0}
    exemplos = {"top": [], "piores": []}
    t0 = time.time()
    for row in alvos:
        cnpj, empresa = row["cnpj"], row["empresa"]
        organic = serper(f'"{empresa}" diretor OR CEO OR "gerente comercial" site:linkedin.com')
        if not organic:
            cur.execute(_LOG, (cnpj, empresa, "sem_resultado", None, None, 0, None)); stats["sem_resultado"] += 1; c.commit(); continue
        ext = haiku_extrai(client, MODEL_HAIKU, empresa, organic) or {}
        nome = (ext.get("nome") or "").strip(); cargo = (ext.get("cargo") or "").strip()
        conf = int(ext.get("confianca") or 0); perfil = ext.get("empresa_no_perfil")
        bate = empresa_bate(empresa, perfil)
        snippet = (organic[0].get("snippet") or "")[:300]
        cargo_ok = cargo_comercial(cargo)
        if nome and conf >= 70 and bate and cargo_ok:
            dom = dominio_de(cur, cnpj, organic)
            email = monta_email(nome, dom)
            slug_m = re.search(r"linkedin\.com/in/([^/?\s]+)", " ".join(o.get("link", "") for o in organic))
            clabel = "alta" if conf >= 80 else "media"
            cur.execute(_CACHE, (cnpj, nome, cargo, clabel, email,
                                 "inferred_pattern" if email else "pending", snippet,
                                 organic[0].get("link"), slug_m.group(1) if slug_m else None, round(conf/100.0,2), clabel))
            cur.execute(_LOG, (cnpj, empresa, "aprovado", nome, cargo, conf, snippet))
            stats["aprovado"] += 1
            if len(exemplos["top"]) < 5:
                exemplos["top"].append(f"{empresa[:30]} -> {nome} ({cargo}) conf={conf} email={email}")
        else:
            st = "fp" if (nome and not bate) else "candidato"
            cur.execute(_LOG, (cnpj, empresa, st, nome or None, cargo or None, conf, snippet))
            stats[st] += 1
            if len(exemplos["piores"]) < 5:
                exemplos["piores"].append(f"{empresa[:30]} -> nome={nome!r} perfil={perfil!r} conf={conf} [{st}]")
        c.commit()
    dt = time.time() - t0
    n = len(alvos)
    print(json.dumps({
        "processadas": n, "aprovado": stats["aprovado"], "candidato": stats["candidato"],
        "fp_detectado": stats["fp"], "sem_resultado": stats["sem_resultado"],
        "taxa_aprovacao_pct": round(100 * stats["aprovado"] / n, 1) if n else 0,
        "taxa_fp_pct": round(100 * stats["fp"] / n, 1) if n else 0,
        "tempo_medio_s": round(dt / n, 2) if n else 0,
        "exemplos_top": exemplos["top"], "exemplos_piores": exemplos["piores"],
    }, ensure_ascii=False, indent=2))
    c.close()


if __name__ == "__main__":
    main()
