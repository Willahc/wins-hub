"""Camada 3 fonte secundaria: CVM IPE (DRI/diretores listadas B3). Usa search chain."""
import logging
import re
from html import unescape
from typing import List

from sales_intelligence.search_engines.chain import default_chain
from sales_intelligence.models.decisor import DecisorBruto

log = logging.getLogger("sales_intel.cvm_ipe")


def descobrir_via_cvm(cnpj: str) -> List[DecisorBruto]:
    if not cnpj or len(cnpj) != 14:
        return []
    cnpj_fmt = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
    query = f'site:rad.cvm.gov.br "{cnpj_fmt}" diretor'
    resp = default_chain.search(query, max_results=15)
    if not resp.results and not resp.raw_html:
        log.info(f"cvm sem resultados (engine={resp.engine}, status={resp.status})")
        return []

    texto = ""
    for sr in resp.results:
        texto += " " + sr.title + " " + sr.snippet
    if resp.raw_html:
        texto += " " + re.sub(r"<[^>]+>", " ", resp.raw_html)
    texto = re.sub(r"\s+", " ", unescape(texto))

    pat = re.compile(
        r"(Diretor(?:\s+(?:de\s+)?[A-Z][a-zçãõéê]+)*?(?:\s+de\s+[A-Z][a-zçãõéê]+(?:\s+com\s+\w+)?)?)\s*[:-]\s*"
        r"([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]+){1,4})",
    )
    decisores = []
    seen = set()
    for m in pat.finditer(texto):
        cargo = m.group(1).strip()
        nome = m.group(2).strip()
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
            url_origem=f"cvm_ipe:{resp.engine}",
            confianca="alta",
            fonte_descoberta="cvm",
        ))
    log.info(f"CVM IPE: {len(decisores)} decisores encontrados (engine={resp.engine})")
    return decisores
