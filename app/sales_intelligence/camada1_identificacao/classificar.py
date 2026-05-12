import re
from typing import Optional


HOLDING_PAT = re.compile(r"\b(HOLDING|PARTICIPACOES|PARTICIPAÇÕES)\b", re.IGNORECASE)
SPV_PAT = re.compile(r"\b(SPE|S\.?P\.?E\.?|CONCESSIONARIA|CONCESSIONÁRIA|EMPREENDIMENTOS?)\b", re.IGNORECASE)


def classificar_organizacao(dossier_parcial: dict) -> dict:
    razao = (dossier_parcial.get("razao_social") or "")
    fantasia = (dossier_parcial.get("nome_fantasia") or "")
    cnae = str(dossier_parcial.get("cnae_fiscal") or "")
    matriz = bool(dossier_parcial.get("matriz", True))

    is_holding_nome = bool(HOLDING_PAT.search(razao))
    is_holding_cnae = cnae.startswith("6462")  # 6462-0/00 sociedade holding
    is_spv_nome = bool(SPV_PAT.search(razao))

    if is_holding_nome or is_holding_cnae:
        tipo = "holding"
        spv = "holding"
        conf = 0.9 if (is_holding_nome and is_holding_cnae) else 0.7
    elif not matriz:
        tipo = "filial"
        spv = "filial"
        conf = 1.0
    elif is_spv_nome:
        tipo = "spv_operadora"
        spv = "spv_operadora"
        conf = 0.75
    elif fantasia and fantasia.upper() not in razao.upper() and razao.upper() not in fantasia.upper():
        # nome fantasia diferente da razao - provavel SPV de grupo
        tipo = "spv_provavel"
        spv = "spv_operadora"
        conf = 0.55
    else:
        tipo = "matriz_operadora"
        spv = "matriz_grupo"
        conf = 0.65

    return {
        "tipo_organizacao": tipo,
        "spv_ou_matriz": spv,
        "grupo_controlador": None,  # v1: sem lookup de grupos economicos
        "confianca_classificacao": conf,
    }
