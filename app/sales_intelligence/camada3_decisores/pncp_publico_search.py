"""Camada 3 — fonte especializada para obras PNCP de orgaos publicos.

Mari (linkedin_search) descobre decisores B2B privados (cargos tipo
"Procurement Manager", "Gerente de Suprimentos") via LinkedIn. Para
contratacoes PNCP de municipios, estados, secretarias, autarquias e
similares, os decisores reais sao pregoeiros, diretores de licitacao
e secretarios de obras — perfis com pegada em sites .gov.br e DOU.

Roda como FALLBACK no drain_queue quando descobrir_via_search_engines
retorna 0 decisores E a obra tem fonte LIKE 'pncp%' OU empresa do tipo
orgao publico.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from sales_intelligence.search_engines.chain import default_chain
from sales_intelligence.models.decisor import DecisorBruto

log = logging.getLogger("sales_intel.pncp_publico_search")


# Empresa que comeca com algum desses padroes = orgao publico
ORGAO_PUBLICO_RX = re.compile(
    r'^(municipio|estado|prefeitura|secretaria|departamento|servico social|'
    r'consorcio|unidade gestora|companhia.*saneamento|distrito federal|'
    r'governo|autarquia|fundacao\s+publica|instituto.*estadual|'
    r'instituto.*federal|empresa.*publica|tribunal|camara\s+municipal|'
    r'assembleia\s+legislativa|conselho\s+regional|conselho\s+federal)\b',
    re.IGNORECASE,
)

# Queries por cargo: (cargo_humano, tipo_cargo, nivel, raiz_busca, template).
# raiz_busca eh usada pelo extrator pra localizar o cargo no texto.
QUERIES_PUBLICO = (
    ("Pregoeiro", "GERENTE_COMPRAS", "tatico", "pregoeir",
     '"{empresa}" pregoeiro 2025 OR 2026'),
    ("Diretor de Licitacao", "GERENTE_COMPRAS", "estrategico", "licitac",
     '"{empresa}" ("diretor de licitação" OR "diretora de licitação" OR "diretor de licitacoes")'),
    ("Secretario de Obras", "COORDENADOR_OBRAS", "estrategico", "secretari",
     '"{empresa}" ("secretário de obras" OR "secretária de obras" OR "secretario municipal de obras")'),
    ("Engenheiro Responsavel", "ENGENHEIRO_MECANICO_CIVIL", "tatico", "engenh",
     '"{empresa}" "engenheiro responsável" (obra OR licitação OR projeto)'),
)


# Captura nomes proprios brasileiros (2-5 palavras), tolera de/da/do/dos/das/e
_NOME_RX = re.compile(
    r'\b('
    r'[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]+'
    r'(?:\s+(?:de|da|do|dos|das|e)?\s*[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]+){1,4}'
    r')\b'
)

# Palavras institucionais que nunca sao nome de pessoa
_BLACKLIST = (
    'prefeitura', 'municipio', 'município', 'secretaria', 'secretário', 'secretária',
    'governo', 'departamento', 'estado', 'instituto', 'comissao', 'comissão',
    'pregoeira', 'pregoeiro', 'licitacao', 'licitação', 'edital', 'editais',
    'concorrencia', 'concorrência', 'companhia', 'tribunal', 'camara',
    'câmara', 'assembleia', 'conselho', 'fundacao', 'fundação',
    'autarquia', 'centro', 'diretoria', 'gerencia', 'gerência',
    'superintendencia', 'superintendência', 'superintendente',
    'unidade', 'gestora', 'agente', 'contratacao', 'contratação',
    'portal', 'publicacao', 'publicação', 'aviso', 'extrato',
    'publica', 'pública', 'publico', 'público',
    'distrito', 'federal', 'nacional', 'estadual', 'municipal',
    'comprasnet', 'pncp', 'banrisul', 'licitanet', 'compras',
    'orgao', 'órgão', 'entidade', 'servico', 'serviço',
    'paranaeducacao', 'paranaeducação', 'invest', 'educacao', 'educação',
    'saude', 'saúde', 'obras', 'infraestrutura', 'transito', 'trânsito',
    'sociedade', 'cooperativa', 'fundo', 'caixa', 'previdencia',
    'previdência', 'tesouro', 'receita', 'fazenda', 'planejamento',
    'administracao', 'administração', 'gestao', 'gestão',
    'diario', 'diário', 'oficial', 'eletronico', 'eletrônico',
    'universidade', 'faculdade', 'instituto', 'acordao', 'acórdão',
    'direito', 'pdf', 'edicao', 'edição', 'numero', 'número',
    'porto velho', 'rio de janeiro', 'sao paulo', 'são paulo',
    'belo horizonte', 'salvador', 'fortaleza', 'curitiba', 'recife',
    'manaus', 'brasilia', 'brasília', 'teresina', 'palmas',
    'goiania', 'goiânia', 'natal', 'cuiaba', 'cuiabá',
)


# Palavras pequenas que NUNCA podem ser a primeira da string
_PRIMEIRA_RX_BAN = re.compile(r'^(no|na|da|de|do|dos|das|em|na|ao|aos|e|o|a)\s', re.IGNORECASE)


def is_orgao_publico(empresa_nome: Optional[str]) -> bool:
    if not empresa_nome:
        return False
    return bool(ORGAO_PUBLICO_RX.match(empresa_nome.strip()))


def _nome_eh_blacklisted(nome: str) -> bool:
    nl = nome.lower()
    return any(b in nl for b in _BLACKLIST)


def _nome_repete_empresa(nome: str, empresa: str) -> bool:
    """True se nome contem palavra >=4 chars da empresa — indica eco institucional."""
    if not empresa:
        return False
    nl = nome.lower()
    palavras_empresa = [p.lower() for p in re.findall(r'[A-Za-zÀ-ú]{4,}', empresa)]
    return any(p in nl for p in palavras_empresa)


def _extrair_nome_proximo(title: str, snippet: str, raiz: str, empresa: str = '') -> Optional[str]:
    """Procura nome proprio (2-5 palavras) numa janela de +/- 200 chars
    em torno da raiz do cargo (ex 'pregoeir'). Retorna primeiro valido."""
    texto = f'{title or ""} — {snippet or ""}'
    texto_l = texto.lower()
    pos = texto_l.find(raiz.lower())
    if pos < 0:
        return None
    win_start = max(0, pos - 200)
    win_end = min(len(texto), pos + 200)
    window = texto[win_start:win_end]

    for m in _NOME_RX.finditer(window):
        nome = m.group(1).strip()
        if len(nome.split()) < 2:
            continue
        # Rejeita TUDO MAIUSCULO (provavelmente sigla orgao)
        if nome == nome.upper():
            continue
        # Rejeita truncamentos visiveis (terminar em ç sem ao)
        if nome.endswith('ç') or nome.endswith('Ç'):
            continue
        # Rejeita se comeca com preposicao/artigo (No, Da, De, etc)
        if _PRIMEIRA_RX_BAN.match(nome):
            continue
        if _nome_eh_blacklisted(nome):
            continue
        # Rejeita se ecoa palavra da empresa
        if _nome_repete_empresa(nome, empresa):
            continue
        # Palavra muito longa (>14 chars) = provavelmente institucional
        if any(len(p) > 14 for p in nome.split() if p.lower() not in ('de','da','do','dos','das','e')):
            continue
        # Precisa pelo menos 2 palavras "substantivas" (>=3 letras, nao preposicao)
        palavras_subs = [p for p in nome.split() if len(p) >= 3 and p.lower() not in ('de','da','do','dos','das','e','no','na','em','ao')]
        if len(palavras_subs) < 2:
            continue
        return nome
    return None


def descobrir_via_pncp_publico(empresa_nome: str,
                                cnpj: Optional[str] = None,
                                max_queries: int = 4) -> List[DecisorBruto]:
    """Descobre decisores de orgao publico via Serper queries semanticas.

    Retorna [] se a empresa nao for orgao publico (deixa Mari atuar).
    Cap: 5 decisores total para evitar inflacao."""
    if not is_orgao_publico(empresa_nome):
        return []

    decisores: List[DecisorBruto] = []
    seen = set()

    for cargo_humano, tipo_cargo, nivel, raiz, query_tpl in QUERIES_PUBLICO[:max_queries]:
        query = query_tpl.format(empresa=empresa_nome[:80])
        log.info(f"[pncp_publico] q={query[:90]!r}")
        try:
            resp = default_chain.search(query, max_results=10)
        except Exception as e:
            log.warning(f"[pncp_publico] busca falhou: {e!r}")
            continue
        if not resp.results:
            log.info(f"  sem resultados (engine={resp.engine}, status={resp.status})")
            continue

        for sr in resp.results:
            nome = _extrair_nome_proximo(sr.title, sr.snippet, raiz, empresa=empresa_nome)
            if not nome:
                continue
            chave = nome.lower()
            if chave in seen:
                continue
            seen.add(chave)
            decisores.append(DecisorBruto(
                nome_pessoa=nome,
                cargo_raw=f'{cargo_humano} @ {empresa_nome[:80]}',
                cargo_normalizado=cargo_humano,
                tipo_cargo=tipo_cargo,
                cargo_idioma='pt-br',
                cargo_nivel=nivel,
                snippet_origem=(sr.snippet or '')[:500],
                url_origem=sr.url,
                confianca='media',
                fonte_descoberta='pncp_publico_serper',
            ))
            if len(decisores) >= 5:
                log.info(f"[pncp_publico] cap 5 atingido — empresa={empresa_nome[:60]}")
                return decisores

    log.info(f"[pncp_publico] {len(decisores)} decisores — empresa={empresa_nome[:60]}")
    return decisores
