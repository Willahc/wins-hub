"""Camada 3 fonte secundaria: DOU (nomeacoes oficiais). Usa search chain."""
import logging
import re
from html import unescape
from typing import List

from sales_intelligence.search_engines.chain import default_chain
from sales_intelligence.models.decisor import DecisorBruto

log = logging.getLogger("sales_intel.dou_search")


def descobrir_via_dou(empresa_nome: str) -> List[DecisorBruto]:
    if not empresa_nome:
        return []
    query = f'site:in.gov.br "{empresa_nome}" (nomeacao OR designacao OR portaria)'
    resp = default_chain.search(query, max_results=15)
    if not resp.results and not resp.raw_html:
        log.info(f"dou sem resultados (engine={resp.engine}, status={resp.status})")
        return []

    texto = ""
    for sr in resp.results:
        texto += " " + sr.title + " " + sr.snippet
    if resp.raw_html:
        texto += " " + re.sub(r"<[^>]+>", " ", resp.raw_html)
    texto = re.sub(r"\s+", " ", unescape(texto))

    pat = re.compile(
        r"(?:Nomeia|Designa|Designar)\s+"
        r"([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÇa-záàâãéêíóôõúç]+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÇa-záàâãéêíóôõúç]+){1,5})"
        r"\s+(?:para\s+(?:o\s+)?cargo\s+de\s+|para\s+o\s+cargo\s+|como\s+)"
        r"([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wçãõéê\s]+?)(?=\s+(?:da|de|na|no|em|em substituicao|\.))",
        re.IGNORECASE,
    )
    decisores = []
    seen = set()
    for m in pat.finditer(texto):
        nome = m.group(1).strip()
        cargo = m.group(2).strip()[:80]
        if nome.lower() in seen or len(nome.split()) < 2:
            continue
        seen.add(nome.lower())
        i = m.start()
        snippet = texto[max(0, i - 100): i + 300][:500]
        decisores.append(DecisorBruto(
            nome_pessoa=nome,
            cargo_raw=cargo,
            cargo_normalizado=cargo,
            tipo_cargo="OUTRO",
            cargo_idioma="pt-br",
            cargo_nivel="estrategico",
            snippet_origem=snippet,
            url_origem=f"dou:{resp.engine}",
            confianca="alta",
            fonte_descoberta="dou",
        ))
    log.info(f"DOU: {len(decisores)} nomeacoes (engine={resp.engine})")
    return decisores
