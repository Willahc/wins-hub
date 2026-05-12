"""
Coleta de inteligência comercial por empresa.

API: hackertarget.com hostsearch (free tier público, sem key).
Limite: ~100 queries/IP/dia.

Output: lista de subdomínios → derivação de tags acionáveis pro pitch.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

log = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

HACKERTARGET_URL = "https://api.hackertarget.com/hostsearch/"
HTTP_TIMEOUT = 30


# Mapa subdomínio-prefix → tag acionável.
# Substring match (lowercased) contra a parte ANTES do domínio raiz.
# Ordem importa: tags mais específicas antes das genéricas.
SUBDOM_TAG_MAP: list[tuple[str, str]] = [
    # ITSM / suporte
    ("servicedesk",   "tem_itsm"),
    ("helpdesk",      "tem_itsm"),
    ("itsm",          "tem_itsm"),
    ("suporte",       "tem_helpdesk"),
    # Treinamento corporativo (alto valor: indica demanda contínua de instrutor)
    ("escolavirtual", "tem_treinamento_corporativo"),
    ("educa",         "tem_treinamento_corporativo"),
    ("ead",           "tem_treinamento_corporativo"),
    ("lms",           "tem_treinamento_corporativo"),
    ("aprenda",       "tem_treinamento_corporativo"),
    ("treinamento",   "tem_treinamento_corporativo"),
    ("universidade",  "tem_universidade_corporativa"),
    # Compras / fornecedores (essencial pro pitch B2B)
    ("fornecedor",    "tem_portal_fornecedor"),
    ("supplier",      "tem_portal_fornecedor"),
    ("compras",       "tem_portal_compras"),
    ("procurement",   "tem_portal_compras"),
    # Documentos / assinatura
    ("assinatura",    "tem_assinatura_digital"),
    ("docs",          "tem_portal_documentos"),
    ("documentos",    "tem_portal_documentos"),
    # Integração / TI
    ("integracao",    "tem_integracao_api"),
    ("api",           "tem_api_publica"),
    ("aplicativos",   "tem_portal_aplicativos"),
    # Ambientes (indica time de TI maduro)
    ("homologacao",   "tem_ambiente_homol"),
    ("staging",       "tem_ambiente_homol"),
    ("dev",           "tem_ambiente_dev"),
    ("hml",           "tem_ambiente_homol"),
    ("test",          "tem_ambiente_dev"),
    # Portais corporativos
    ("intranet",      "tem_intranet"),
    ("extranet",      "tem_extranet"),
    ("portal",        "tem_portal_corporativo"),
    # Pessoas / vagas
    ("vagas",         "tem_portal_carreira"),
    ("trabalhe",      "tem_portal_carreira"),
    ("carreira",      "tem_portal_carreira"),
    ("rh",            "tem_rh_interno"),
    # Negócio
    ("crm",           "tem_crm"),
    ("erp",           "tem_erp"),
    ("loja",          "tem_ecommerce"),
    ("shop",          "tem_ecommerce"),
    ("ouvidoria",     "tem_ouvidoria"),
    ("blog",          "tem_blog_corporativo"),
]


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def buscar_subdominios(dominio: str) -> tuple[list[str], Optional[str]]:
    """Consulta hackertarget.com, devolve (lista_subdominios, erro_ou_None)."""
    dom = (dominio or "").strip().lower()
    if not dom or "." not in dom:
        return [], "dominio_invalido"
    try:
        r = httpx.get(HACKERTARGET_URL, params={"q": dom},
                      timeout=HTTP_TIMEOUT, follow_redirects=True)
    except httpx.RequestError as e:
        return [], f"erro_http: {type(e).__name__}"

    if r.status_code != 200:
        return [], f"http_{r.status_code}"
    body = (r.text or "").strip()
    if not body or body.lower().startswith("api count exceeded"):
        return [], "api_limit_excedido"
    if "error" in body.lower() and len(body) < 200:
        return [], f"erro_api: {body[:200]}"

    subs = set()
    for linha in body.splitlines():
        # CSV: "subdomain,ip"
        parts = linha.split(",")
        if not parts or not parts[0]:
            continue
        sd = parts[0].strip().lower()
        if sd == dom or sd.endswith("." + dom):
            subs.add(sd)
    return sorted(subs), None


def derivar_tags(subdominios: list[str], dominio: str) -> list[str]:
    """Substring match nos prefixos dos subdomínios → tags."""
    dom = dominio.lower()
    tags: set[str] = set()
    for sd in subdominios:
        # Pega o prefix (parte antes do domínio raiz)
        if sd == dom:
            continue
        prefix = sd
        if sd.endswith("." + dom):
            prefix = sd[: -(len(dom) + 1)]
        # Considera todos os tokens (ex: "loja.api" → ambos)
        for token in re.split(r"[.\-_]", prefix):
            for needle, tag in SUBDOM_TAG_MAP:
                if needle in token:
                    tags.add(tag)
    return sorted(tags)


def coletar_intel_empresa(cnpj: str, empresa: Optional[str], dominio: str,
                          *, fonte: str = "hackertarget") -> dict:
    """Coleta + deriva tags + persiste em empresa_intel (UPSERT por (cnpj, dominio)).
    Em caso de erro (rate limit, http 4xx/5xx, etc.), NÃO sobrescreve registro válido
    pré-existente — apenas loga e retorna pra retry no próximo ciclo."""
    subs, erro = buscar_subdominios(dominio)
    tags = derivar_tags(subs, dominio) if subs else []

    if erro:
        log.warning(f"intel {cnpj}/{dominio}: {erro} — skip persist (preserva dados anteriores)")
    else:
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO empresa_intel (cnpj, empresa, dominio, subdominios, tags, fonte, erro)
                    VALUES (%s, %s, %s, %s, %s, %s, NULL)
                    ON CONFLICT (cnpj, dominio) DO UPDATE SET
                        empresa     = EXCLUDED.empresa,
                        subdominios = EXCLUDED.subdominios,
                        tags        = EXCLUDED.tags,
                        fonte       = EXCLUDED.fonte,
                        erro        = NULL,
                        coletado_em = NOW()
                    """,
                    (cnpj, empresa, dominio.lower(), subs, tags, fonte),
                )
            conn.commit()
        finally:
            conn.close()

    return {
        "cnpj": cnpj, "empresa": empresa, "dominio": dominio,
        "subdominios": subs, "tags": tags, "erro": erro,
    }


def coletar_intel_obras_ouro(*, max_idade_dias: int = 7) -> dict:
    """Para cada empresa de obra-ouro com `dominio_email` cadastrado,
    coleta/atualiza intel se a última coleta foi > N dias atrás."""
    conn = _conn()
    rodadas = 0
    skips = 0
    erros = 0
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                WITH ouro AS (
                    SELECT DISTINCT o.empresa, o.cnpj, fm.dominio_email
                    FROM obras o
                    JOIN fornecedor_meta fm ON fm.cnpj = o.cnpj
                    WHERE o.nivel1_nome IS NOT NULL AND o.nivel1_nome != ''
                      AND (COALESCE(o.nivel1_email,'') != '' OR COALESCE(o.nivel1_linkedin,'') != '')
                      AND cargo_decisor_keyword(o.nivel1_cargo)
                      AND (o.visivel IS NULL OR o.visivel = TRUE)
                      AND fm.dominio_email IS NOT NULL AND fm.dominio_email != ''
                )
                SELECT o.empresa, o.cnpj, o.dominio_email,
                       i.coletado_em
                FROM ouro o
                LEFT JOIN empresa_intel i
                       ON i.cnpj = o.cnpj AND i.dominio = lower(o.dominio_email)
                ORDER BY i.coletado_em ASC NULLS FIRST
                """
            )
            alvos = cur.fetchall()
    finally:
        conn.close()

    for a in alvos:
        if a["coletado_em"]:
            from datetime import datetime, timezone, timedelta
            idade = datetime.now(timezone.utc) - a["coletado_em"]
            if idade < timedelta(days=max_idade_dias):
                skips += 1
                continue
        try:
            r = coletar_intel_empresa(a["cnpj"], a["empresa"], a["dominio_email"])
            if r.get("erro"):
                erros += 1
            rodadas += 1
        except Exception as e:
            log.exception(f"erro coletando intel {a['cnpj']}/{a['dominio_email']}: {e}")
            erros += 1

    return {"alvos": len(alvos), "rodadas": rodadas, "skips_recentes": skips, "erros": erros}


if __name__ == "__main__":
    import sys, json as _json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if len(sys.argv) >= 3 and sys.argv[1] == "--cnpj":
        # CLI: python recon_intel.py --cnpj 12345678000199 --dominio empresa.com.br [--empresa "Nome"]
        args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
        r = coletar_intel_empresa(
            args.get("--cnpj"),
            args.get("--empresa"),
            args.get("--dominio"),
        )
        print(_json.dumps(r, indent=2, ensure_ascii=False, default=str))
    elif "--obras-ouro" in sys.argv:
        r = coletar_intel_obras_ouro()
        print(_json.dumps(r, indent=2, ensure_ascii=False))
    else:
        print("Uso:")
        print("  python recon_intel.py --cnpj <cnpj> --dominio <dom> [--empresa <nome>]")
        print("  python recon_intel.py --obras-ouro")
        sys.exit(1)
