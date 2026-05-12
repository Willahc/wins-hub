import re
import logging
from collections import Counter
from typing import List, Optional, Tuple

from sales_intelligence.camada2_pattern_detection.classificar_pessoa import (
    classificar_local_part,
)

log = logging.getLogger("sales_intel.detectar_padrao")


def _local_e_dominio(email: str) -> Tuple[str, str]:
    e = email.lower().strip()
    if "@" not in e:
        return "", ""
    local, _, host = e.partition("@")
    return local, host


def _pattern_format_from_local(local: str) -> Optional[str]:
    """Heuristica: descobrir formato do local-part. Retorna nome do padrao.

    (Renomeada de _classificar_local_part para nao colidir com
    classificar_local_part do modulo classificar_pessoa.)
    """
    # apenas letras/digitos/separadores
    if not re.match(r"^[a-z][a-z0-9._-]*$", local):
        return None
    # padroes ordenados por prioridade (mais especifico primeiro)
    if re.match(r"^[a-z]+\.[a-z]+$", local) and len(local.split(".")[0]) > 1:
        return "{first}.{last}"
    if re.match(r"^[a-z]+_[a-z]+$", local) and len(local.split("_")[0]) > 1:
        return "{first}_{last}"
    if re.match(r"^[a-z]\.[a-z]+$", local):
        return "{f}.{last}"
    if re.match(r"^[a-z]\_[a-z]+$", local):
        return "{f}_{last}"
    if re.match(r"^[a-z][a-z]+$", local):
        # ambiguo entre {first}, {last}, {first}{last}
        return "{first}{last}" if len(local) >= 8 else "{first}"
    return None


def detectar_padrao(emails: List[str], nomes_conhecidos: Optional[List[str]] = None):
    """Identifica padrao de email dominante. Retorna EmailPattern ou None.

    Anti-alucinacao v2 (2026-05-11): contagem por CLASSIFICACAO (pessoa/setor/
    placeholder/indefinido) antes de contar pattern. Threshold de confianca
    eh por pessoas_count, nao pelo total da amostra.
    """
    if not emails:
        return None

    dominio_origem = ""
    classifs: Counter = Counter()
    exemplos_por_padrao: dict = {}
    pessoas_count = 0
    setores_count = 0
    placeholders_count = 0
    indef_count = 0

    for em in emails:
        local, host = _local_e_dominio(em)
        if not local or not host:
            continue
        if not dominio_origem:
            dominio_origem = host

        tipo = classificar_local_part(local)
        if tipo == "placeholder":
            placeholders_count += 1
            continue
        if tipo == "setor":
            setores_count += 1
            continue
        if tipo == "indefinido":
            indef_count += 1
            continue

        # tipo == "pessoa"
        pessoas_count += 1
        padrao = _pattern_format_from_local(local)
        if padrao:
            classifs[padrao] += 1
            exemplos_por_padrao.setdefault(padrao, []).append(em)

    if pessoas_count == 0:
        log.info(
            f"sem pessoas reais (setores={setores_count} "
            f"placeholders={placeholders_count} indef={indef_count})"
        )
        return None

    if not classifs:
        log.info(f"pessoas={pessoas_count} mas sem pattern reconhecivel")
        return None

    dominante, count = classifs.most_common(1)[0]

    # Threshold por pessoas_count (anti-alucinacao em profundidade)
    if pessoas_count >= 3:
        conf = "alta"
    elif pessoas_count == 2:
        conf = "media"
    else:  # pessoas_count == 1
        conf = "baixa"

    amostra_total = pessoas_count + setores_count + placeholders_count + indef_count

    log.info(
        f"padrao={dominante} conf={conf} pessoas={pessoas_count} "
        f"setores={setores_count} ph={placeholders_count} indef={indef_count}"
    )

    from sales_intelligence.models.email_pattern import EmailPattern
    return EmailPattern(
        padrao=dominante,
        confianca=conf,
        exemplos=exemplos_por_padrao[dominante][:5],
        amostra_total=amostra_total,
        pessoas_count=pessoas_count,
        dominio_origem=dominio_origem,
    )
