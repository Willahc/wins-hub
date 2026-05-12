"""Camada 3 fonte secundaria: CREA (engenheiros). Usa search chain."""
import logging
import re
from html import unescape
from typing import List

from sales_intelligence.search_engines.chain import default_chain
from sales_intelligence.models.decisor import DecisorBruto

log = logging.getLogger("sales_intel.crea_search")


def descobrir_via_crea(cnpj: str, empresa_nome: str) -> List[DecisorBruto]:
    if not empresa_nome:
        return []
    query = f'site:crea-*.org.br "{empresa_nome}" engenheiro'
    resp = default_chain.search(query, max_results=20)
    if not resp.results and not resp.raw_html:
        log.info(f"crea sem resultados (engine={resp.engine}, status={resp.status})")
        return []

    # combinar texto de todos os snippets/raw_html
    texto = ""
    for sr in resp.results:
        texto += " " + sr.title + " " + sr.snippet
    if resp.raw_html:
        texto += " " + re.sub(r"<[^>]+>", " ", resp.raw_html)
    texto = re.sub(r"\s+", " ", unescape(texto))

    pat = re.compile(
        r"(?:Eng\.?|Eng[ªº]\.?)\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]+){1,4})",
        re.IGNORECASE,
    )
    decisores = []
    seen = set()
    for m in pat.finditer(texto):
        nome = m.group(1).strip()
        if nome.lower() in seen or len(nome.split()) < 2:
            continue
        seen.add(nome.lower())
        i = m.start()
        snippet = texto[max(0, i - 200): i + 300][:500]
        decisores.append(DecisorBruto(
            nome_pessoa=nome,
            cargo_raw="Engenheiro",
            cargo_normalizado="Engenheiro",
            tipo_cargo="ENGENHEIRO_MECANICO_CIVIL",
            cargo_idioma="pt-br",
            cargo_nivel="operacional",
            snippet_origem=snippet,
            url_origem=f"crea_search:{resp.engine}",
            confianca="media",
            fonte_descoberta="crea",
        ))
    log.info(f"CREA: {len(decisores)} engenheiros encontrados (engine={resp.engine})")
    return decisores
