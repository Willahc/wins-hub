"""
Captador Agência iNFRA — portal de notícias de infraestrutura.
Fonte: WordPress REST API em https://agenciainfra.com/blog/wp-json/wp/v2/posts

Estrategia:
- Filtra posts em categorias setoriais (Energia, Transporte, Mineração, Óleo&Gás, Saneamento, Cidades)
- Janela: últimos N dias (default 90) via `modified_after` da API
- Paginação até 200 posts por execução (cap pra evitar avalanche)
- Filtro de palavras-chave no título+excerpt + extração heurística
- fonte_tipo='NOTICIA'
- id_externo: agenciainfra_<post_id>
"""
import os
import re
import sys
import logging
import unicodedata
import hashlib
from datetime import datetime, timedelta

import requests
import psycopg2
from psycopg2.extras import execute_values

log = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

URL_API = "https://agenciainfra.com/blog/wp-json/wp/v2/posts"
HTTP_TIMEOUT = 30
USER_AGENT = "WiNS-Hub/1.0 (+https://winshubcomercial.com.br)"

# IDs de categorias mapeados pra setor — descobertos via /wp-json/wp/v2/categories
CATEGORIA_SETOR = {
    10:    "ENERGIA",         # iNFRAEnergia
    13271: "ENERGIA",         # Óleo & Gás
    7:     "LOGISTICO",       # iNFRATransporte
    13239: "MINERACAO",       # Mineração
    7827:  "INFRAESTRUTURA",  # iNFRASaneamento
    18468: "INFRAESTRUTURA",  # iNFRACidades
}
CATS_FILTRO = list(CATEGORIA_SETOR.keys())

DIAS_JANELA = 90
MAX_POSTS = 200
PER_PAGE = 50

KEYWORDS_OBRA = [
    "investe", "investimento", "investira", "investirao",
    "leilao", "concessao", "concedeu",
    "expansao", "ampliacao",
    "construcao", "construir",
    "implantacao", "implantar",
    "modernizacao", "retrofit",
    "duplicacao",
    "linhao", "subestacao",
    "ferrovia", "rodovia", "porto", "aeroporto",
    "edital", "licitacao", "homologacao",
    "anuncia r$", "anunciou r$", "aprovou r$",
    "obra de", "obras de", "obras no",
    "capex",
]

BLACKLIST = [
    "balanco", "lucro liquido", "demonstracao financeira",
    "premio", "premiacao", "homenagem",
    "morre aos", "falece",
    "recebe medalha",
]

UFS = {"AC","AL","AM","AP","BA","CE","DF","ES","GO","MA","MG","MS","MT",
       "PA","PB","PE","PI","PR","RJ","RN","RO","RR","RS","SC","SE","SP","TO"}

RE_VALOR = re.compile(
    r"r\$\s*([\d\.,]+)\s*(milh[oõ]es|bilh[oõ]es|mi\b|bi\b|trilh[oõ]es|tri\b)",
    re.IGNORECASE,
)
RE_UF_PAREN = re.compile(r"[\(\/\s]([A-Z]{2})[\)\s\.,]")
RE_EMPRESA = re.compile(
    r"^([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç0-9 \-\.&]{2,60}?)\s+"
    r"(investe|investira|investirao|anuncia|inaugura|expande|amplia|constroi|"
    r"implanta|moderniza|aporta|venceu|arrematou|conquistou)",
    re.IGNORECASE,
)
RE_HTML = re.compile(r"<[^>]+>")
RE_ENTITIES = re.compile(r"&[a-z]+;|&#\d+;")


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ASCII", "ignore").decode("ASCII")
    return s.lower()


def _strip_html(s: str) -> str:
    return RE_ENTITIES.sub(" ", RE_HTML.sub(" ", s or "")).strip()


def _bate_keyword(texto_norm: str) -> bool:
    if any(b in texto_norm for b in BLACKLIST):
        return False
    return any(k in texto_norm for k in KEYWORDS_OBRA)


def _extrair_valor(texto: str) -> tuple[float, str]:
    m = RE_VALOR.search(texto)
    if not m:
        return 0.0, ""
    num_str = m.group(1).replace(".", "").replace(",", ".")
    try:
        num = float(num_str)
    except ValueError:
        return 0.0, ""
    unidade = m.group(2).lower()
    if unidade.startswith("tri"):
        return num * 1_000_000_000_000, f"R$ {num:.1f} Tri"
    if unidade.startswith("bi"):
        return num * 1_000_000_000, f"R$ {num:.1f} Bi"
    return num * 1_000_000, f"R$ {num:.0f} Mi"


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
    if len(nome) < 3 or nome.lower() in {"empresa", "fabrica", "industria", "grupo", "governo"}:
        return None
    return nome[:200]


def _setor_de_categorias(category_ids: list[int]) -> str:
    """Primeira categoria do post que tem mapping vence."""
    for cid in (category_ids or []):
        if cid in CATEGORIA_SETOR:
            return CATEGORIA_SETOR[cid]
    return "INFRAESTRUTURA"


SETOR_NECESSIDADES = {
    "ENERGIA":        ["CIVIL_TECNICA", "ELETRICA_INDUSTRIAL", "TI_INFRAESTRUTURA"],
    "INFRAESTRUTURA": ["CIVIL_TECNICA", "TERRAPLANAGEM", "TOPOGRAFIA"],
    "MINERACAO":      ["CIVIL_TECNICA", "TERRAPLANAGEM", "ELETRICA_INDUSTRIAL"],
    "LOGISTICO":      ["CIVIL_TECNICA", "ESTRUTURA_METALICA"],
    "INDUSTRIAL":     ["CIVIL_TECNICA", "ELETRICA_INDUSTRIAL", "HIDRAULICA"],
}


def fetch_posts(*, dias: int, max_posts: int) -> list[dict]:
    """Pagina a API até obter max_posts ou esgotar."""
    desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00")
    posts: list[dict] = []
    page = 1
    while len(posts) < max_posts:
        params = {
            "per_page": PER_PAGE,
            "page": page,
            "categories": ",".join(str(c) for c in CATS_FILTRO),
            "modified_after": desde,
            "_fields": "id,date,modified,slug,link,title,excerpt,categories",
            "orderby": "modified",
            "order": "desc",
        }
        try:
            r = requests.get(URL_API, params=params,
                             headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
            if r.status_code == 400:
                # fim da paginação (alguns WPs retornam 400 quando page > total)
                break
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"  página {page} falhou: {e}")
            break
        batch = r.json()
        if not batch:
            break
        posts.extend(batch)
        if len(batch) < PER_PAGE:
            break
        page += 1
    return posts[:max_posts]


def main(*, dry_run: bool = False, dias: int = DIAS_JANELA,
         max_posts: int = MAX_POSTS) -> dict:
    log.info(f"=== INICIO AGENCIAINFRA {'(DRY-RUN)' if dry_run else ''} (janela {dias}d, cap {max_posts}) ===")
    stats = {"posts_baixados": 0, "match_keyword": 0, "obras_a_inserir": 0}

    posts = fetch_posts(dias=dias, max_posts=max_posts)
    stats["posts_baixados"] = len(posts)
    log.info(f"  posts baixados: {len(posts)}")

    obras_para_inserir = []
    for p in posts:
        titulo = _strip_html(p.get("title", {}).get("rendered", ""))
        excerpt = _strip_html(p.get("excerpt", {}).get("rendered", ""))
        link = p.get("link", "")
        post_id = p.get("id")
        cats = p.get("categories") or []

        texto_norm = _normalize(titulo + " " + excerpt)
        if not _bate_keyword(texto_norm):
            continue
        stats["match_keyword"] += 1

        empresa = _extrair_empresa(titulo)
        valor, valor_fmt = _extrair_valor(titulo + " " + excerpt)
        uf = _extrair_uf(titulo + " " + excerpt)
        setor = _setor_de_categorias(cats)
        necessidades = SETOR_NECESSIDADES.get(setor, ["CIVIL_TECNICA"])
        id_ext = f"agenciainfra_{post_id}"

        data_pub = None
        try:
            d = p.get("date") or p.get("modified")
            if d:
                data_pub = d.split("T")[0]
        except Exception:
            pass

        nome_obra = titulo[:280]
        descricao_obra = f"[Agência iNFRA] {excerpt[:600]} — fonte: {link}"

        obras_para_inserir.append((
            id_ext, nome_obra, empresa, None, setor, None, uf,
            valor or None, valor_fmt or None,
            "EM_EXECUCAO", "NOTICIA",
            3, 45, necessidades,
            descricao_obra[:1500],
            "agenciainfra_wp", link, data_pub, "NOTICIA",
        ))

    stats["obras_a_inserir"] = len(obras_para_inserir)
    log.info(f"  obras a inserir (após filtro keyword): {len(obras_para_inserir)}")

    if dry_run:
        log.info("  [DRY-RUN] nada gravado. Amostra dos 5 primeiros:")
        for o in obras_para_inserir[:5]:
            log.info(f"    {o[0]}: {o[1][:80]}  empresa={o[2]} setor={o[4]} valor={o[7]} uf={o[6]}")
        log.info("=== FIM AGENCIAINFRA ===")
        return stats

    if not obras_para_inserir:
        log.info("  nada a inserir")
        log.info("=== FIM AGENCIAINFRA ===")
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
            setor = COALESCE(obras.setor, EXCLUDED.setor),
            valor_estimado = COALESCE(obras.valor_estimado, EXCLUDED.valor_estimado),
            valor_formatado = COALESCE(obras.valor_formatado, EXCLUDED.valor_formatado),
            descricao = EXCLUDED.descricao,
            data_publicacao = COALESCE(obras.data_publicacao, EXCLUDED.data_publicacao)
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, obras_para_inserir)
        log.info(f"  UPSERT executado: {cur.rowcount} linhas afetadas")
        stats["upsert_rowcount"] = cur.rowcount
    conn.commit()
    conn.close()

    log.info("=== FIM AGENCIAINFRA ===")
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    dry = "--dry-run" in sys.argv
    r = main(dry_run=dry)
    print(f"\nResultado: {r}")
