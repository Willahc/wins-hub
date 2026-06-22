"""Helpers compartilhados pelos captadores PNCP (Portal Nacional de Contratacoes Publicas).

API publica:
    GET https://pncp.gov.br/api/consulta/v1/contratacoes/proposta
        ?dataInicial=YYYYMMDD&dataFinal=YYYYMMDD
        &codigoModalidadeContratacao=N
        &pagina=N

Modalidades relevantes para WiNS Hub:
    4  = Concorrencia Eletronica       (obras civis grandes)
    5  = Concorrencia Presencial       (idem)
    10 = Manifestacao de Interesse     (antecipa edital em 4-26 semanas)
    12 = Credenciamento                (registro de futuros fornecedores)

Idempotencia: id_externo = "PNCP:" + numeroControlePNCP
              (campo UNIQUE em obras).
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime
from typing import Any, Dict, Iterator, List, Optional

import psycopg2
import requests
from brutils import is_valid_cnpj


PNCP_BASE = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta"

# Modalidades
MOD_CONCORRENCIA_ELETRONICA = 4
MOD_CONCORRENCIA_PRESENCIAL = 5
MOD_MANIFESTACAO_INTERESSE = 10
MOD_CREDENCIAMENTO = 12

# CNPJs "guarda-chuva" — PNCP API retorna o mesmo CNPJ pra múltiplas entidades distintas.
# Detectados em 04/06/2026 cleanup: cada um agregava 12-30 municípios/consórcios sob 1 CNPJ.
# brutils valida só DV — passa. Solução: blacklist upstream + trigger DB preventivo.
# Adicionar novos quando check_cnpj_ofensores.py alertar.
_CNPJ_GUARDA_CHUVA_KNOWN = frozenset({
    "04056214000130",  # PNCP atribui a ~30 prefeituras diferentes (Bonfim-RR canônico)
    "01618402000117",  # PNCP atribui a ~20 municípios diferentes (Lavandeira-TO canônico)
    "08979143000107",  # PNCP atribui a ~12 consórcios distintos (VSF/Engie/Olympus/etc)
})

# Threshold de Pipeline (R$ 1 M). Sintonizavel.
VALOR_MINIMO = 10_000_000  # 16/06: 1mi->10mi, alinhado ao critério obra-válida (setor_categorias + >=10mi ou decisor)

# Keywords positivas (objetoCompra precisa conter pelo menos uma pra entrar como obra)
OBRA_KEYWORDS = (
    "obra", "construc", "construção", "reforma", "amplia",
    "implantação", "implantacao", "edifica", "engenharia",
    "pavimenta", "infraestrutura", "saneamento", "esgoto", "adutora",
    "rodovia", "ferrovia", "ponte", "viaduto", "porto", "aeroporto",
    "subesta", "linha de transmiss", "trecho",
    "hospital", "ubs", "upa", "centro de saúde",
    "escola", "creche", "universidade",
    "mina", "minera", "epc",
)

# Setor heuristico por palavras no objeto
SETOR_KEYWORDS = (
    ("SANEAMENTO",     ("esgoto", "saneamento", "agua ", "água", "ete", "eta ", "adutora", "drenagem")),
    ("INFRAESTRUTURA", ("rodovia", "estrada", "ponte", "viaduto", "pavimenta", "trecho", "duplica")),
    ("LOGISTICO",      ("ferrovia", "porto", "terminal", "aeroporto", "logistic")),
    ("ENERGIA",        ("energia", "subesta", "transmiss", "geração", "geracao", "eolica", "solar")),
    ("MINERACAO",      ("mina", "minera", "minerio")),
)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "db"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME", "wins_hub"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def setor_de_objeto(objeto: str) -> str:
    """Heuristica por keyword. Default OUTRO (nao bate matchmaking, fica como sinal)."""
    if not objeto:
        return "OUTRO"
    o = objeto.lower()
    for setor, kws in SETOR_KEYWORDS:
        if any(k in o for k in kws):
            return setor
    if any(k in o for k in ("obra", "construc", "edifica", "engenharia")):
        return "INFRAESTRUTURA"
    return "OUTRO"


def is_obra(objeto: Optional[str], modalidade_id: int, valor: Optional[float],
            filtrar_obra_keyword: bool = True) -> bool:
    """Decide se um record PNCP entra como obra.

    - Manifestacao de Interesse (10) entra sempre (sinal antecipado).
    - Concorrencia (4, 5) precisa de keyword + valor.
    - Demais (Pregao, Dispensa) descartadas a menos que keyword forte.
    """
    if modalidade_id == MOD_MANIFESTACAO_INTERESSE:
        return True
    if not objeto:
        return False
    if (valor or 0) < VALOR_MINIMO:
        return False
    if not filtrar_obra_keyword:
        return True
    o = objeto.lower()
    return any(k in o for k in OBRA_KEYWORDS)


def fetch_pncp_pages(*, modalidade: int, data_inicial: date, data_final: date,
                     max_paginas: int = 20,
                     session: Optional[requests.Session] = None,
                     log: Optional[logging.Logger] = None
                     ) -> Iterator[List[Dict[str, Any]]]:
    """Pagina a API PNCP. Generator → cada yield e a LISTA de records da pagina.
    Permite ao caller fazer early-stop por pagina (ex: defesa)."""
    log = log or logging.getLogger("pncp")
    sess = session or requests.Session()
    pagina = 1
    while pagina <= max_paginas:
        url = (f"{PNCP_BASE}"
               f"?dataInicial={data_inicial:%Y%m%d}"
               f"&dataFinal={data_final:%Y%m%d}"
               f"&codigoModalidadeContratacao={modalidade}"
               f"&pagina={pagina}")
        try:
            r = sess.get(url, timeout=60)
            r.raise_for_status()
        except Exception as e:
            log.warning(f"PNCP fetch falhou mod={modalidade} pag={pagina}: {e}")
            return
        try:
            d = r.json()
        except ValueError:
            log.warning(f"PNCP body vazio/invalido mod={modalidade} pag={pagina} status={r.status_code}")
            return
        if not isinstance(d, dict):
            log.warning(f"PNCP resposta nao-dict mod={modalidade} pag={pagina}")
            return
        data = d.get("data") or []
        yield data
        total_paginas = d.get("totalPaginas") or 0
        if pagina >= total_paginas:
            break
        pagina += 1
        time.sleep(0.3)


def fetch_pncp(*, modalidade: int, data_inicial: date, data_final: date,
               max_paginas: int = 20,
               session: Optional[requests.Session] = None,
               log: Optional[logging.Logger] = None) -> Iterator[Dict[str, Any]]:
    """Wrapper que achata fetch_pncp_pages em records individuais."""
    for page in fetch_pncp_pages(modalidade=modalidade,
                                 data_inicial=data_inicial,
                                 data_final=data_final,
                                 max_paginas=max_paginas,
                                 session=session,
                                 log=log):
        for record in page:
            yield record


def _parse_data_pncp(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def record_para_dict_obra(record: Dict[str, Any], fonte: str) -> Optional[Dict[str, Any]]:
    """Normaliza record PNCP -> dict pronto pra INSERT em obras. None se invalido."""
    try:
        orgao = record["orgaoEntidade"]
        unidade = record["unidadeOrgao"]
    except KeyError:
        return None

    cnpj_raw = (orgao.get("cnpj") or "").strip()
    cnpj_clean = re.sub(r"\D", "", cnpj_raw)
    cnpj_valido = is_valid_cnpj(cnpj_clean) if len(cnpj_clean) == 14 else False
    # CNPJs guarda-chuva conhecidos (PNCP retorna mesmo CNPJ pra múltiplas entidades).
    # Detectados em 04/06/2026 — agregam 12-30 municípios/consórcios distintos.
    # Trigger DB também protege (trg_detectar_cnpj_guarda_chuva), mas filtro upstream
    # evita warning + observação poluída.
    if cnpj_clean in _CNPJ_GUARDA_CHUVA_KNOWN:
        cnpj_valido = False

    nro_pncp = record.get("numeroControlePNCP") or ""
    if not nro_pncp:
        return None

    objeto = (record.get("objetoCompra") or "").strip()
    nome = objeto[:200] if objeto else (record.get("modalidadeNome") or "Contratacao") + f" {nro_pncp}"

    setor = setor_de_objeto(objeto)
    uf = unidade.get("ufSigla")
    municipio = unidade.get("municipioNome")
    valor = record.get("valorTotalEstimado")
    if valor in (0, 0.0):
        valor = None

    data_pub = _parse_data_pncp(record.get("dataPublicacaoPncp"))

    return {
        "nome": nome,
        "empresa": orgao.get("razaoSocial", "")[:255],
        "cnpj": cnpj_clean if cnpj_valido else None,
        "uf": (uf or "")[:2] or None,
        "municipio": municipio,
        "setor": setor,
        "valor_estimado": valor,
        "fonte": fonte,
        "url_fonte": record.get("linkSistemaOrigem") or "",
        "data_anuncio": data_pub,
        "descricao": objeto[:1000],
        "id_externo": f"PNCP:{nro_pncp}",
        "_cnpj_valido": cnpj_valido,
    }


def inserir_obra_pncp(conn, dados: Dict[str, Any]) -> Optional[str]:
    """INSERT obras com ON CONFLICT (id_externo) DO NOTHING. Retorna id se novo."""
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
                %s, 'anunciado', %s, 0.9,
                %s, false, %s
            )
            ON CONFLICT (id_externo) DO NOTHING
            RETURNING id
        """, (
            dados["nome"], dados["empresa"], dados["cnpj"],
            dados["uf"], dados["municipio"], dados["setor"],
            dados["valor_estimado"], dados["fonte"],
            dados["url_fonte"], dados["data_anuncio"],
            dados["descricao"], dados["id_externo"],
        ))
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else None


# CNPJs de orgaos da Defesa (Marinha, Exercito, Aeronautica + Min Defesa).
# Fonte: razaoSocial vista no PNCP — match por contains case-insensitive.
PADROES_DEFESA = (
    "marinha", "exercito", "exército", "aeronautica", "aeronáutica",
    "ministério da defesa", "ministerio da defesa",
    "comando do exerc", "comando da marinha", "comando da aeronautica",
    "comando da aeron",
    "imbel ", "amazul ", "emgepron", "embraer defesa",
)


def is_orgao_defesa(razao_social: str) -> bool:
    if not razao_social:
        return False
    r = razao_social.lower()
    return any(p in r for p in PADROES_DEFESA)
