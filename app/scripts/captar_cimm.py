"""
Captador CIMM — Centro de Informação Metal Mecânica.
Fonte: https://www.cimm.com.br/portal/noticia/rss (RSS XML, 50 items mais recentes)

Estrategia:
- Parse RSS via xml.etree (sem feedparser)
- Filtra items com palavras-chave de OBRA/INVESTIMENTO no título+descricao
- Extrai heuristicamente: empresa (primeiro proper noun antes do verbo de ação),
  valor (regex R$ X Mi/Bi), UF (sigla de 2 letras com ou sem parênteses), município
- Setor padrão: INDUSTRIAL (CIMM cobre metal-mecânica)
- fonte_tipo='NOTICIA' — exclui de is_ouro até validação manual
- id_externo: cimm_<numero do GUID>

Exemplo de noticia tipica que vira obra:
  "New Holland investe R$ 100 milhões para nacionalizar produção em Curitiba (PR)"
  → empresa=New Holland, valor=100M, UF=PR, municipio=Curitiba, setor=INDUSTRIAL
"""
import os
import re
import sys
import logging
import unicodedata
from datetime import datetime
import xml.etree.ElementTree as ET

import requests
import psycopg2
from psycopg2.extras import execute_values

log = logging.getLogger(__name__)

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

URL_RSS = "https://www.cimm.com.br/portal/noticia/rss"
URL_BASE = "https://www.cimm.com.br/"
HTTP_TIMEOUT = 30
USER_AGENT = "WiNS-Hub/1.0 (+https://winshubcomercial.com.br)"

# Palavras-chave que indicam obra/investimento real (texto normalizado)
KEYWORDS_OBRA = [
    "investe", "investimento", "investira", "investirao",
    "expansao", "expansao fabril", "expansao da", "expandir",
    "ampliacao", "amplia",
    "nova fabrica", "nova planta", "nova unidade", "novo centro",
    "construcao", "construir", "constroi",
    "implantacao", "implantar", "implanta",
    "modernizacao", "modernizar", "moderniza",
    "retrofit", "revamp",
    "inauguracao", "inauguracao da", "inaugurar", "inaugura",
    "anuncia r$", "aporte de", "capex",
    "nacionalizar producao",
]

# Blacklist — termos macro/política/financeiros que poluem o feed
BLACKLIST = [
    "mercosul", "tarifa", "imposto", "reforma tributaria",
    "balanco", "demonstracao financeira", "lucro liquido",
    "desemprego", "inflacao", "selic",
    "tendencias", "perspectivas",
    "premio", "premiacao", "homenagem",
]

# UFs brasileiras pra extração
UFS = {"AC","AL","AM","AP","BA","CE","DF","ES","GO","MA","MG","MS","MT",
       "PA","PB","PE","PI","PR","RJ","RN","RO","RR","RS","SC","SE","SP","TO"}

# Padrões de extração
RE_VALOR = re.compile(
    r"r\$\s*([\d\.,]+)\s*(milh[oõ]es|bilh[oõ]es|mi\b|bi\b)",
    re.IGNORECASE,
)
# UF entre parênteses: "(SP)", "/SP", "em São Paulo (SP)"
RE_UF_PAREN = re.compile(r"[\(\/\s]([A-Z]{2})[\)\s\.,]")
# Empresa: heurística — primeira string antes de "investe|anuncia|expandirá|inaugura"
RE_EMPRESA = re.compile(
    r"^([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç0-9 \-\.&]{2,60}?)\s+"
    r"(investe|investira|investirao|anuncia|inaugura|expande|amplia|constroi|"
    r"implanta|moderniza|nacionaliza|aporta)",
    re.IGNORECASE,
)


def _normalize(s: str) -> str:
    """lowercase + sem acentos pra matching."""
    s = unicodedata.normalize("NFKD", s or "").encode("ASCII", "ignore").decode("ASCII")
    return s.lower()


def _bate_keyword(texto_norm: str) -> bool:
    if any(b in texto_norm for b in BLACKLIST):
        return False
    return any(k in texto_norm for k in KEYWORDS_OBRA)


def _extrair_valor(texto: str) -> tuple[float, str]:
    """Retorna (valor_em_reais, formato_humano) ou (0, '')."""
    m = RE_VALOR.search(texto)
    if not m:
        return 0.0, ""
    num_str = m.group(1).replace(".", "").replace(",", ".")
    try:
        num = float(num_str)
    except ValueError:
        return 0.0, ""
    unidade = m.group(2).lower()
    if unidade.startswith("bi"):
        valor = num * 1_000_000_000
        return valor, f"R$ {num:.1f} Bi"
    else:
        valor = num * 1_000_000
        return valor, f"R$ {num:.0f} Mi"


def _extrair_uf(texto: str) -> str | None:
    for m in RE_UF_PAREN.finditer(" " + texto + " "):
        cand = m.group(1)
        if cand in UFS:
            return cand
    return None


def _extrair_empresa(titulo: str) -> str | None:
    m = RE_EMPRESA.match(titulo.strip())
    if not m:
        return None
    nome = m.group(1).strip()
    # Filtros mínimos pra não pegar lixo
    if len(nome) < 3 or nome.lower() in {"empresa", "fabrica", "industria", "grupo"}:
        return None
    return nome[:200]


def _id_externo(guid: str, link: str) -> str:
    """Extrai número/slug estável da URL pra id_externo."""
    src = guid or link or ""
    # CIMM URLs são tipo /exibir_noticia/27297-new-holland-...
    m = re.search(r"/(\d{4,})-([a-z0-9-]+)", src)
    if m:
        return f"cimm_{m.group(1)}"
    # Fallback: hash curto da URL
    import hashlib
    h = hashlib.md5(src.encode()).hexdigest()[:12]
    return f"cimm_{h}"


def parse_rss(xml_bytes: bytes) -> list[dict]:
    """Parse RSS retornando lista de items normalizados."""
    items = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        def _txt(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""

        title = _txt("title")
        link = _txt("link")
        guid = _txt("guid")
        desc = _txt("description")
        pubdate = _txt("pubDate")
        # Limpa tags HTML residuais da descricao
        desc = re.sub(r"<[^>]+>", "", desc).strip()

        items.append({
            "title": title, "link": link, "guid": guid,
            "description": desc, "pubdate": pubdate,
        })
    return items


def main(*, dry_run: bool = False) -> dict:
    log.info(f"=== INICIO CIMM {'(DRY-RUN)' if dry_run else ''} ===")
    stats = {"feed_items": 0, "match_keyword": 0, "obras_a_inserir": 0}

    try:
        r = requests.get(URL_RSS, headers={"User-Agent": USER_AGENT},
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        log.exception(f"erro fetch RSS: {e}")
        return {**stats, "erro": str(e)[:200]}

    items = parse_rss(r.content)
    stats["feed_items"] = len(items)
    log.info(f"  feed items: {len(items)}")

    obras_para_inserir = []
    for it in items:
        titulo = it["title"]
        descricao = it["description"]
        texto_norm = _normalize(titulo + " " + descricao)
        if not _bate_keyword(texto_norm):
            continue
        stats["match_keyword"] += 1

        empresa = _extrair_empresa(titulo)
        valor, valor_fmt = _extrair_valor(titulo + " " + descricao)
        uf = _extrair_uf(titulo + " " + descricao)
        id_ext = _id_externo(it["guid"], it["link"])

        # Necessidades default pra metal-mecânica
        necessidades = ["CIVIL_TECNICA", "ELETRICA_INDUSTRIAL", "ESTRUTURA_METALICA"]

        # Data de publicação
        data_pub = None
        try:
            from email.utils import parsedate_to_datetime
            if it["pubdate"]:
                data_pub = parsedate_to_datetime(it["pubdate"]).date().isoformat()
        except Exception:
            pass

        nome_obra = titulo[:280]
        descricao_obra = (
            f"[CIMM] {descricao[:600]} — fonte: {it['link']}"
        )

        obras_para_inserir.append((
            id_ext,                         # id_externo
            nome_obra,                      # nome
            empresa,                        # empresa
            None,                           # cnpj (notícia raramente tem)
            "INDUSTRIAL",                   # setor
            None,                           # municipio (heurística não é confiável)
            uf,                             # uf
            valor or None,                  # valor_estimado
            valor_fmt or None,              # valor_formatado
            "EM_EXECUCAO",                  # fase (notícia ≈ obra anunciada/iniciando)
            "NOTICIA",                      # status_licenca
            3,                              # urgencia (média p/ notícia sem data crítica)
            45,                             # lead_score (baixo até validação manual)
            necessidades,                   # necessidades
            descricao_obra[:1500],          # descricao
            "cimm_rss",                     # fonte
            it["link"],                     # url_fonte
            data_pub,                       # data_publicacao
            "NOTICIA",                      # fonte_tipo
        ))

    stats["obras_a_inserir"] = len(obras_para_inserir)
    log.info(f"  obras a inserir (após filtro keyword): {len(obras_para_inserir)}")
    _STATS["buscados"] = len(obras_para_inserir)

    if dry_run:
        log.info("  [DRY-RUN] nada gravado. Amostra dos 3 primeiros:")
        for o in obras_para_inserir[:3]:
            log.info(f"    {o[0]}: {o[1][:80]}  empresa={o[2]} valor={o[7]} uf={o[6]}")
        log.info("=== FIM CIMM ===")
        return stats

    if not obras_para_inserir:
        log.info("  nada a inserir")
        log.info("=== FIM CIMM ===")
        return stats

    conn = psycopg2.connect(**DB_CONFIG)
    sql = """
        INSERT INTO obras (
            id_externo, nome, empresa, cnpj, setor, municipio, uf,
            valor_estimado, valor_formatado, fase, status_licenca,
            urgencia, lead_score, necessidades, descricao, fonte, url_fonte,
            data_publicacao, fonte_tipo
        ) VALUES %s
        ON CONFLICT (id_externo) DO UPDATE SET
            nome = EXCLUDED.nome,
            empresa = COALESCE(obras.empresa, EXCLUDED.empresa),
            uf = COALESCE(obras.uf, EXCLUDED.uf),
            valor_estimado = COALESCE(obras.valor_estimado, EXCLUDED.valor_estimado),
            valor_formatado = COALESCE(obras.valor_formatado, EXCLUDED.valor_formatado),
            descricao = EXCLUDED.descricao,
            data_publicacao = COALESCE(obras.data_publicacao, EXCLUDED.data_publicacao)
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, obras_para_inserir)
        log.info(f"  UPSERT executado: {cur.rowcount} linhas afetadas")
        stats["upsert_rowcount"] = cur.rowcount
        _STATS["novos"] = cur.rowcount
    conn.commit()
    conn.close()

    log.info("=== FIM CIMM ===")
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    dry = "--dry-run" in sys.argv
    r = main(dry_run=dry)
    print(f"\nResultado: {r}")
