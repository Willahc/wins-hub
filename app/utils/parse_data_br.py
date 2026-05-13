"""Parser de data PT-BR informal.

Normaliza formatos brasileiros que `dateparser` não cobre nativamente:

  - "1º trimestre 2027" / "primeiro trimestre 2027" / "Q1 2027" → 2027-01-01
  - "2º trimestre 2027" / "Q2 2027"                            → 2027-04-01
  - "3º trimestre 2027" / "Q3 2027"                            → 2027-07-01
  - "4º trimestre 2027" / "Q4 2027"                            → 2027-10-01
  - "primeiro semestre 2027" / "1S2027"                        → 2027-01-01
  - "segundo semestre 2027" / "2S2027"                         → 2027-07-01
  - "outubro/27" / "out/27"                                    → 2027-10-01
  - "daqui a 6 meses" / "em 6 meses"                           → hoje + 6 × 30 dias
  - "início de 2027" / "começo de 2027"                        → 2027-02-01
  - "meados de 2027"                                           → 2027-06-01
  - "fim de 2027" / "final de 2027"                            → 2027-11-01

Quando nenhum padrão bate, cai pra `dateparser.parse(s, languages=['pt'])`.

API:
    parse_data_br(texto) -> date | None
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

import dateparser

# Mapa trimestre → mês inicial
_QUARTER_MES = {1: 1, 2: 4, 3: 7, 4: 10}
_SEMESTRE_MES = {1: 1, 2: 7}

_ORD_PT = {
    "primeiro": 1, "segundo": 2, "terceiro": 3, "quarto": 4,
    "1o": 1, "2o": 2, "3o": 3, "4o": 4,
}

_MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}

_PERIODO_ANO_MES = {
    "inicio": 2, "comeco": 2,
    "meados": 6,
    "fim": 11, "final": 11,
}


def _normalizar(s: str) -> str:
    """Lowercase + remove ordinais e acentos comuns. Espaço único."""
    s = s.lower().strip()
    s = s.replace("º", "o").replace("°", "o").replace("ª", "a")
    # remover acentos comuns (sem unidecode pra evitar dependência aqui)
    tr = str.maketrans({
        "á": "a", "ã": "a", "â": "a", "à": "a",
        "é": "e", "ê": "e",
        "í": "i",
        "ó": "o", "ô": "o", "õ": "o",
        "ú": "u",
        "ç": "c",
    })
    s = s.translate(tr)
    s = re.sub(r"\s+", " ", s)
    return s


def _ano_2d_para_4d(yy: str) -> int:
    """27 -> 2027. Domínio obras 2026+ → sempre século 21."""
    return 2000 + int(yy)


def _trimestre(s: str) -> Optional[date]:
    # "primeiro/1o trimestre [de] 2027" / "1o tri 2027"
    m = re.search(
        r"\b(primeiro|segundo|terceiro|quarto|[1-4]o)\s+(?:trimestre|tri)\b"
        r"(?:\s+(?:de\s+)?(\d{4}|\d{2}))?",
        s,
    )
    n: Optional[int]
    ano_str: Optional[str]
    if m:
        n = _ORD_PT.get(m.group(1))
        ano_str = m.group(2)
    else:
        # "Q3 2027" / "Q3/2027" / "Q3"
        m = re.search(r"\bq([1-4])\b(?:\s*[/\s]\s*(\d{4}|\d{2}))?", s)
        if not m:
            return None
        n = int(m.group(1))
        ano_str = m.group(2)
    if not n or not ano_str:
        return None
    ano = int(ano_str) if len(ano_str) == 4 else _ano_2d_para_4d(ano_str)
    return date(ano, _QUARTER_MES[n], 1)


def _semestre(s: str) -> Optional[date]:
    # "primeiro/segundo/1o/2o semestre [de] 2027"
    m = re.search(
        r"\b(primeiro|segundo|[12]o)\s+semestre\b"
        r"(?:\s+(?:de\s+)?(\d{4}|\d{2}))?",
        s,
    )
    n: Optional[int]
    ano_str: Optional[str]
    if m:
        ord_str = m.group(1)
        n = _ORD_PT.get(ord_str) or int(ord_str.replace("o", ""))
        ano_str = m.group(2)
    else:
        # "1S2027" / "2S2027" / "1s/2027"
        m = re.search(r"\b([12])\s*s\s*[/\s]?\s*(\d{4}|\d{2})\b", s)
        if not m:
            return None
        n = int(m.group(1))
        ano_str = m.group(2)
    if not n or not ano_str:
        return None
    ano = int(ano_str) if len(ano_str) == 4 else _ano_2d_para_4d(ano_str)
    return date(ano, _SEMESTRE_MES[n], 1)


def _relativo(s: str, hoje: Optional[date] = None) -> Optional[date]:
    # "daqui a 6 meses" / "em 6 meses" / "daqui 6 meses"
    m = re.search(
        r"\b(?:daqui(?:\s+a)?|em)\s+(\d{1,3})\s+(meses|mes|anos|ano|dias|dia|semanas|semana)\b",
        s,
    )
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    base = hoje or date.today()
    if unit.startswith("ano"):
        days = n * 365
    elif unit.startswith("mes"):
        days = n * 30
    elif unit.startswith("semana"):
        days = n * 7
    else:
        days = n
    return base + timedelta(days=days)


def _periodo_ano(s: str) -> Optional[date]:
    # "inicio/comeco/meados/fim/final de 2027"
    m = re.search(
        r"\b(inicio|comeco|meados|fim|final)\s+(?:de\s+)?(\d{4}|\d{2})\b",
        s,
    )
    if not m:
        return None
    periodo = m.group(1)
    ano_str = m.group(2)
    ano = int(ano_str) if len(ano_str) == 4 else _ano_2d_para_4d(ano_str)
    return date(ano, _PERIODO_ANO_MES[periodo], 1)


def _mes_barra_ano(s: str) -> Optional[date]:
    # "outubro/27" / "out/27" / "outubro-2027" / "out de 27"
    mes_pat = "|".join(sorted(_MESES_PT.keys(), key=len, reverse=True))
    m = re.search(rf"\b({mes_pat})\s*(?:[/\-]|\s+de\s+)\s*(\d{{2,4}})\b", s)
    if not m:
        return None
    nome_mes, ano_str = m.group(1), m.group(2)
    ano = int(ano_str) if len(ano_str) == 4 else _ano_2d_para_4d(ano_str)
    return date(ano, _MESES_PT[nome_mes], 1)


_HANDLERS = (
    _trimestre,
    _semestre,
    _relativo,
    _periodo_ano,
    _mes_barra_ano,
)


def parse_data_br(texto: Optional[str], hoje: Optional[date] = None) -> Optional[date]:
    """Tenta interpretar uma string PT-BR como data. Retorna `date` ou `None`.

    Args:
        texto: string informal (ex: "1o trimestre 2027").
        hoje:  data de referência pra cálculos relativos. Default = hoje real.

    Notas:
        - Se nenhum handler regex bate, faz fallback pra dateparser PT.
        - Anos com 2 dígitos sempre viram 20XX (domínio: obras 2026+).
    """
    if not texto:
        return None
    s = _normalizar(texto)
    if not s:
        return None

    for handler in _HANDLERS:
        try:
            if handler is _relativo:
                d = handler(s, hoje=hoje)
            else:
                d = handler(s)
        except Exception:
            d = None
        if d is not None:
            return d

    # Fallback: dateparser com locale PT
    try:
        parsed = dateparser.parse(
            s,
            languages=["pt"],
            settings={"PREFER_DATES_FROM": "future"},
        )
    except Exception:
        return None
    if parsed is None:
        return None
    return parsed.date() if isinstance(parsed, datetime) else parsed


if __name__ == "__main__":
    # Smoke local rápido (pra rodar via `python -m app.utils.parse_data_br`)
    casos = [
        "1o trimestre 2027",
        "primeiro trimestre 2027",
        "2o trimestre 2027",
        "Q3 2026",
        "Q4 2027",
        "primeiro semestre 2027",
        "segundo semestre 2027",
        "1S2027",
        "2S2027",
        "outubro/27",
        "out/27",
        "daqui a 6 meses",
        "em 6 meses",
        "inicio de 2027",
        "meados de 2027",
        "fim de 2027",
        "final de 2027",
        "outubro de 2027",
        "",
        "blablabla",
        None,
    ]
    for c in casos:
        print(f"  {c!r:40s} -> {parse_data_br(c)}")
