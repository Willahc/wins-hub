#!/usr/bin/env python3
"""Captador DOE/DOM multi-backend.

Backends:
  - querido_diario: Querido-Diario API (OKBR, municipalidades).
  - requests_html:  HTTP simples + trafilatura (estados sem anti-bot pesado).
  - playwright_pdf: Playwright + pdfplumber (estados que servem PDF diario).

CLI:
  python captar_doe.py --uf <id> [--dias-back 7] [--limite-edicoes 5]
  Sem --uf: usa state_codes do YAML (legacy querido-diario flow).

Configuracao em app/scripts/doe_config.yaml:
  estados:
    - id: rj
      nome: "..."
      url: "..."
      backend: playwright_pdf
      keywords: [...]

Cada UF tem id_externo = "DOE-<UF>:<sha1(url+identifica)[:16]>", fonte=doe_<uf>,
escopo='regional'. Idempotente via ON CONFLICT.

STATS_JSON na ultima linha.
"""
from __future__ import annotations

import argparse
import sys as _scompat
if "/app" not in _scompat.path: _scompat.path.insert(0, "/app")
from services.llm_haiku_compat import _haiku_client, _haiku_async_client  # free-first 25/06
import atexit
import hashlib
import io
import json as _json
import logging
import os
import re
import sys
import traceback as _tb
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

import psycopg2
import requests
import yaml
from brutils import is_valid_cnpj


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_doe")

API_QD = "https://api.queridodiario.ok.org.br/gazettes"
KEYWORDS_BUSCA_QD = ("obra", "licenca", "construc", "edital", "pavimentacao", "outorga")
DEFAULT_DIAS_BACK = 7
DEFAULT_LIMITE_EDICOES = 5
DEFAULT_SIZE_POR_KW = 50
DEFAULT_CONFIG_YAML = "/app/scripts/doe_config.yaml"

# Keywords default pra filtro local (estados podem override no YAML)
KEYWORDS_OBRA_DEFAULT = (
    r"\bobra\b", r"construc(?:ao|oes)\s+de", r"reform(?:a|as)\s+(?:de|do|da)",
    r"amplia(?:cao|coes)", r"implanta(?:cao|coes)", r"edifica(?:cao|coes)",
    r"licenc(?:a|as)\s+(?:ambiental|de\s+instalac|de\s+operac|previa)",
    r"investiment[oa]", r"capex",
    r"r\$\s*\d[\d\.\,\s]*\s*(?:milh|bilh)",
    r"pavimenta(?:cao|coes)", r"subesta(?:cao|coes)", r"linha\s+de\s+transmiss",
    r"rodovia", r"ferrovia", r"porto", r"aeroporto",
    r"hospital", r"escola", r"creche",
    r"outorga", r"concessao",
    r"adutora", r"esgoto", r"saneamento", r"epc\b",
)
DEFAULT_KEYWORDS_RE = re.compile("|".join(KEYWORDS_OBRA_DEFAULT), re.IGNORECASE)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
PROMPT_DOE = """Voce analisa publicacoes de Diarios Oficiais brasileiros (DOM/DOE).
Extraia APENAS se a publicacao anuncia OBRA/INVESTIMENTO REAL (nao retificacao,
nao prorrogacao de prazo sem CAPEX, nao decreto puramente formal).

UF/CONTEXTO: {uf}
TITULO: {titulo}
TEXTO ({n_chars} chars):
{texto}

Retorne JSON puro (sem markdown), schema:
{{
  "eh_obra_real": bool,
  "motivo_skip": "string ou null",
  "empresa_nome": "razao social ou orgao licitante",
  "cnpj_provavel": "14 digitos ou null",
  "capex_brl": "numero puro. 'R$ 2 mi' -> 2000000",
  "uf": "sigla 2 letras",
  "municipio": "string ou null",
  "setor": "INDUSTRIAL/ENERGIA/LOGISTICO/MINERACAO/INFRAESTRUTURA/SANEAMENTO/AGRO/DATA_CENTER/OUTRO",
  "tipo_publicacao": "EDITAL/ORDEM_INICIO/CONTRATO/LICENCA/DECRETO/OUTRO",
  "descricao_curta": "1 frase ate 200 chars",
  "confianca": "0.0-1.0"
}}

Se confianca < 0.6 marque eh_obra_real=false."""

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

_STATS = {
    "buscados": 0,
    "novos": 0,
    "erros": 0,
    "fetched_text": 0,
    "filtrados_keyword": 0,
    "credito_skip": 0,
    "extracoes_haiku": 0,
    "haiku_skip": 0,
}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


# =============================================================================
# Backends
# =============================================================================


class BackendBase(ABC):
    """Interface comum pros 3 backends."""

    def __init__(self, uf_cfg: Dict[str, Any]):
        self.uf_cfg = uf_cfg
        self.uf = uf_cfg.get("id", "??").upper()
        self.nome = uf_cfg.get("nome", "?")
        self.url_base = uf_cfg.get("url", "")
        kws = uf_cfg.get("keywords") or []
        if kws:
            self.keywords_re = re.compile("|".join(re.escape(k) for k in kws), re.IGNORECASE)
        else:
            self.keywords_re = DEFAULT_KEYWORDS_RE

    @abstractmethod
    def listar_edicoes_dia(self, data: date, limite: int = 5) -> List[Dict[str, Any]]:
        """Retorna lista de [{url, titulo, identifica}, ...] pra data dada."""
        ...

    @abstractmethod
    def baixar_conteudo(self, edicao: Dict[str, Any]) -> str:
        """Baixa e extrai texto da edicao. '' se falhar."""
        ...


class BackendRequestsHTML(BackendBase):
    """HTTP simples + trafilatura. Estados sem anti-bot pesado."""

    def listar_edicoes_dia(self, data: date, limite: int = 5) -> List[Dict[str, Any]]:
        try:
            import trafilatura  # noqa: F401
        except ImportError:
            log.error("trafilatura nao instalado")
            return []
        try:
            r = requests.get(self.url_base, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120 Safari/537.36"})
            r.raise_for_status()
        except Exception as e:
            log.warning(f"[{self.uf}] requests falhou {self.url_base}: {e}")
            return []
        # Heuristica: capturar links com texto que sugere materia/edicao
        hrefs = re.findall(r'<a\s+[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', r.text, flags=re.IGNORECASE)
        edicoes: List[Dict[str, Any]] = []
        from urllib.parse import urljoin
        for url, txt in hrefs:
            t = (txt or "").strip()
            if not t or len(t) < 10:
                continue
            if self.keywords_re.search(t):
                full = urljoin(self.url_base, url)
                edicoes.append({"url": full, "titulo": t[:200], "identifica": ""})
                if len(edicoes) >= limite * 3:  # buffer pre-filter
                    break
        return edicoes[:limite]

    def baixar_conteudo(self, edicao: Dict[str, Any]) -> str:
        import trafilatura
        url = edicao.get("url", "")
        if not url:
            return ""
        try:
            r = requests.get(url, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120 Safari/537.36"})
            r.raise_for_status()
        except Exception as e:
            log.debug(f"[{self.uf}] download {url}: {e}")
            return ""
        # Detecta PDF (extensao OU magic bytes) — DOEs estaduais frequentemente
        # listam links .pdf direto na landing. Trafilatura nao processa PDF.
        if url.lower().endswith(".pdf") or r.content[:4] == b"%PDF":
            try:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                    return "\n".join(p.extract_text() or "" for p in pdf.pages[:50])
            except Exception as e:
                log.debug(f"[{self.uf}] pdf parse {url}: {e}")
                return ""
        try:
            return trafilatura.extract(r.text) or ""
        except Exception:
            return ""


class BackendPlaywrightPDF(BackendBase):
    """Playwright + pdfplumber. Estados que servem PDF via SPA."""

    def listar_edicoes_dia(self, data: date, limite: int = 5) -> List[Dict[str, Any]]:
        from playwright.sync_api import sync_playwright
        edicoes: List[Dict[str, Any]] = []
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120 Safari/537.36"))
            page = ctx.new_page()
            try:
                page.goto(self.url_base, timeout=30000, wait_until="domcontentloaded")
                # Aguardar settle (IOERJ pode auto-navegar). Tolerar timeout.
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                # heuristica: pegar links de PDF + links de "diario"/"edicao"
                items = page.eval_on_selector_all(
                    "a",
                    "els => els.map(e => ({h: e.href, t: (e.innerText||'').trim().slice(0,150)}))"
                    ".filter(o => /\\.pdf$/i.test(o.h) || /(di[áa]rio|edi[çc][ãa]o|jornal)/i.test(o.t))"
                )
                for it in items[: limite * 3]:
                    if not it.get("h"):
                        continue
                    edicoes.append({
                        "url": it["h"],
                        "titulo": (it.get("t") or "")[:200],
                        "identifica": "",
                    })
                    if len(edicoes) >= limite:
                        break
            except Exception as e:
                log.warning(f"[{self.uf}] playwright {self.url_base}: {e}")
            finally:
                b.close()
        return edicoes

    def baixar_conteudo(self, edicao: Dict[str, Any]) -> str:
        url = edicao.get("url", "")
        if not url:
            return ""
        try:
            r = requests.get(url, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
        except Exception as e:
            log.debug(f"[{self.uf}] pdf dl {url}: {e}")
            return ""
        if url.lower().endswith(".pdf") or r.content[:4] == b"%PDF":
            try:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                    return "\n".join(p.extract_text() or "" for p in pdf.pages[:30])  # cap 30 pgs
            except Exception as e:
                log.debug(f"[{self.uf}] pdf parse: {e}")
                return ""
        # fallback: tratar como HTML
        try:
            import trafilatura
            return trafilatura.extract(r.text) or ""
        except Exception:
            return ""


class BackendQueridoDiario(BackendBase):
    """Querido-Diario API. Cobertura municipal pra fallback."""

    def listar_edicoes_dia(self, data: date, limite: int = 5) -> List[Dict[str, Any]]:
        sess = requests.Session()
        gazettes: Dict[str, Dict[str, Any]] = {}
        # state_code filter: derived de uf_cfg.id (uf code, mas API usa state_code estado)
        state_filter = (self.uf or "").upper()
        for kw in KEYWORDS_BUSCA_QD:
            try:
                r = sess.get(API_QD, params={"querystring": kw, "size": DEFAULT_SIZE_POR_KW},
                             timeout=30, headers={"User-Agent": "WiNS-Hub captar_doe/2.0"})
                r.raise_for_status()
            except Exception as e:
                log.warning(f"[{self.uf}] QD fetch {kw!r}: {e}")
                continue
            for g in r.json().get("gazettes", []):
                if g.get("state_code", "").upper() != state_filter:
                    continue
                try:
                    d = datetime.strptime(g.get("date", ""), "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                key = g.get("url", "")
                if key in gazettes:
                    continue
                gazettes[key] = {
                    "url": key,
                    "titulo": g.get("territory_name", ""),
                    "identifica": f"{g.get('territory_name')} {g.get('date')}",
                    "_meta": g,
                }
                if len(gazettes) >= limite:
                    break
            if len(gazettes) >= limite:
                break
        return list(gazettes.values())[:limite]

    def baixar_conteudo(self, edicao: Dict[str, Any]) -> str:
        meta = edicao.get("_meta") or {}
        sess = requests.Session()
        # prefer txt_url (pre-extraido por OKBR)
        for url_field in ("txt_url", "url"):
            url = meta.get(url_field)
            if not url:
                continue
            try:
                r = sess.get(url, timeout=60, headers={"User-Agent": "WiNS-Hub"})
                if r.status_code == 200 and r.content:
                    if url_field == "txt_url":
                        return r.text
                    # PDF
                    if r.content[:4] == b"%PDF":
                        try:
                            import pdfplumber
                            with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                                return "\n".join(p.extract_text() or "" for p in pdf.pages[:30])
                        except Exception:
                            pass
            except Exception:
                continue
        return ""


class BackendDirectPDF(BackendBase):
    """Baixa PDF diretamente via URL template com placeholders {yyyy}/{mm}/{dd}.
    Para portais com URL previsivel por data (ex: ioepa.com.br PA).
    Config YAML: 'url' contem o template (sem url_busca extra).
    """

    def listar_edicoes_dia(self, data: date, limite: int = 5) -> List[Dict[str, Any]]:
        template = self.url_base
        url = (template
               .replace("{yyyy}", str(data.year))
               .replace("{mm}", f"{data.month:02d}")
               .replace("{dd}", f"{data.day:02d}"))
        # HEAD pra detectar se existe edicao (sabados/domingos retornam 404)
        try:
            r = requests.head(url, timeout=15, allow_redirects=True)
            if r.status_code != 200:
                log.debug(f"[{self.uf}] direct_pdf {data}: HTTP {r.status_code}")
                return []
        except Exception as e:
            log.debug(f"[{self.uf}] direct_pdf {data}: {e}")
            return []
        return [{"url": url, "titulo": f"DOE {self.uf.upper()} {data.isoformat()}",
                 "identifica": data.isoformat()}]

    def baixar_conteudo(self, edicao: Dict[str, Any]) -> str:
        url = edicao.get("url", "")
        if not url:
            return ""
        try:
            r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
        except Exception as e:
            log.debug(f"[{self.uf}] direct_pdf dl {url}: {e}")
            return ""
        if r.content[:4] != b"%PDF":
            return ""
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                # cap 50 paginas pra PDFs gigantes (PA tem ~600pg)
                return "\n".join(p.extract_text() or "" for p in pdf.pages[:50])
        except Exception as e:
            log.debug(f"[{self.uf}] direct_pdf parse {url}: {e}")
            return ""


BACKENDS = {
    "requests_html": BackendRequestsHTML,
    "playwright_pdf": BackendPlaywrightPDF,
    "querido_diario": BackendQueridoDiario,
    "direct_pdf": BackendDirectPDF,
}


# =============================================================================
# Haiku + INSERT
# =============================================================================


def haiku_extrair(client, uf: str, titulo: str, texto: str) -> Optional[Dict[str, Any]]:
    prompt = PROMPT_DOE.format(uf=uf, titulo=titulo[:200], n_chars=len(texto), texto=texto[:5000])
    try:
        r = client.messages.create(
            model=HAIKU_MODEL, max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.warning(f"haiku: {e}")
        return None
    raw = (r.content[0].text if r.content else "").strip()
    raw = re.sub(r"^```(?:json)?", "", raw)
    raw = re.sub(r"```\s*$", "", raw).strip()
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError:
        return None
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _normalizar_setor(s: Optional[str]) -> str:
    if not s:
        return "OUTRO"
    s = s.strip().upper()
    canon = {"INDUSTRIAL", "ENERGIA", "LOGISTICO", "MINERACAO", "INFRAESTRUTURA",
             "SANEAMENTO", "AGRO", "DATA_CENTER", "OUTRO"}
    if s in canon:
        return s
    aliases = {"INDUSTRIA": "INDUSTRIAL", "LOGISTICA": "LOGISTICO",
               "TECNOLOGIA": "DATA_CENTER", "GOVERNO": "OUTRO",
               "CONSTRUCAO": "INFRAESTRUTURA"}
    return aliases.get(s, "OUTRO")


def _id_ext(uf: str, edicao: Dict[str, Any]) -> str:
    seed = f"{edicao.get('url', '')}|{edicao.get('identifica', '')}"
    h = hashlib.sha1(seed.encode()).hexdigest()[:16]
    return f"DOE-{uf.upper()}:{h}"


# Marcadores de secao em DOEs brasileiros — quebra texto em "atos" individuais
# antes de mandar pro Haiku, evitando truncamento em PDFs gigantes (PA = 600pg).
_SECAO_RE = re.compile(
    r"\n(?=(?:ATO|PORTARIA|EDITAL|CONTRATO|EXTRATO|RESOLU[CÇ][AÃ]O|DECRETO|"
    r"AVISO|DESPACHO|ORDEM\s+DE\s+SERVI[CÇ]O|TERMO\s+DE)\b)",
    re.IGNORECASE,
)
_CHUNK_MIN = 150       # ignora chunks menores que isso (cabecalho, ruido)
_CHUNK_MAX_PER_EDICAO = 80   # cap p/ evitar gastar Haiku em DOE de 600pg


def chunkar_secoes(texto: str) -> List[str]:
    """Divide DOE em secoes por marcadores. Pre-condicao p/ Haiku targeted."""
    if not texto:
        return []
    chunks = _SECAO_RE.split(texto)
    # Filtra ruido (cabecalho/sumario/indice)
    return [c.strip() for c in chunks if c and len(c.strip()) >= _CHUNK_MIN]


# Padroes de "operacao de credito" — autorizacoes financeiras do Estado SEM
# executor privado. Identificadas via falso-positivo PA 03/06 (Programa de
# Investimentos R$575mi inserido como obra; e' so autorizacao legislativa).
_CREDITO_PATTERNS_RE = re.compile(
    r"(?:"
    r"autoriza.{0,300}?contratar.{0,300}?opera[cç][aã]o\s+de\s+cr[ée]dito"
    r"|opera[cç][aã]o\s+de\s+cr[ée]dito\s+interno"
    r"|lei.{0,300}?autoriza.{0,600}?poder\s+executivo.{0,600}?cr[ée]dito"
    r"|decreto.{0,300}?abertura\s+de\s+cr[ée]dito"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def chunk_tem_executor(chunk: str) -> bool:
    """False se chunk e' autorizacao de operacao de credito (sem executor privado).
    True caso contrario — Haiku decide se e' obra real. Evita gastar Haiku em atos
    legislativos/financeiros do Estado sem contraparte privada."""
    if not chunk:
        return False
    return not _CREDITO_PATTERNS_RE.search(chunk)


def inserir_obra(conn, uf: str, edicao: Dict[str, Any], dados: Dict[str, Any]) -> Optional[str]:
    cnpj_raw = (dados.get("cnpj_provavel") or "").strip()
    cnpj_clean = re.sub(r"\D", "", cnpj_raw)
    cnpj_valido = is_valid_cnpj(cnpj_clean) if len(cnpj_clean) == 14 else False

    state = (dados.get("uf") or uf or "")[:2].upper() or None
    nome = (dados.get("descricao_curta") or edicao.get("titulo") or "Publicacao DOE")[:200]
    empresa = (dados.get("empresa_nome") or "")[:255]
    municipio = dados.get("municipio")
    capex = dados.get("capex_brl")
    if isinstance(capex, str):
        try:
            capex = float(re.sub(r"[^\d\.]", "", capex.replace(",", ".")))
        except ValueError:
            capex = None
    setor = _normalizar_setor(dados.get("setor"))
    confianca = float(dados.get("confianca") or 0.6)

    fonte_uf = f"doe_{uf.lower()}"
    id_ext = _id_ext(uf, edicao)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO obras (
                nome, empresa, cnpj, uf, municipio, setor,
                valor_estimado, fase, fonte, fonte_tipo,
                url_fonte, status, data_anuncio, confianca_extracao,
                descricao, descricao_sintetica, id_externo,
                validacao_obra_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, 'LICITACAO_ABERTA', %s, 'OFICIAL',
                %s, 'anunciado', %s, %s,
                %s, false, %s,
                NOW()
            )
            ON CONFLICT (id_externo) DO NOTHING
            RETURNING id
        """, (
            nome, empresa, cnpj_clean if cnpj_valido else None,
            state, municipio, setor,
            capex, fonte_uf,
            edicao.get("url") or "", date.today(), confianca,
            (edicao.get("titulo") or "")[:1000], id_ext,
        ))
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else None


# =============================================================================
# Main
# =============================================================================


def carregar_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def processar_uf(uf_cfg: Dict[str, Any], dias_back: int, limite_edicoes: int) -> None:
    uf_id = uf_cfg.get("id", "??").lower()
    backend_name = uf_cfg.get("backend", "querido_diario")
    backend_cls = BACKENDS.get(backend_name)
    if not backend_cls:
        log.error(f"[{uf_id}] backend desconhecido: {backend_name}")
        return
    backend = backend_cls(uf_cfg)
    hoje = date.today()
    edicoes_total: List[Dict[str, Any]] = []
    for d_offset in range(dias_back):
        d = hoje - timedelta(days=d_offset)
        try:
            eds = backend.listar_edicoes_dia(d, limite=limite_edicoes)
        except Exception as e:
            log.warning(f"[{uf_id}] listar {d}: {e}")
            continue
        edicoes_total.extend(eds)
        if len(edicoes_total) >= limite_edicoes:
            break
    # dedup
    seen = set()
    edicoes = []
    for e in edicoes_total:
        k = e.get("url") or e.get("identifica")
        if k in seen:
            continue
        seen.add(k)
        edicoes.append(e)
    edicoes = edicoes[: limite_edicoes]
    log.info(f"[{uf_id}] backend={backend_name} edicoes coletadas: {len(edicoes)}")
    if not edicoes:
        return

    from anthropic import Anthropic
    client = _haiku_client(api_key=os.getenv("ANTHROPIC_API_KEY"))
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        for e in edicoes:
            _STATS["buscados"] += 1
            texto = backend.baixar_conteudo(e)
            if not texto:
                continue
            _STATS["fetched_text"] += 1
            # Chunk por secao (ATO/PORTARIA/EDITAL/CONTRATO/...) — uma chamada
            # Haiku por chunk relevante, em vez de 1 chamada com texto[:5000].
            secoes = chunkar_secoes(texto)
            if not secoes:
                # Sem marcadores → fallback edicao inteira (preserva comportamento)
                secoes = [texto]
            chunks_kw = [c for c in secoes if backend.keywords_re.search(c)]
            chunks_kw = chunks_kw[:_CHUNK_MAX_PER_EDICAO]
            if not chunks_kw:
                continue
            _STATS["filtrados_keyword"] += 1
            log.info(f"[{uf_id}] edicao {e.get('identifica','')}: {len(secoes)} secoes → {len(chunks_kw)} com keyword")
            for idx, chunk in enumerate(chunks_kw):
                # Pre-filter: chunks de operacao de credito (sem executor privado)
                # sao descartados sem chamar Haiku — evita falso-positivo R$575mi PA.
                if not chunk_tem_executor(chunk):
                    _STATS["credito_skip"] = _STATS.get("credito_skip", 0) + 1
                    continue
                edicao_chunk = dict(e)
                edicao_chunk["identifica"] = f"{e.get('identifica','')}#sec{idx:03d}"
                dados = haiku_extrair(
                    client, uf_cfg.get("setor_uf_hint") or uf_id.upper(),
                    e.get("titulo", ""), chunk,
                )
                _STATS["extracoes_haiku"] += 1
                if not dados or not dados.get("eh_obra_real"):
                    _STATS["haiku_skip"] += 1
                    continue
                try:
                    oid = inserir_obra(conn, uf_id, edicao_chunk, dados)
                    if oid:
                        _STATS["novos"] += 1
                except Exception as exc:
                    log.warning(f"[{uf_id}] insert: {exc}")
                    _STATS["erros"] += 1
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uf", default="", help="ID do estado no YAML (ex: rj). Vazio = legacy QD flow.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_YAML)
    parser.add_argument("--dias-back", type=int, default=DEFAULT_DIAS_BACK)
    parser.add_argument("--limite-edicoes", type=int, default=DEFAULT_LIMITE_EDICOES)
    args = parser.parse_args()

    config = carregar_config(args.config)
    estados = {e["id"]: e for e in (config.get("estados") or [])}

    if args.uf:
        if args.uf not in estados:
            log.error(f"--uf {args.uf!r} nao encontrado em estados do YAML")
            return 2
        processar_uf(estados[args.uf], args.dias_back, args.limite_edicoes)
        log.info(f"DOE-{args.uf.upper()} done — {_STATS}")
        return 0

    # Legacy: sem --uf, processar state_codes do YAML antigo (compatibilidade)
    state_codes_csv = config.get("state_codes", "")
    if not state_codes_csv:
        log.warning("Sem --uf nem state_codes — nada a fazer")
        return 0
    # Legacy comportamento: querido_diario backend, varre state_codes
    legacy_uf_cfg = {
        "id": "legacy",
        "backend": "querido_diario",
        "keywords": [],
    }
    for sc in [x.strip().lower() for x in state_codes_csv.split(",") if x.strip()]:
        legacy_uf_cfg_iter = dict(legacy_uf_cfg)
        legacy_uf_cfg_iter["id"] = sc
        processar_uf(legacy_uf_cfg_iter, args.dias_back, args.limite_edicoes)
    log.info(f"DOE legacy done — {_STATS}")
    return 0


if __name__ == "__main__":
    try:
        _rc = main() or 0
    except SystemExit:
        raise
    except BaseException as _exc:  # noqa: BLE001
        _STATS["erros"] = max(_STATS["erros"], 1)
        log.error(f"DOE UNCAUGHT {type(_exc).__name__}: {_exc}")
        log.error(f"trace:\n{_tb.format_exc()}")
        sys.exit(1)
    sys.exit(_rc)
