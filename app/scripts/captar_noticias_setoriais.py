#!/usr/bin/env python3
"""Captador notícias setoriais — RSS + Haiku extração + BrasilAPI validação.

Pipeline:
  1. Carrega YAML de fontes RSS + keywords
  2. Pra cada fonte: feedparser.parse → entries
  3. Filtro keyword pre-LLM (economiza calls Haiku em notícias irrelevantes)
  4. Hash MD5(title+link) → dedup contra noticias_processadas
  5. Haiku claude-haiku-4-5-20251001 extrai JSON estruturado
  6. BrasilAPI valida CNPJ (se fornecido) ou retorna por razão social
  7. Dedup contra obras existentes (cnpj + UF + capex 0.7x-1.3x)
  8. INSERT obras com fonte='noticia_<fonte>', status='anunciado'
  9. UPDATE noticias_processadas

Uso:
    python /app/scripts/captar_noticias_setoriais.py            # todas fontes
    python /app/scripts/captar_noticias_setoriais.py --fonte click_petroleo_gas
    python /app/scripts/captar_noticias_setoriais.py --dry      # não persiste
"""
import sys
sys.path.insert(0, "/app")

import argparse
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, date
from pathlib import Path

import feedparser
import psycopg2
import yaml
from psycopg2.extras import RealDictCursor, Json

from utils.parse_data_br import parse_data_br

LOG_DIR = Path("/app/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"captar_noticias_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("captar_noticias")

# STATS_JSON — orquestrador parseia última linha pra contadores em log_captacao
import atexit as _atexit
import json as _stats_json
_STATS = {"buscados": 0, "novos": 0, "erros": 0}
def _emit_stats_json():
    try:
        print(f"STATS_JSON: {_stats_json.dumps(_STATS)}", flush=True)
    except Exception:
        pass
_atexit.register(_emit_stats_json)


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

YAML_PATH = Path("/app/scripts/fontes_noticias.yaml")
HAIKU_MODEL = "claude-haiku-4-5-20251001"
# Free-first (25/06): conta Anthropic sem saldo. Com HAIKU_HABILITADO=false usa
# a cadeia LLM GRATUITA (Groq 70B -> Gemini Flash -> OpenRouter) p/ extrair campos.
# Voltar ao Haiku quando houver receita: HAIKU_HABILITADO=true (ou remover a var).
USAR_LLM_GRATIS = os.getenv("HAIKU_HABILITADO", "true").strip().lower() in ("false", "0", "no", "off")
try:
    from services.llm_extracao import extrair_json as _extrair_json_gratis
except Exception:
    _extrair_json_gratis = None

# Setores canônicos aceitos para obras vindas de notícia. Defensivo: Haiku às vezes
# ignora as instrucoes do prompt e devolve 'INDUSTRIA' (sem 'L') ou 'LOGISTICA' (sem 'O').
SETORES_CANONICOS = {
    "INDUSTRIAL", "ENERGIA", "LOGISTICO", "MINERACAO", "INFRAESTRUTURA",
    "SANEAMENTO", "AGRO", "DATA_CENTER", "OUTRO",
}
SETOR_ALIASES = {
    # input (uppercase, sem acento) -> canônico
    "INDUSTRIA":           "INDUSTRIAL",
    "INDUSTRIAS":          "INDUSTRIAL",
    "FABRICA":             "INDUSTRIAL",
    "MANUFATURA":          "INDUSTRIAL",
    "LOGISTICA":           "LOGISTICO",
    "TRANSPORTE":          "LOGISTICO",
    "FERROVIA":            "LOGISTICO",
    "FERROVIARIO":         "LOGISTICO",
    "RODOVIA":             "INFRAESTRUTURA",
    "RODOVIARIO":          "INFRAESTRUTURA",
    "PORTUARIO":           "INFRAESTRUTURA",
    "PORTO":               "INFRAESTRUTURA",
    "AEROPORTO":           "INFRAESTRUTURA",
    "MINERACAO":           "MINERACAO",
    "MINERIO":             "MINERACAO",
    "ENERGIA":             "ENERGIA",
    "ELETRICA":            "ENERGIA",
    "EOLICA":              "ENERGIA",
    "SOLAR":               "ENERGIA",
    "TECNOLOGIA":          "DATA_CENTER",
    "TI":                  "DATA_CENTER",
    "DATACENTER":          "DATA_CENTER",
    "DATA_CENTER":         "DATA_CENTER",
    "AGRO":                "AGRO",
    "AGRICULTURA":         "AGRO",
    "AGRONEGOCIO":         "AGRO",
    "AGROINDUSTRIAL":      "AGRO",
    "SANEAMENTO":          "SANEAMENTO",
    "AGUA":                "SANEAMENTO",
    "ESGOTO":              "SANEAMENTO",
    "INFRAESTRUTURA":      "INFRAESTRUTURA",
    "GOVERNO":             "OUTRO",
    "OUTROS":              "OUTRO",
    "OUTRO":               "OUTRO",
}


def _normalizar_setor(raw):
    """Converte saida do Haiku pra setor canônico. None ou inválido => 'OUTRO'."""
    if not raw:
        return "OUTRO"
    s = str(raw).strip().upper().replace("Á", "A").replace("É", "E").replace("Í", "I") \
                 .replace("Ó", "O").replace("Ú", "U").replace("Ç", "C").replace("Ã", "A") \
                 .replace("Õ", "O").replace("Ê", "E").replace("Ô", "O").replace("Â", "A")
    if s in SETORES_CANONICOS:
        return s
    return SETOR_ALIASES.get(s, "OUTRO")


PROMPT_BASE = """Você analisa notícias brasileiras de investimentos industriais/infraestrutura.
Extraia APENAS se a notícia anuncia uma OBRA/INVESTIMENTO REAL no Brasil (não rumor, não opinião, não geral sobre setor).

Notícia:
TÍTULO: {title}
RESUMO: {summary}
URL: {link}
{tipo_hint}
Retorne JSON puro (sem markdown), schema:
{{
  "eh_obra_real": bool,
  "motivo_skip": "string se eh_obra_real=false, senão null",
  "empresa_nome": "razão social ou nome comercial",
  "cnpj_provavel": "se mencionado, senão null",
  "capex_brl": "valor em REAIS, número puro. Se '2 bilhões' → 2000000000",
  "uf": "sigla 2 letras",
  "municipio": "string ou null",
  "setor": "EXATAMENTE um dos valores: INDUSTRIAL, ENERGIA, LOGISTICO, MINERACAO, INFRAESTRUTURA, SANEAMENTO, AGRO, DATA_CENTER, OUTRO",
  "cnae_provavel": "código CNAE 7 dígitos ou null",
  "prazo_inicio_operacao": "YYYY-MM ou null",
  "descricao_curta": "1 frase",
  "confianca": "0.0-1.0"
}}

REGRAS DE SETOR:
- Use EXATAMENTE um dos rótulos canônicos acima (uppercase, sem acentos).
- INDUSTRIAL (NÃO 'INDUSTRIA', NÃO 'INDÚSTRIA'); LOGISTICO (NÃO 'LOGISTICA').
- Se nenhum se aplica, use OUTRO.

Se confianca < 0.6, marque eh_obra_real=false."""

# Hints por tipo_provavel (matcheado via keywords_por_tipo_obra) — guia extração especializada.
TIPO_HINTS = {
    "fabrica": "\nContexto: notícia provável de NOVA FÁBRICA / PLANTA INDUSTRIAL. Priorize extração de: capex_brl (sempre presente em anúncios de fábrica), municipio (cidade-sede da unidade), cnae_provavel (atividade industrial principal).",
    "infraestrutura": "\nContexto: notícia provável de OBRA DE INFRAESTRUTURA (rodovia/saneamento/PPP). Priorize: descricao_curta com tipo de concessao/edital, uf+municipio do trecho, prazo_inicio_operacao se mencionado, órgão responsável no campo empresa_nome (DNIT/Estado/etc).",
    "energia": "\nContexto: notícia provável de PROJETO DE ENERGIA (geração/transmissão/distribuição). Priorize: capex_brl, capacidade em MW se mencionado na descricao_curta, uf+municipio, setor='energia' explícito.",
    "mineracao": "\nContexto: notícia provável de MINERAÇÃO/EXPLORAÇÃO. Priorize: capex_brl, mineral explorado na descricao_curta, uf+municipio, fase de licenciamento se mencionado.",
    "multi_tipo": "\nContexto: notícia matchou múltiplos tipos. Mantenha extração genérica mas seja conservador em confianca.",
    None: "",
}


def carregar_config():
    with open(YAML_PATH) as f:
        return yaml.safe_load(f)


def filtro_keyword(text, keywords):
    """Match insensitive contra keywords; * vira regex .*"""
    text = (text or "").lower()
    for kw in keywords:
        pat = re.escape(kw.lower()).replace(r"\*", ".*")
        if re.search(pat, text):
            return True
    return False


def match_keywords_tipo(text, kw_globais, kw_por_tipo):
    """Retorna (matched_global, tipo_provavel).

    matched_global: True se ao menos 1 keyword global bateu (gate pre-Haiku).
    tipo_provavel: nome do tipo cujas keywords bateram. Se >1, 'multi_tipo'.
                   Se nenhum tipo bateu mas global sim, None (extração genérica).
    """
    if not filtro_keyword(text, kw_globais):
        return False, None
    tipos = []
    for tipo, kws in (kw_por_tipo or {}).items():
        if filtro_keyword(text, kws):
            tipos.append(tipo)
    if len(tipos) == 0:
        return True, None
    if len(tipos) == 1:
        return True, tipos[0]
    return True, "multi_tipo"


def hash_noticia(title, link):
    return hashlib.md5(f"{title or ''}|{link or ''}".encode()).hexdigest()


def ja_processada(conn, h):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM noticias_processadas WHERE hash=%s LIMIT 1", (h,))
        return cur.fetchone() is not None


def gravar_processada(conn, h, fonte, url, title, pubdate, virou_obra,
                      obra_id=None, motivo_skip=None, raw_haiku=None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO noticias_processadas
              (hash, fonte, url, title, pubdate, virou_obra, obra_id, motivo_skip, raw_haiku_response)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (hash) DO NOTHING
        """, (h, fonte, url, title, pubdate, virou_obra, obra_id, motivo_skip,
              Json(raw_haiku) if raw_haiku else None))
    conn.commit()


def extrair_via_haiku(client, title, summary, link, tipo_provavel=None):
    """Chama Haiku. Retorna dict ou None se falha. tipo_provavel guia o hint contextual."""
    hint = TIPO_HINTS.get(tipo_provavel, "")
    prompt = PROMPT_BASE.format(title=title or "", summary=summary or "", link=link or "", tipo_hint=hint)
    if USAR_LLM_GRATIS:
        if _extrair_json_gratis is None:
            log.warning("LLM gratis indisponivel (import falhou)")
            return None, 0, 0
        return _extrair_json_gratis(prompt, max_tokens=700), 0, 0
    try:
        msg = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.warning(f"Haiku falhou: {e}")
        return None, 0, 0
    text = msg.content[0].text if msg.content else ""
    # strip markdown fences se houver
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        data = json.loads(text)
    except Exception as e:
        log.warning(f"JSON parse falhou: {e} | text={text[:120]}")
        return None, msg.usage.input_tokens, msg.usage.output_tokens
    return data, msg.usage.input_tokens, msg.usage.output_tokens


def validar_cnpj(cnpj):
    """Via services.brasilapi.consultar_cnpj_com_erro (cache local)."""
    if not cnpj:
        return None
    cnpj_clean = re.sub(r"\D", "", cnpj)
    if len(cnpj_clean) != 14:
        return None
    try:
        from services.brasilapi import consultar_cnpj_com_erro
        dados, erro = consultar_cnpj_com_erro(cnpj_clean)
        if erro or not dados:
            return None
        return cnpj_clean
    except Exception as e:
        log.debug(f"BrasilAPI lookup erro: {e}")
        return None


def parse_sitemap_g1(sitemap_index_url, max_entries=30, path_filter=None,
                    horas_lookback=24):
    """Parse G1 sitemap-index → daily sitemap → URLs filtradas → scrape title+og:desc.

    sitemap_index_url: ex https://g1.globo.com/sitemap/g1/sitemap.xml
    path_filter: substring que URL precisa conter (ex 'economia'). None = qualquer.
    horas_lookback: só URLs com lastmod nas últimas N horas.

    Retorna lista de dicts compatíveis com entries do feedparser:
      [{"title", "summary", "link", "published_parsed"}, ...]
    """
    import requests
    from xml.etree import ElementTree as ET
    from datetime import datetime, timedelta, timezone

    headers = {"User-Agent": "Mozilla/5.0 (Linux x86_64) Chrome/147 Safari/537.36"}
    try:
        r = requests.get(sitemap_index_url, headers=headers, timeout=10)
        root = ET.fromstring(r.text)
    except Exception as e:
        log.error(f"sitemap-index fetch falhou: {e}")
        return []

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    # pega o sitemap mais recente (1º)
    daily = None
    for sm in root.findall("sm:sitemap", ns):
        loc = sm.find("sm:loc", ns)
        if loc is not None:
            daily = loc.text
            break
    if not daily:
        log.error("nenhum sitemap diario encontrado")
        return []

    try:
        r2 = requests.get(daily, headers=headers, timeout=10)
        root2 = ET.fromstring(r2.text)
    except Exception as e:
        log.error(f"sitemap diario fetch falhou: {e}")
        return []

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=horas_lookback)
    candidates = []
    for url in root2.findall("sm:url", ns):
        loc_el = url.find("sm:loc", ns)
        mod_el = url.find("sm:lastmod", ns)
        if loc_el is None:
            continue
        loc = loc_el.text or ""
        if path_filter:
            # Aceita string OU lista de prefixos. Match insensitive.
            filters = path_filter if isinstance(path_filter, list) else [path_filter]
            if not any(f.lower() in loc.lower() for f in filters):
                continue
        # parse data
        pub_dt = None
        if mod_el is not None and mod_el.text:
            try:
                pub_dt = datetime.fromisoformat(mod_el.text.replace("Z", "+00:00"))
            except Exception:
                pub_dt = None
        if pub_dt and pub_dt < cutoff:
            continue
        candidates.append({"loc": loc, "pub_dt": pub_dt})
        if len(candidates) >= max_entries:
            break

    # scrape title + og:description de cada
    entries = []
    for c in candidates:
        try:
            page = requests.get(c["loc"], headers=headers, timeout=6)
            html = page.text
        except Exception:
            continue
        title_m = re.search(r"<title>([^<]+)</title>", html, re.I)
        og_m = re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', html, re.I)
        desc_m = re.search(r'<meta\s+name="description"\s+content="([^"]+)"', html, re.I)
        title = (title_m.group(1) if title_m else "").strip()
        summary = (og_m.group(1) if og_m else (desc_m.group(1) if desc_m else "")).strip()
        if not title:
            continue
        entries.append({
            "title": title,
            "summary": summary,
            "link": c["loc"],
            "published_parsed": c["pub_dt"].timetuple() if c["pub_dt"] else None,
        })
    return entries


def buscar_cnpj_por_razao(conn, razao_social):
    """Tenta achar CNPJ via tabela fornecedores (BrasilAPI cached)."""
    if not razao_social:
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cnpj FROM fornecedores
            WHERE razao_social ILIKE %s OR nome_fantasia ILIKE %s
            ORDER BY length(razao_social) ASC
            LIMIT 1
        """, (f"%{razao_social}%", f"%{razao_social}%"))
        row = cur.fetchone()
        return row[0] if row else None


def duplicada_obra(conn, cnpj, uf, capex):
    """Match CNPJ + UF + capex entre 0.7x e 1.3x."""
    if not cnpj or not uf or not capex:
        return None
    capex_lo = float(capex) * 0.7
    capex_hi = float(capex) * 1.3
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM obras
            WHERE cnpj = %s AND uf = %s
              AND valor_estimado BETWEEN %s AND %s
            LIMIT 1
        """, (cnpj, uf, capex_lo, capex_hi))
        row = cur.fetchone()
        return str(row[0]) if row else None


def inserir_obra(conn, fonte, data_extraida, url, pubdate):
    """INSERT obra a partir do dict extraído. Retorna obra_id."""
    nome = data_extraida.get("descricao_curta") or data_extraida.get("empresa_nome") or "Obra anunciada via notícia"
    nome = nome[:255]
    cnpj = data_extraida.get("cnpj_validado")
    empresa = data_extraida.get("empresa_nome", "")[:255]
    uf = (data_extraida.get("uf") or "")[:2]
    municipio = data_extraida.get("municipio")
    capex = data_extraida.get("capex_brl")
    setor = _normalizar_setor(data_extraida.get("setor"))
    cnae = data_extraida.get("cnae_provavel")
    descricao = data_extraida.get("descricao_curta", "")
    confianca = float(data_extraida.get("confianca", 0.0))

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO obras (
                nome, empresa, cnpj, uf, municipio, setor,
                valor_estimado, fase, fonte, fonte_tipo,
                url_fonte, status, data_anuncio, confianca_extracao,
                descricao, descricao_sintetica
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, 'LICITACAO_ABERTA', %s, 'NOTICIA',
                %s, 'anunciado', %s, %s,
                %s, false
            )
            RETURNING id
        """, (nome, empresa, cnpj, uf, municipio, setor,
              capex, f"noticia_{fonte}",
              url, pubdate.date() if pubdate else None, confianca,
              descricao))
        obra_id = cur.fetchone()[0]
    conn.commit()
    return str(obra_id)


def processar_fonte(conn, client, fonte_cfg, keywords, kw_por_tipo, dry=False):
    nome = fonte_cfg["nome"]
    rss = fonte_cfg["rss"]
    log.info(f"--- {nome} | {rss} ---")
    stats = {"entries": 0, "match_kw": 0, "ja_processada": 0,
             "haiku_call": 0, "extracted_ok": 0, "haiku_skip": 0,
             "inseridas": 0, "duplicadas_obra": 0, "falhas": 0,
             "tokens_in": 0, "tokens_out": 0}

    tipo_fonte = fonte_cfg.get("tipo", "rss")

    if tipo_fonte == "rss_flaresolverr":
        # B3 — RSS via FlareSolverr (bypass CF). Body vem como HTML <pre>HTML-encoded XML</pre>.
        try:
            from fetch_via_flaresolverr import fetch_via_flaresolverr
            import html as _html_lib
            body = fetch_via_flaresolverr(rss, max_timeout_ms=60000, retries=1)
            if not body:
                log.error(f"FlareSolverr não retornou body pra {nome}")
                return stats
            # Extrai conteúdo de <pre> (Chromium renderiza XML assim)
            m_pre = re.search(r"<pre[^>]*>(.*?)</pre>", body, re.S | re.I)
            xml_text = m_pre.group(1) if m_pre else body
            xml_text = _html_lib.unescape(xml_text)
            feed = feedparser.parse(xml_text)
            stats["entries"] = len(feed.entries or [])
            log.info(f"  entries (rss_flaresolverr): {stats['entries']}")
        except Exception as e:
            log.error(f"rss_flaresolverr {nome}: {e}")
            return stats
    elif tipo_fonte == "sitemap":
        # B4.2 — Sitemap (G1, etc) — scraping HTML
        path_filter = fonte_cfg.get("path_filter")
        max_entries = fonte_cfg.get("max_entries", 30)
        horas_lookback = fonte_cfg.get("horas_lookback", 24)
        try:
            sm_entries = parse_sitemap_g1(rss, max_entries=max_entries,
                                          path_filter=path_filter,
                                          horas_lookback=horas_lookback)
        except Exception as e:
            log.error(f"sitemap fetch {nome}: {e}")
            sm_entries = []
        # Adapta pra interface feedparser: feed.entries
        class _Feed:
            entries = []
            status = 200
            bozo = False
            def get(self, k, default=None):
                return getattr(self, k, default)
        feed = _Feed()
        # Constroi entry-like com método .get() e .items()
        class _Entry(dict):
            def get(self, k, default=None):
                return dict.get(self, k, default)
        feed.entries = [_Entry(e) for e in sm_entries]
        stats["entries"] = len(feed.entries)
        log.info(f"  entries (sitemap): {stats['entries']}")
    else:
        try:
            feed = feedparser.parse(rss)
        except Exception as e:
            log.error(f"feedparser falhou pra {nome}: {e}")
            return stats

        # Fallback Playwright pra CF challenge ou 403 (B3.2)
        if (getattr(feed, "bozo", False) and feed.get("status") in (403, 503)) or not (feed.entries or []):
            if feed.get("status") in (403, 503):
                log.info(f"  {nome}: HTTP {feed.get('status')} (provável CF). Tentando Playwright...")
            else:
                log.info(f"  {nome}: feed sem entries. Tentando Playwright fallback...")
            try:
                from fetch_rss_playwright import fetch_with_playwright
                xml_content = fetch_with_playwright(rss, timeout=15.0)
                if xml_content:
                    feed2 = feedparser.parse(xml_content)
                    if feed2.entries:
                        feed = feed2
                        log.info(f"  {nome}: Playwright recuperou {len(feed.entries)} entries.")
            except Exception as e:
                log.warning(f"  {nome}: Playwright fallback falhou: {e}")

        stats["entries"] = len(feed.entries or [])
        log.info(f"  entries: {stats['entries']}")

    for entry in (feed.entries or [])[:30]:
        title = entry.get("title") or ""
        summary = (entry.get("summary") or "")[:1500]
        link = entry.get("link") or ""
        pubdate_raw = entry.get("published_parsed") or entry.get("updated_parsed")
        try:
            pubdate = datetime(*pubdate_raw[:6]) if pubdate_raw else None
        except (TypeError, ValueError):
            pubdate = None

        full_text = f"{title} {summary}"
        matched, tipo_provavel = match_keywords_tipo(full_text, keywords, kw_por_tipo)
        if not matched:
            continue
        stats["match_kw"] += 1
        stats.setdefault("por_tipo", {}).setdefault(tipo_provavel or "_sem_tipo", 0)
        stats["por_tipo"][tipo_provavel or "_sem_tipo"] += 1

        h = hash_noticia(title, link)
        if ja_processada(conn, h):
            stats["ja_processada"] += 1
            continue

        # Haiku
        stats["haiku_call"] += 1
        data, ti, to = extrair_via_haiku(client, title, summary, link, tipo_provavel=tipo_provavel)
        stats["tokens_in"] += ti
        stats["tokens_out"] += to

        if not data:
            stats["falhas"] += 1
            if not dry:
                gravar_processada(conn, h, nome, link, title, pubdate, False,
                                  motivo_skip="haiku_parse_falhou")
            continue

        if not data.get("eh_obra_real"):
            stats["haiku_skip"] += 1
            if not dry:
                gravar_processada(conn, h, nome, link, title, pubdate, False,
                                  motivo_skip=data.get("motivo_skip") or "haiku_eh_obra_real_false",
                                  raw_haiku=data)
            log.info(f"  SKIP: {title[:60]} ({data.get('motivo_skip','no_motivo')})")
            continue

        stats["extracted_ok"] += 1

        # Normalizar prazo_inicio_operacao (Haiku às vezes retorna "1o trimestre 2027",
        # "Q3 2026", "daqui a 6 meses" etc). Persiste o ISO em data pra que
        # gravar_processada(raw_haiku=data) carregue o valor normalizado.
        prazo_raw = data.get("prazo_inicio_operacao")
        if prazo_raw:
            prazo_dt = parse_data_br(prazo_raw)
            data["prazo_inicio_operacao_parsed"] = prazo_dt.isoformat() if prazo_dt else None
            if prazo_dt is None:
                log.debug(f"parse_data_br falhou pra {prazo_raw!r}")

        # Validar/buscar CNPJ
        cnpj_validado = validar_cnpj(data.get("cnpj_provavel"))
        if not cnpj_validado:
            cnpj_validado = buscar_cnpj_por_razao(conn, data.get("empresa_nome"))
            if cnpj_validado:
                # double check via BrasilAPI
                if not validar_cnpj(cnpj_validado):
                    cnpj_validado = None
        data["cnpj_validado"] = cnpj_validado

        # Dedup obras
        dup_id = duplicada_obra(conn, cnpj_validado, data.get("uf"), data.get("capex_brl"))
        if dup_id:
            stats["duplicadas_obra"] += 1
            if not dry:
                gravar_processada(conn, h, nome, link, title, pubdate, False,
                                  motivo_skip=f"duplicada_de_obra_{dup_id}",
                                  raw_haiku=data)
            log.info(f"  DUP: {title[:60]} → obra {dup_id[:8]}")
            continue

        # INSERT obra
        if dry:
            log.info(f"  [DRY] inseriria: {title[:60]} cnpj={cnpj_validado} capex={data.get('capex_brl')}")
            continue
        # PORTÃO DE ENTRADA (Fase 2): Haiku já extraiu; portão decide se entra.
        # fail-CLOSED na decisão (não passou => não insere); fail-OPEN só em erro do portão.
        try:
            import sys as _s
            if "/app/scripts/portao" not in _s.path:
                _s.path.insert(0, "/app/scripts/portao")
            import portao as _pt
            _v = _pt.avaliar({
                "nome": data.get("descricao_curta") or data.get("empresa_nome"),
                "empresa": data.get("empresa_nome"), "cnpj": cnpj_validado,
                "setor": data.get("setor"), "valor_estimado": data.get("capex_brl"),
                "uf": data.get("uf"), "municipio": data.get("municipio"),
                "descricao": data.get("descricao_curta"),
                "haiku_extraiu": {"cnpj": cnpj_validado, "valor": data.get("capex_brl"),
                                  "setor": data.get("setor"), "empresa": data.get("empresa_nome")},
            }, {"fonte": nome, "fonte_tipo": "NOTICIA"}, conn)
        except Exception as _e:
            _v = None
            log.warning(f"  [PORTAO] erro (fail-open, insere): {_e!r}")
        if _v is not None and not _v["passou"]:
            stats["portao_descartou"] = stats.get("portao_descartou", 0) + 1
            gravar_processada(conn, h, nome, link, title, pubdate, False,
                              motivo_skip=("portao:" + (_v["motivo"] or "?"))[:200], raw_haiku=data)
            log.info(f"  PORTAO DESCARTA ({_v['motivo']}): {title[:50]}")
            continue
        try:
            obra_id = inserir_obra(conn, nome, data, link, pubdate)
            gravar_processada(conn, h, nome, link, title, pubdate, True,
                              obra_id=obra_id, raw_haiku=data)
            stats["inseridas"] += 1
            log.info(f"  ✓ obra inserida: {obra_id[:8]} | {title[:60]}")
        except Exception as e:
            stats["falhas"] += 1
            log.error(f"INSERT obra falhou: {e}")
            try:
                gravar_processada(conn, h, nome, link, title, pubdate, False,
                                  motivo_skip=f"insert_falhou: {e!r}"[:200],
                                  raw_haiku=data)
            except Exception:
                pass

    log.info(f"  [{nome}] stats: {stats}")
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fonte", help="processar só uma fonte por nome")
    parser.add_argument("--dry", action="store_true", help="não persiste em obras nem noticias_processadas")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info(f"CAPTAR NOTÍCIAS — start {datetime.utcnow().isoformat()}  mode={'DRY' if args.dry else 'COMMIT'}")
    log.info(f"Log: {LOG_FILE}")
    log.info("=" * 60)

    cfg = carregar_config()
    fontes = cfg.get("fontes", [])
    # Suporta keywords_globais (novo nome) ou keywords_capex (compat retroativa).
    keywords = cfg.get("keywords_globais") or cfg.get("keywords_capex", [])
    kw_por_tipo = cfg.get("keywords_por_tipo_obra", {})

    if args.fonte:
        fontes = [f for f in fontes if f.get("nome") == args.fonte]
        if not fontes:
            log.error(f"fonte '{args.fonte}' não encontrada")
            sys.exit(2)

    log.info(f"Fontes: {[f['nome'] for f in fontes]}  Keywords: {len(keywords)}")

    try:
        from anthropic import Anthropic
        client = Anthropic()
    except Exception as e:
        log.error(f"Anthropic init falhou: {e}")
        sys.exit(3)

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    total_stats = {}
    try:
        for fonte_cfg in fontes:
            stats = processar_fonte(conn, client, fonte_cfg, keywords, kw_por_tipo, dry=args.dry)
            total_stats[fonte_cfg["nome"]] = stats
    finally:
        conn.close()

    log.info("=" * 60)
    total_in = sum(s.get("tokens_in", 0) for s in total_stats.values())
    total_out = sum(s.get("tokens_out", 0) for s in total_stats.values())
    custo = total_in * 1e-6 + total_out * 5e-6  # Haiku 4.5 ~$1/MTok in + $5/MTok out
    _STATS["buscados"] = sum(int(s.get("entries", 0) or 0) for s in total_stats.values())
    _STATS["novos"] = sum(int(s.get("inseridas", 0) or 0) for s in total_stats.values())
    _STATS["erros"] = sum(int(s.get("falhas", 0) or 0) for s in total_stats.values())
    log.info(f"FIM — fontes processadas: {len(total_stats)}")
    log.info(f"Tokens Haiku: in={total_in}, out={total_out}  custo estimado: ${custo:.4f}")
    log.info(f"Stats por fonte: {json.dumps(total_stats, default=str)}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
