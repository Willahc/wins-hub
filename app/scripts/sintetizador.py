"""
Sintetizador de descrições — gera texto a partir dos campos estruturados
da obra, em português neutro, ~250-700 chars. Determinístico, idempotente,
sem chamadas externas.

Uso típico:
    from scripts.sintetizador import gerar_descricao
    desc = gerar_descricao(obra_dict)  # dict com chaves do schema obras

A obra entra como dict (ou RealDictRow); valores ausentes/vazios são
omitidos da frase resultante. Política de chamada: sobrescrever apenas
quando LENGTH(descricao_atual) < 200 — fica a critério do caller.
"""
from __future__ import annotations

# ── Lookup tables ─────────────────────────────────────────────────────────────
SETOR_LABEL = {
    "MINERACAO": "mineração",
    "ENERGIA": "energia",
    "LATICINIOS": "laticínios",
    "INDUSTRIAL": "industrial",
    "PORTUARIO": "portuário",
    "FRIGORIFICO": "frigorífico",
    "INFRAESTRUTURA": "infraestrutura",
    "LOGISTICO": "logístico",
    "PETROLEO_GAS": "petróleo e gás",
    "SUCROENERGÉTICO": "sucroenergético",
    "AGROINDUSTRIAL": "agroindustrial",
    "AUTOMOTIVO": "automotivo",
}

FASE_LABEL = {
    "OPERACAO": "em operação",
    "EM_EXECUCAO": "em execução",
    "LICENCA_INSTALACAO": "com licença de instalação",
    "LICENCA_PREVIA": "com licença prévia",
    "LICITACAO_ABERTA": "com licitação aberta",
}

FONTE_LABEL = {
    "anm_cfem": "ANM (Compensação Financeira pela Exploração Mineral – CFEM)",
    "mapa_sif": "MAPA (Sistema SIF de inspeção de produtos de origem animal)",
    "bndes_financiamento": "BNDES (operações de financiamento)",
    "ibama_sislic": "IBAMA (Sistema de Licenciamento Ambiental – SISLIC)",
    "antaq_tup": "ANTAQ (Terminais de Uso Privado – TUP)",
    "antt_ferro_pic": "ANTT (Programa de Investimentos em Concessões Ferroviárias – PIC)",
    "aneel_siga": "ANEEL (Sistema de Informações de Geração – SIGA)",
    "PLANILHA": "planilha interna",
    "aneel_transmissao": "ANEEL (concessões de transmissão)",
    "cvm_ipe": "CVM (Informações Periódicas e Eventuais – IPE)",
    "anac_concessao": "ANAC (concessões aeroportuárias)",
    "antt_rod": "ANTT (concessões rodoviárias)",
    "anp_ep": "ANP (exploração e produção de petróleo e gás)",
    "debentures_infra": "debêntures de infraestrutura",
    "abiove_processadoras": "ABIOVE (processadoras de soja)",
    "unica_usinas": "UNICA (usinas sucroenergéticas)",
    "manual": "cadastro manual",
}


def _g(obra, k):
    """Get com strip de string ou None."""
    v = obra.get(k) if isinstance(obra, dict) else None
    if v is None: return None
    s = str(v).strip()
    return s or None


def gerar_descricao(obra) -> str:
    """Constrói descrição em português usando apenas campos populados."""
    nome = _g(obra, "nome")
    setor_raw = _g(obra, "setor")
    fase_raw = _g(obra, "fase")
    fonte_raw = _g(obra, "fonte")
    municipio = _g(obra, "municipio")
    uf = _g(obra, "uf")
    empresa = _g(obra, "empresa")
    cnpj = _g(obra, "cnpj")
    valor_fmt = _g(obra, "valor_formatado")
    url_fonte = _g(obra, "url_fonte")

    setor = SETOR_LABEL.get(setor_raw or "", setor_raw or "")
    fase = FASE_LABEL.get(fase_raw or "", "")
    fonte = FONTE_LABEL.get(fonte_raw or "", fonte_raw or "")

    # Frase 1: identificação
    f1 = f'"{nome}"' if nome else "Empreendimento"
    if setor:
        f1 = f"{f1} é um empreendimento do setor {setor}"
    else:
        f1 = f"{f1} é um empreendimento cadastrado"
    if municipio and uf:
        f1 += f", localizado em {municipio}/{uf}"
    elif uf:
        f1 += f", localizado no estado de {uf}"
    if fase:
        f1 += f", atualmente {fase}"
    f1 += "."

    # Frase 2: empresa responsável
    f2 = ""
    if empresa:
        f2 = f"A empresa responsável é {empresa}"
        if cnpj:
            f2 += f" (CNPJ {cnpj})"
        f2 += "."

    # Frase 3: valor
    f3 = f"O valor estimado/movimentado é de {valor_fmt}." if valor_fmt else ""

    # Frase 4: fonte
    f4 = f"Registro originado de {fonte}" if fonte else "Registro de origem cadastral"
    if url_fonte:
        f4 += f" — fonte oficial: {url_fonte}"
    f4 += "."

    return " ".join(p for p in (f1, f2, f3, f4) if p)
