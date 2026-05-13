#!/usr/bin/env python3
"""Captador DOU (Diario Oficial da Uniao) via InLabs.

Login: inlabs.in.gov.br/logar.php (form-data: email, password)
       → set PHPSESSID cookie
Listagem: inlabs.in.gov.br/index.php?p=YYYY-MM-DD
Download: links sao ZIP por secao (DO1, DO2, DO3, DO1E, etc).
ZIP contem N XMLs (1 por publicacao). Cada XML tem identifica, ementa, texto.

Estrategia desta primeira versao:
  - Foco em DO3 (avisos de licitacao, contratos, ordens de inicio) e DO1 (decretos
    do executivo com destinacao de recursos a obras).
  - Filtro regex no texto.lower(): keywords ("obra", "licenca ambiental",
    "contratacao de servicos comuns de engenharia", "ordem de inicio",
    "R$ N milhoes/bilhoes", "investimento de").
  - Para XMLs que passam o filtro, chama Haiku 4.5 pra extrair JSON:
    empresa, cnpj, capex_brl, uf, setor, tipo_publicacao, descricao_curta.
  - INSERT em obras com fonte='dou' (fonte_tipo='OFICIAL'), idempotente via
    id_externo = "DOU:<identifica>".

Custo Haiku estimado: ~$0.50-2.00 por edicao diaria.

Credenciais: variaveis de ambiente INLABS_USER, INLABS_PASS (preencher .env).

STATS_JSON na ultima linha (orchestrator).
"""
from __future__ import annotations

import argparse
import atexit
import io
import json as _json
import logging
import os
import re
import sys
import zipfile
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Tuple
from xml.etree import ElementTree as ET

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

import psycopg2
import requests
from brutils import is_valid_cnpj


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_dou_inlabs")

FONTE = "dou"

INLABS_BASE = "https://inlabs.in.gov.br"
INLABS_USER = os.getenv("INLABS_USER", "")
INLABS_PASS = os.getenv("INLABS_PASS", "")

# Secoes priorizadas: DO3 (avisos licitacao) + DO1 (atos executivos)
SECOES_PRIORIZADAS = ("DO3", "DO1")

# Keywords (lower, regex). Match ANY one no texto da publicacao.
KEYWORDS_OBRA = (
    r"\bobra\b", r"obras?\s+de\s+(?:engenharia|infraestrutura|construc)",
    r"construc(?:ao|oes)\s+de", r"reform(?:a|as)\s+(?:de|do|da)",
    r"amplia(?:cao|coes)\s+(?:de|do|da)",
    r"implanta(?:cao|coes)\s+(?:de|do|da)",
    r"edifica(?:cao|coes)",
    r"licenc(?:a|as)\s+(?:ambiental|de\s+instalac|de\s+operac|previa)",
    r"ordem\s+de\s+inicio\s+(?:dos|do|de)\s+servic",
    r"contrato\s+de\s+(?:concessao|gestao|epc|empreitada)",
    r"investiment[oa]\s+(?:de\s+)?r\$",
    r"r\$\s*\d[\d\.\,\s]*\s*(?:milh|bilh)",
    r"capex\s+(?:de\s+)?r\$",
    r"epc\b", r"epcm\b",
    r"adutora", r"pavimenta(?:cao|coes)", r"subesta(?:cao|coes)",
    r"linha\s+de\s+transmiss",
    r"rodovia", r"ferrovia", r"porto", r"aeroporto", r"hospital",
    r"sanea(?:mento|dora)",
)
KEYWORDS_OBRA_RE = re.compile("|".join(KEYWORDS_OBRA), re.IGNORECASE)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

_STATS = {
    "buscados": 0,          # XMLs processados
    "novos": 0,             # obras inseridas
    "erros": 0,
    "filtrados_keyword": 0,
    "extracoes_haiku": 0,
    "haiku_skip": 0,
}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


# --- Prompt Haiku ---------------------------------------------------------

HAIKU_MODEL = "claude-haiku-4-5-20251001"

PROMPT_DOU = """Voce analisa publicacoes do Diario Oficial da Uniao (DOU).
Extraia apenas se a publicacao anuncia OBRA/INVESTIMENTO REAL no Brasil
(nao rumor, nao prorrogacao de prazo sem CAPEX, nao retificacao formal).

PUBLICACAO:
SECAO: {secao}
IDENTIFICA: {identifica}
EMENTA: {ementa}
TEXTO: {texto}

Retorne JSON puro (sem markdown), schema:
{{
  "eh_obra_real": bool,
  "motivo_skip": "string se eh_obra_real=false, senao null",
  "empresa_nome": "razao social ou orgao licitante",
  "cnpj_provavel": "se mencionado, 14 digitos, senao null",
  "capex_brl": "valor em REAIS, numero puro. Se 'R$ 2 bilhoes' -> 2000000000",
  "uf": "sigla 2 letras se mencionado",
  "municipio": "string ou null",
  "setor": "EXATAMENTE: INDUSTRIAL, ENERGIA, LOGISTICO, MINERACAO, INFRAESTRUTURA, SANEAMENTO, AGRO, DATA_CENTER, OUTRO",
  "tipo_publicacao": "EDITAL/ORDEM_INICIO/CONTRATO/LICENCA/DECRETO/OUTRO",
  "descricao_curta": "1 frase ate 200 chars",
  "confianca": "0.0-1.0"
}}

Se confianca < 0.6 marque eh_obra_real=false."""


# --- HTTP / parsing -------------------------------------------------------


def inlabs_login(sess: requests.Session) -> bool:
    if not INLABS_USER or not INLABS_PASS:
        log.error("INLABS_USER ou INLABS_PASS nao configurado em .env")
        return False
    try:
        r = sess.post(
            f"{INLABS_BASE}/logar.php",
            data={"email": INLABS_USER, "password": INLABS_PASS},
            timeout=30,
            allow_redirects=False,
        )
    except Exception as e:
        log.error(f"login HTTP erro: {e}")
        return False
    if "PHPSESSID" not in sess.cookies.get_dict():
        # Algumas vezes o cookie eh seteado mesmo em 302 — tolerar
        log.warning(f"login: cookie PHPSESSID nao encontrado (status={r.status_code})")
    if r.status_code in (200, 302) and ("logar.php" not in (r.headers.get("location") or "")):
        log.info("login InLabs OK")
        return True
    log.error(f"login falhou: status={r.status_code} location={r.headers.get('location')}")
    return False


def listar_zips_do_dia(sess: requests.Session, data: date) -> List[Tuple[str, str]]:
    """Retorna lista de (secao, url_zip) disponiveis pra data dada."""
    url = f"{INLABS_BASE}/index.php?p={data:%Y-%m-%d}"
    try:
        r = sess.get(url, timeout=30)
    except Exception as e:
        log.error(f"listar HTTP erro: {e}")
        return []
    if r.status_code != 200:
        log.error(f"listar falhou: status={r.status_code}")
        return []
    # InLabs index responde HTML com links pros ZIPs. Padrao tipico:
    # download.php?dl=YYYY-MM-DD/DO3.zip
    import html
    hrefs = re.findall(r'href="([^"]+\.zip)"', r.text, flags=re.IGNORECASE)
    resultado: List[Tuple[str, str]] = []
    for h_raw in hrefs:
        h = html.unescape(h_raw)  # decode &amp; etc
        # extrair secao do nome do arquivo
        m = re.search(r"(DO\d+E?)\.zip", h, flags=re.IGNORECASE)
        secao = m.group(1).upper() if m else "?"
        # URL absoluto
        if h.startswith("http"):
            url_full = h
        elif h.startswith("/"):
            url_full = INLABS_BASE + h
        else:
            url_full = f"{INLABS_BASE}/{h.lstrip('/')}"
        resultado.append((secao, url_full))
    return resultado


def baixar_e_iterar_xmls(sess: requests.Session, url_zip: str) -> Iterator[bytes]:
    """Yield XML bytes de cada arquivo .xml dentro do ZIP. Streaming."""
    try:
        r = sess.get(url_zip, timeout=120, stream=True)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"download {url_zip} falhou: {e}")
        return
    buf = io.BytesIO(r.content)
    try:
        with zipfile.ZipFile(buf) as zf:
            for info in zf.infolist():
                if not info.filename.lower().endswith(".xml"):
                    continue
                with zf.open(info) as fh:
                    yield fh.read()
    except zipfile.BadZipFile:
        log.warning(f"ZIP corrompido: {url_zip}")


def parse_xml_publicacao(xml_bytes: bytes) -> Optional[Dict[str, str]]:
    """Extrai campos do XML InLabs/DOU. Schema parcial."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    # Buscar campos por tag (estrutura tipica InLabs):
    #   <article ... > / <body> com children: Identifica, Titulo, Ementa, Texto
    def get_text(*candidates: str) -> str:
        for tag in candidates:
            el = root.find(f".//{tag}")
            if el is not None and (el.text or "").strip():
                return el.text.strip()
            # tentar attribute
        return ""

    identifica = get_text("Identifica", "identifica")
    titulo = get_text("Titulo", "titulo")
    ementa = get_text("Ementa", "ementa")
    texto = get_text("Texto", "texto")
    # secao via attribute
    secao = root.get("name") or root.get("secao") or root.attrib.get("name") or ""
    pubname = root.attrib.get("pubName", "")
    # Se nao achou nenhum, tenta toString agressivo
    if not (identifica or ementa or texto):
        return None
    return {
        "identifica": identifica or titulo or "(sem id)",
        "titulo": titulo,
        "ementa": ementa,
        "texto": (texto or ementa)[:8000],  # cap pra Haiku
        "secao": secao or pubname or "",
    }


def haiku_extrair(client, pub: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Chama Haiku pra extrair dados estruturados. Retorna dict ou None."""
    prompt = PROMPT_DOU.format(
        secao=pub.get("secao", ""),
        identifica=(pub.get("identifica", ""))[:300],
        ementa=(pub.get("ementa", ""))[:1500],
        texto=(pub.get("texto", ""))[:6000],
    )
    try:
        r = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.warning(f"haiku falhou: {e}")
        return None
    raw = (r.content[0].text if r.content else "").strip()
    # Strip code fence se Haiku envolver
    raw = re.sub(r"^```(?:json)?", "", raw)
    raw = re.sub(r"```\s*$", "", raw)
    raw = raw.strip()
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        log.debug(f"haiku JSON parse falhou: {raw[:200]}")
        return None


# --- Persistencia ---------------------------------------------------------


def _normalizar_setor(s: Optional[str]) -> str:
    if not s:
        return "OUTRO"
    s = s.strip().upper()
    canonicos = {"INDUSTRIAL", "ENERGIA", "LOGISTICO", "MINERACAO",
                 "INFRAESTRUTURA", "SANEAMENTO", "AGRO", "DATA_CENTER", "OUTRO"}
    if s in canonicos:
        return s
    aliases = {"INDUSTRIA": "INDUSTRIAL", "LOGISTICA": "LOGISTICO",
               "TECNOLOGIA": "DATA_CENTER", "GOVERNO": "OUTRO"}
    return aliases.get(s, "OUTRO")


def inserir_obra_dou(conn, pub: Dict[str, str], dados: Dict[str, Any]) -> Optional[str]:
    """INSERT em obras (ON CONFLICT id_externo DO NOTHING). Retorna id se novo."""
    cnpj_raw = (dados.get("cnpj_provavel") or "").strip()
    cnpj_clean = re.sub(r"\D", "", cnpj_raw)
    cnpj_valido = is_valid_cnpj(cnpj_clean) if len(cnpj_clean) == 14 else False
    cnpj_final = cnpj_clean if cnpj_valido else None

    nome = (dados.get("descricao_curta") or pub.get("identifica") or "Publicacao DOU")[:200]
    empresa = (dados.get("empresa_nome") or "")[:255]
    uf = (dados.get("uf") or "")[:2] or None
    municipio = dados.get("municipio")
    capex = dados.get("capex_brl")
    if isinstance(capex, str):
        try:
            capex = float(re.sub(r"[^\d\.]", "", capex.replace(",", ".")))
        except ValueError:
            capex = None
    setor = _normalizar_setor(dados.get("setor"))
    confianca = float(dados.get("confianca") or 0.6)
    id_ext = f"DOU:{pub.get('identifica', '')[:200]}"

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO obras (
                nome, empresa, cnpj, uf, municipio, setor,
                valor_estimado, fase, fonte, fonte_tipo,
                url_fonte, status, data_anuncio, confianca_extracao,
                descricao, descricao_sintetica, id_externo
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, 'LICITACAO_ABERTA', %s, 'OFICIAL',
                %s, 'anunciado', %s, %s,
                %s, false, %s
            )
            ON CONFLICT (id_externo) DO NOTHING
            RETURNING id
        """, (
            nome, empresa, cnpj_final, uf, municipio, setor,
            capex, FONTE,
            "", date.today(), confianca,
            (pub.get("ementa") or "")[:1000], id_ext,
        ))
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else None


# --- Main -----------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=None,
                        help="Data YYYY-MM-DD (default hoje)")
    parser.add_argument("--secoes", default=",".join(SECOES_PRIORIZADAS),
                        help="Secoes csv (default DO3,DO1)")
    parser.add_argument("--limite-xmls", type=int, default=0,
                        help="Max XMLs a processar (0 = sem limite). Util pra smoke.")
    args = parser.parse_args()

    if not INLABS_USER:
        log.error("INLABS_USER vazio. Preencha .env e reinicie container.")
        return 2

    data_alvo = (datetime.strptime(args.data, "%Y-%m-%d").date()
                 if args.data else date.today())
    secoes_alvo = tuple(s.strip().upper() for s in args.secoes.split(","))

    log.info(f"DOU InLabs — data={data_alvo} secoes={secoes_alvo} limite_xmls={args.limite_xmls}")

    sess = requests.Session()
    if not inlabs_login(sess):
        return 3

    zips = listar_zips_do_dia(sess, data_alvo)
    log.info(f"  encontrados {len(zips)} ZIPs no indice")
    zips_filtrados = [(sec, url) for (sec, url) in zips if sec in secoes_alvo]
    log.info(f"  apos filtro secao: {len(zips_filtrados)} ZIPs")

    # Lazy import anthropic — evita custo de import se nao for usar
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        for secao, url_zip in zips_filtrados:
            log.info(f"  >> {secao}: {url_zip}")
            for xml_bytes in baixar_e_iterar_xmls(sess, url_zip):
                _STATS["buscados"] += 1
                pub = parse_xml_publicacao(xml_bytes)
                if not pub:
                    continue
                texto_completo = " ".join((pub.get("ementa", ""), pub.get("texto", "")))
                if not KEYWORDS_OBRA_RE.search(texto_completo):
                    continue
                _STATS["filtrados_keyword"] += 1

                dados = haiku_extrair(client, pub)
                _STATS["extracoes_haiku"] += 1
                if not dados or not dados.get("eh_obra_real"):
                    _STATS["haiku_skip"] += 1
                    continue

                try:
                    oid = inserir_obra_dou(conn, pub, dados)
                    if oid:
                        _STATS["novos"] += 1
                except Exception as e:
                    log.warning(f"insert erro {pub.get('identifica')}: {e}")
                    _STATS["erros"] += 1
                    try:
                        conn.rollback()
                    except Exception:
                        pass

                if args.limite_xmls and _STATS["buscados"] >= args.limite_xmls:
                    log.info(f"  limite_xmls={args.limite_xmls} atingido")
                    break
            else:
                continue
            break  # quebrar zips se atingiu limite
    finally:
        conn.close()

    log.info(f"DOU done — {_STATS}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
