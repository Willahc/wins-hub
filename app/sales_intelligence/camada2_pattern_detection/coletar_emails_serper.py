"""Coletor de emails via Serper API (Google Search).

v3 (2026-05-11): segundo round de fixes apos smoke v2
  - Adicionada PLACEHOLDERS_LOCAL (jane.doe, first.last, foo, bar, exemplo...)
    porque smoke v2 detectou pattern em emails-exemplo de docs/tutorials
  - Q3 invertida: '"diretor" "{dom}"' em vez de '"{dom}" diretor' (era 400)
    Regra empirica Serper: palavra-chave aspeada PRIMEIRO, dominio depois.

v2 (2026-05-11): bugs corrigidos pos-smoke inicial
  - Query 1: `"@{dom}"` quebrava com HTTP 400 -> trocada por `"email" "{dom}"`
  - Query 3: `... OR ...` quebrava -> split em 2 queries simples (mas v2 ainda 400)
  - Filtro GENERICOS_LOCAL expandido com setoriais BR (apoio, selecao, cac...)
  - Filtro considera CADA PARTE do local-part (apoio.plantao -> filtra)
"""
import os
import re
import logging
import requests
from typing import List, Tuple

log = logging.getLogger("sales_intel.coletar_serper")

SERPER_API_URL = "https://google.serper.dev/search"

# Local-parts genericos que NAO indicam pattern individual.
# Verificacao via split em [._-] -> qualquer parte basta para rejeitar.
GENERICOS_LOCAL = {
    # padrao geral
    "contato", "contact", "info", "admin", "noreply", "no-reply", "no_reply",
    "noresponse", "do-not-reply", "sac", "atendimento", "suporte", "support",
    "help", "imprensa", "comunicacao", "rh",
    "vendas", "comercial", "marketing", "financeiro", "tesouraria",
    "postmaster", "abuse", "mailer-daemon", "webmaster", "email",
    # setoriais BR
    "selecao", "recrutamento", "vagas", "trabalheconosco",
    "ouvidoria", "reclamacao", "reclamacoes",
    "tarifa", "tarifas", "pedagio",
    "cac", "canalde", "canaldefornecedores", "fornecedores",
    "faleconosco", "duvida", "duvidas",
    "apoio", "plantao", "urgencia", "emergencia",
    "cobranca", "faturamento", "pagamento",
    "compras", "licitacao", "licitacoes", "compliance",
}

# Placeholders de docs/tutoriais/forms — NAO sao pessoas reais.
PLACEHOLDERS_LOCAL = {
    "jane", "doe", "john", "jane.doe", "john.doe",
    "first", "last", "firstname", "lastname",
    "first.last", "firstname.lastname",
    "example", "exemplo", "teste", "test",
    "usuario", "user", "username",
    "nome", "sobrenome",
    "fulano", "beltrano", "sicrano",
    "foo", "bar", "baz",
    "mail", "email",
    "seu", "seunome", "seu_nome",
}


def _query_set(dominio: str) -> List[str]:
    """4 queries Serper, em ordem de produtividade observada.

    Regra empirica: palavra-chave aspeada primeiro, dominio aspeado depois.
    Quebrar essa ordem retorna HTTP 400 em metade dos casos.
    """
    return [
        f'"email" "{dominio}"',                          # winner v1: ate 8 emails
        f'site:linkedin.com "{dominio}" email',          # linkedin perfis
        f'"diretor" "{dominio}"',                        # v3: cargo primeiro
        f'"gerente" "{dominio}"',                        # v3: cargo primeiro
    ]


def _local_eh_descartavel(local: str) -> bool:
    """True se o local-part deve ser descartado (generico, placeholder, curto)."""
    if len(local) < 3:
        return True
    # composto (separador) ou >=5 chars
    if not any(c in local for c in "._-") and len(local) < 5:
        return True
    # exato em qualquer lista
    if local in GENERICOS_LOCAL or local in PLACEHOLDERS_LOCAL:
        return True
    # qualquer parte separada por [._-]
    for parte in re.split(r"[._-]", local):
        if not parte:
            continue
        if parte in GENERICOS_LOCAL or parte in PLACEHOLDERS_LOCAL:
            return True
    return False


def coletar_emails_via_serper(
    dominio: str, max_queries: int = 4
) -> List[Tuple[str, str]]:
    """Busca emails publicos do dominio via Serper.

    Retorna lista de (email, fonte_url) deduplicada case-insensitive.

    Anti-alucinacao:
      - Sem SERPER_API_KEY -> retorna []
      - HTTP nao-200 -> log warning, continua proxima query
      - Local-part curto/generico/setorial/placeholder -> descarta
      - Apenas emails do dominio alvo (regex ancorado)
    """
    if not dominio:
        return []
    api_key = os.getenv("SERPER_API_KEY")
    if not api_key:
        log.warning("SERPER_API_KEY ausente; serper harvester desligado")
        return []

    dominio_lower = dominio.lower()
    dominio_escaped = re.escape(dominio_lower)
    email_re = re.compile(
        rf"[a-zA-Z0-9._%+-]+@{dominio_escaped}", re.IGNORECASE
    )

    queries = _query_set(dominio_lower)[:max_queries]
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    seen: set = set()
    resultado: List[Tuple[str, str]] = []

    for q in queries:
        try:
            r = requests.post(
                SERPER_API_URL,
                json={"q": q, "gl": "br", "hl": "pt-br", "num": 10},
                headers=headers,
                timeout=15,
            )
            if r.status_code != 200:
                log.warning(f"serper HTTP {r.status_code} q={q!r}")
                continue
            body = r.json()
            organic = body.get("organic") or []
        except requests.RequestException as e:
            log.warning(f"serper req falhou q={q!r}: {e}")
            continue
        except ValueError as e:
            log.warning(f"serper json falhou q={q!r}: {e}")
            continue

        n_found = 0
        for item in organic:
            url = item.get("link") or ""
            text = (item.get("title") or "") + " " + (item.get("snippet") or "")
            for match in email_re.finditer(text):
                em = match.group(0).lower().strip(".,;:'\"<>()")
                if em in seen:
                    continue
                local = em.split("@", 1)[0]
                if _local_eh_descartavel(local):
                    continue
                seen.add(em)
                resultado.append((em, url))
                n_found += 1
        log.info(f"serper_harvester: query={q!r} results={len(organic)} emails_novos={n_found}")

    log.info(
        f"serper_harvester: dominio={dominio_lower} "
        f"emails_encontrados={len(resultado)} "
        f"amostra={[e for e, _ in resultado[:3]]}"
    )
    return resultado
