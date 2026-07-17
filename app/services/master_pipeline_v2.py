#!/usr/bin/env python3
"""Pipeline mestre V2 — alimenta wins_v2 apos capturas V1.

API publica:
    processar_captura_para_master(fonte, captador, id_externo, payload, contexto=None)
    processar_captura_para_master_safe(...)  # nunca propaga excecao
    is_master_pipeline_enabled()

Regras:
- Escreve apenas em wins_v2
- Nao publica obras V2
- Nao altera Portao / frontend / decisores
- Falha V2 nao interrompe V1 (use _safe)
- Feature flag MASTER_PIPELINE_V2_ENABLED (default false)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("wins_v2.master_pipeline")

# ---------------------------------------------------------------------------
# Constantes / catalogo 288 campos
# ---------------------------------------------------------------------------

ORIGEM_HISTORICO = "HISTORICO_IMPORTADO"
ORIGEM_NOVA = "CAPTURA_NOVA"
ORIGEM_REPROC = "REPROCESSAMENTO"

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CAMPOS_PATH = _DATA_DIR / "campos_288.json"

# Fallback se data/ nao estiver montada no container
_CAMPOS_288_FALLBACK = [
    "registro_mestre_id", "captura_id", "fonte", "captador", "id_externo",
    "hash_conteudo", "capturado_em", "quantidade_fontes", "fontes_encontradas",
    "completude_percentual", "possui_conflito", "qualidade_registro", "id_global",
    "versao_captador", "hash_payload", "payload_original", "titulo",
    "titulo_original", "descricao", "descricao_original", "data_publicacao",
    "data_original", "data_inicio", "data_fim", "valor_capex",
    "valor_financiamento", "valor_referencia", "tipo_valor", "moeda",
    "cnpj_contratante", "nome_contratante", "orgao_poder", "orgao_esfera",
    "cnpj_executora", "nome_executora", "cnpj_beneficiaria", "nome_beneficiaria",
    "cnpj_requerente", "nome_requerente", "cnpj_concessionaria",
    "nome_concessionaria", "papel_empresa", "municipio_obra", "municipio_sede",
    "uf_obra", "uf_sede", "tipo_localizacao", "municipios_abrangidos",
    "coordenadas", "tipo_registro", "setor", "fase_normalizada", "fase_original",
    "ativo_fisico", "acao_engenharia", "rastreabilidade", "confianca",
    "url_fonte", "url_detalhe", "documentos", "numero_processo",
    "numero_contrato", "endpoint_origem", "tipo_origem", "eh_obra_real",
    "motivo_skip", "cnpj_hint", "cnae_provavel", "metodo_extracao",
    "requer_validacao",
]


def load_campos_288() -> List[str]:
    if _CAMPOS_PATH.is_file():
        data = json.loads(_CAMPOS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list) and len(data) == 288:
            return [str(x) for x in data]
    # tentar path no container
    alt = Path("/app/services/master_pipeline_data/campos_288.json")
    if alt.is_file():
        data = json.loads(alt.read_text(encoding="utf-8"))
        if isinstance(data, list) and len(data) == 288:
            return [str(x) for x in data]
    return list(_CAMPOS_288_FALLBACK)


CAMPOS_288: List[str] = load_campos_288()

# Papel correto por familia (nao promover contratante a executora)
CNPJ_ROLE_BY_FAMILY: Dict[str, str] = {
    "pncp": "CONTRATANTE",
    "obrasgov": "EXECUTORA",
    "bndes": "BENEFICIARIA",
    "ibama": "REQUERENTE",
    "aneel_siga": "AGENTE_PROPRIETARIO",
    "aneel_transmissao": "CONCESSIONARIA",
    "antaq": "ENTIDADE_TERMINAL",
    "cvm": "EMISSOR",
    "dou": "HINT",
    "noticias": "HINT",
    "licenciamento": "REQUERENTE",
    "setorial": "HINT",
    "debentures": "EMISSOR",
    "default": "NAO_CLASSIFICADO",
}

PNCP_FONTES = {
    "pncp_obras", "pncp_civil_100k", "pncp_consulta", "pncp_defesa", "pncp_full",
}
TEXT_FONTES = {
    "dou", "noticias_setoriais", "eletrobras_ri", "doe_multi", "doe_pa",
    "doe_ms", "google_alerts", "agenciainfra_wp", "cimm_rss",
}


def is_master_pipeline_enabled() -> bool:
    raw = (os.environ.get("MASTER_PIPELINE_V2_ENABLED") or "false").strip().lower()
    return raw in {"1", "true", "yes", "on", "sim"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def hash_payload(payload: Any) -> str:
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def digits_only(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def is_valid_cnpj(value: Any) -> bool:
    number = digits_only(value)
    if len(number) != 14 or number == number[0] * 14:
        return False

    def check(base: str, weights: Sequence[int]) -> int:
        total = sum(int(d) * w for d, w in zip(base, weights))
        rem = total % 11
        return 0 if rem < 2 else 11 - rem

    d1 = check(number[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    d2 = check(number[:13], (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return number[-2:] == f"{d1}{d2}"


def normalize_cnpj(value: Any) -> Optional[str]:
    number = digits_only(value)
    if len(number) != 14 or not is_valid_cnpj(number):
        return None
    return number


def to_decimal(value: Any) -> Optional[float]:
    if value in (None, "", "NULL"):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def to_date_str(value: Any) -> Optional[str]:
    if value in (None, "", "NULL"):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", text):
        d, m, y = text.split("/")
        return f"{y}-{m}-{d}"
    if re.fullmatch(r"\d{8}", text):
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        return text[:10]
    return text[:10] if text else None


def family_of(fonte: str, captador: str) -> str:
    f = (fonte or "").lower()
    c = (captador or "").lower()
    blob = f"{f} {c}"
    if "pncp" in blob:
        return "pncp"
    if "obrasgov" in blob:
        return "obrasgov"
    if "bndes" in blob:
        return "bndes"
    if "ibama" in blob:
        return "ibama"
    if "transmiss" in blob or "aneel_trans" in blob:
        return "aneel_transmissao"
    if "aneel" in blob:
        return "aneel_siga"
    if "antaq" in blob:
        return "antaq"
    if "cvm" in blob:
        return "cvm"
    if "dou" in blob or "doe_" in blob:
        return "dou"
    if "noticia" in blob or "google_alert" in blob or "agencia" in blob:
        return "noticias"
    if any(x in blob for x in ("recife", "curitiba", "geosampa", "licenc", "alvara")):
        return "licenciamento"
    if "debentur" in blob:
        return "debentures"
    if any(x in blob for x in ("anp", "antt", "dnit", "suframa", "anm")):
        return "setorial"
    return "default"


def payload_get(payload: Mapping[str, Any], *paths: str) -> Any:
    """Resolve caminho simples com ponto ou chaves alternativas."""
    for path in paths:
        if not path:
            continue
        if path in payload and payload[path] not in (None, ""):
            return payload[path]
        cur: Any = payload
        ok = True
        for part in path.split("."):
            if isinstance(cur, Mapping) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


# ---------------------------------------------------------------------------
# Mapeamento canônico (somente campos existentes na fonte)
# ---------------------------------------------------------------------------

def mapear_para_288(
    fonte: str,
    captador: str,
    id_externo: str,
    payload: Mapping[str, Any],
    *,
    captura_id: Any = None,
    origem_marcador: str = ORIGEM_NOVA,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Retorna (campos, evidencias, entidades_papeis).

    campos: dict com as 288 chaves (None se ausente)
    evidencias: lista de {campo, valor_normalizado, valor_original, caminho, papel?}
    entidades: lista de {cnpj, nome, papel, campo_cnpj, campo_nome}
    """
    fam = family_of(fonte, captador)
    campos: Dict[str, Any] = {k: None for k in CAMPOS_288}
    evidencias: List[Dict[str, Any]] = []
    entidades: List[Dict[str, Any]] = []

    def set_field(
        campo: str,
        value: Any,
        *,
        caminho: str,
        original: Any = None,
        confianca: float = 1.0,
        tipo_origem: str = "nativo",
    ) -> None:
        if campo not in campos:
            return
        if value in (None, "", [], {}):
            return
        # Nunca gravar CAPEX declarado a partir de estimativa interna sem rotulo
        if campo == "valor_capex" and tipo_origem == "inferido":
            return
        campos[campo] = value
        evidencias.append(
            {
                "campo": campo,
                "valor_normalizado": value if not isinstance(value, (dict, list)) else stable_json(value),
                "valor_original": original if original is not None else value,
                "caminho": caminho,
                "confianca": confianca,
                "tipo_origem": tipo_origem,
            }
        )

    # Metadados sempre presentes
    set_field("fonte", fonte, caminho="meta.fonte", tipo_origem="nativo")
    set_field("captador", captador, caminho="meta.captador", tipo_origem="nativo")
    set_field("id_externo", id_externo, caminho="meta.id_externo", tipo_origem="nativo")
    set_field("captura_id", str(captura_id) if captura_id else None, caminho="meta.captura_id")
    set_field("capturado_em", _now_iso(), caminho="meta.capturado_em", tipo_origem="nativo")
    set_field("tipo_origem", "CAPTURA", caminho="meta.tipo_origem", tipo_origem="inferido", confianca=0.9)
    set_field("endpoint_origem", payload_get(payload, "endpoint_origem", "url", "url_fonte", "linkSistemaOrigem"), caminho="payload.url")

    # Campos V1 genericos embutidos no payload_minimo do inbox / dados normalizados
    nome = payload_get(payload, "nome", "titulo", "objetoCompra", "objeto", "nomempreendimento")
    set_field("titulo", (str(nome)[:500] if nome else None), caminho="nome|titulo|objetoCompra")
    set_field("titulo_original", (str(nome) if nome else None), caminho="nome|titulo|objetoCompra")
    desc = payload_get(payload, "descricao", "objetoCompra", "objeto", "descricao_original")
    set_field("descricao", (str(desc)[:2000] if desc else None), caminho="descricao|objetoCompra")
    set_field("descricao_original", (str(desc) if desc else None), caminho="descricao|objetoCompra")
    set_field("objeto_original", (str(desc) if desc else None), caminho="descricao|objetoCompra")
    set_field("municipio_obra", payload_get(payload, "municipio", "municipio_obra", "unidadeOrgao.municipioNome"), caminho="municipio")
    set_field("uf_obra", (str(payload_get(payload, "uf", "uf_obra", "unidadeOrgao.ufSigla") or "")[:2].upper() or None), caminho="uf")
    set_field("setor", payload_get(payload, "setor", "setor_original"), caminho="setor")
    set_field("fase_original", payload_get(payload, "fase", "fase_original", "situacao"), caminho="fase")
    set_field("url_fonte", payload_get(payload, "url_fonte", "linkSistemaOrigem", "url"), caminho="url_fonte")
    set_field("data_publicacao", to_date_str(payload_get(payload, "data_anuncio", "data_publicacao", "dataPublicacaoPncp", "dataCadastro")), caminho="data_*")
    set_field("data_original", to_date_str(payload_get(payload, "data_anuncio", "data_publicacao", "dataPublicacaoPncp")), caminho="data_*")

    valor = to_decimal(payload_get(payload, "valor_estimado", "valorTotalEstimado", "valor", "valor_contratado_reais", "montante"))
    empresa = payload_get(payload, "empresa", "razao_social", "orgaoEntidade.razaoSocial", "cliente", "nome")
    cnpj_raw = payload_get(payload, "cnpj", "orgaoEntidade.cnpj", "numcnpj", "cnpj_cliente")

    # --- Familia-especifica: papeis e valores ---
    if fam == "pncp":
        set_field("numero_controle_pncp", payload_get(payload, "numeroControlePNCP") or (id_externo.replace("PNCP:", "") if id_externo.startswith("PNCP:") else None), caminho="numeroControlePNCP")
        set_field("objeto_compra_original", payload_get(payload, "objetoCompra", "descricao"), caminho="objetoCompra")
        set_field("modalidade_original", payload_get(payload, "modalidadeNome"), caminho="modalidadeNome")
        set_field("modalidade_codigo", payload_get(payload, "modalidadeId", "codigoModalidadeContratacao"), caminho="modalidadeId")
        set_field("orgao_poder", payload_get(payload, "orgaoEntidade.poderId"), caminho="orgaoEntidade.poderId")
        set_field("orgao_esfera", payload_get(payload, "orgaoEntidade.esferaId"), caminho="orgaoEntidade.esferaId")
        set_field("nome_unidade_gestora", payload_get(payload, "unidadeOrgao.nomeUnidade"), caminho="unidadeOrgao.nomeUnidade")
        set_field("codigo_unidade_gestora", payload_get(payload, "unidadeOrgao.codigoUnidade"), caminho="unidadeOrgao.codigoUnidade")
        set_field("municipio_unidade_gestora", payload_get(payload, "unidadeOrgao.municipioNome"), caminho="unidadeOrgao.municipioNome")
        set_field("uf_unidade_gestora", payload_get(payload, "unidadeOrgao.ufSigla"), caminho="unidadeOrgao.ufSigla")
        cnpj_c = normalize_cnpj(payload_get(payload, "orgaoEntidade.cnpj", "cnpj"))
        nome_c = payload_get(payload, "orgaoEntidade.razaoSocial", "empresa")
        if cnpj_c:
            set_field("cnpj_contratante", cnpj_c, caminho="orgaoEntidade.cnpj")
            set_field("nome_contratante", nome_c, caminho="orgaoEntidade.razaoSocial")
            entidades.append({"cnpj": cnpj_c, "nome": nome_c, "papel": "CONTRATANTE", "campo_cnpj": "cnpj_contratante", "campo_nome": "nome_contratante"})
        # valor estimado NUNCA vira valor_capex declarado
        if valor is not None:
            set_field("valor_estimado_contratacao", valor, caminho="valorTotalEstimado|valor_estimado")
            set_field("tipo_valor", "ESTIMADO", caminho="meta.tipo_valor", tipo_origem="inferido", confianca=0.9)
            set_field("origem_valor", "VALOR_ESTIMADO_PNCP", caminho="meta.origem_valor", tipo_origem="inferido", confianca=0.9)
        set_field("papel_empresa", "CONTRATANTE", caminho="meta.papel", tipo_origem="inferido", confianca=0.9)
        set_field("papel_entidade", "CONTRATANTE", caminho="meta.papel", tipo_origem="inferido", confianca=0.9)

    elif fam == "obrasgov":
        cnpj_e = normalize_cnpj(cnpj_raw)
        if cnpj_e:
            set_field("cnpj_executora", cnpj_e, caminho="cnpj")
            set_field("nome_executora", empresa, caminho="empresa")
            entidades.append({"cnpj": cnpj_e, "nome": empresa, "papel": "EXECUTORA", "campo_cnpj": "cnpj_executora", "campo_nome": "nome_executora"})
        if valor is not None:
            set_field("valor_investimento_previsto", valor, caminho="valor_estimado")
            set_field("tipo_valor", "INVESTIMENTO_PREVISTO", caminho="meta.tipo_valor", tipo_origem="inferido")
            set_field("origem_valor", "OBRASGOV", caminho="meta.origem_valor", tipo_origem="inferido")
        set_field("codigo_obra_ativo", payload_get(payload, "idUnico", "codigo_obra"), caminho="idUnico")
        set_field("papel_empresa", "EXECUTORA", caminho="meta.papel", tipo_origem="inferido")
        set_field("papel_entidade", "EXECUTORA", caminho="meta.papel", tipo_origem="inferido")

    elif fam == "bndes":
        cnpj_b = normalize_cnpj(cnpj_raw)
        if cnpj_b:
            set_field("cnpj_beneficiaria", cnpj_b, caminho="cnpj")
            set_field("nome_beneficiaria", empresa, caminho="empresa|cliente")
            entidades.append({"cnpj": cnpj_b, "nome": empresa, "papel": "BENEFICIARIA", "campo_cnpj": "cnpj_beneficiaria", "campo_nome": "nome_beneficiaria"})
        if valor is not None:
            set_field("valor_financiamento", valor, caminho="valor_estimado|valor_contratado_reais")
            set_field("tipo_valor", "FINANCIAMENTO", caminho="meta.tipo_valor", tipo_origem="inferido")
            set_field("origem_valor", "BNDES", caminho="meta.origem_valor", tipo_origem="inferido")
        set_field("numero_contrato", payload_get(payload, "numero_do_contrato", "numero_contrato"), caminho="numero_do_contrato")
        set_field("data_contratacao", to_date_str(payload_get(payload, "data_da_contratacao", "data_contratacao", "data_anuncio")), caminho="data_da_contratacao")
        set_field("produto_financeiro", payload_get(payload, "produto"), caminho="produto")
        set_field("porte_cliente", payload_get(payload, "porte_do_cliente"), caminho="porte_do_cliente")
        set_field("situacao_contrato", payload_get(payload, "situacao_do_contrato"), caminho="situacao_do_contrato")
        set_field("papel_empresa", "BENEFICIARIA", caminho="meta.papel", tipo_origem="inferido")

    elif fam == "ibama":
        cnpj_r = normalize_cnpj(cnpj_raw)
        if cnpj_r:
            set_field("cnpj_requerente", cnpj_r, caminho="cnpj")
            set_field("nome_requerente", empresa, caminho="empresa")
            entidades.append({"cnpj": cnpj_r, "nome": empresa, "papel": "REQUERENTE", "campo_cnpj": "cnpj_requerente", "campo_nome": "nome_requerente"})
        set_field("numero_processo", payload_get(payload, "numeroProcesso", "numero_processo"), caminho="numeroProcesso")
        set_field("numero_licenca", payload_get(payload, "numLicenca", "numero_licenca"), caminho="numLicenca")
        set_field("tipo_licenca", payload_get(payload, "desTipoLicenca", "tipo_licenca"), caminho="desTipoLicenca")
        set_field("tipologia", payload_get(payload, "tipologia"), caminho="tipologia")
        set_field("data_emissao", to_date_str(payload_get(payload, "dataEmissao")), caminho="dataEmissao")
        set_field("data_vencimento", to_date_str(payload_get(payload, "dataVencimento")), caminho="dataVencimento")
        set_field("papel_empresa", "REQUERENTE", caminho="meta.papel", tipo_origem="inferido")

    elif fam == "aneel_siga":
        cnpj_a = normalize_cnpj(cnpj_raw)
        if cnpj_a:
            set_field("cnpj_agente_proprietario", cnpj_a, caminho="cnpj|numcnpj")
            set_field("nome_agente_proprietario", empresa, caminho="empresa|nomagente")
            entidades.append({"cnpj": cnpj_a, "nome": empresa, "papel": "AGENTE_PROPRIETARIO", "campo_cnpj": "cnpj_agente_proprietario", "campo_nome": "nome_agente_proprietario"})
        set_field("codigo_ceg", payload_get(payload, "codceg", "ceg", "codigo_ceg"), caminho="codceg")
        set_field("nucleo_ceg", payload_get(payload, "idenucleoceg", "nucleoceg"), caminho="idenucleoceg")
        pot = to_decimal(payload_get(payload, "mdapotenciaoutorgadakw", "potenciaoutorgada"))
        set_field("potencia_outorgada_kw", pot, caminho="mdapotenciaoutorgadakw")
        set_field("tipo_geracao", payload_get(payload, "sigtipogeracao", "tipogeracao"), caminho="sigtipogeracao")
        set_field("data_entrada_operacao", to_date_str(payload_get(payload, "datentradaoperacao")), caminho="datentradaoperacao")
        # potencia * referencia NUNCA vira valor_capex factual
        if valor is not None:
            set_field("valor_referencia", valor, caminho="valor_estimado")
            set_field("tipo_valor", "REFERENCIA", caminho="meta.tipo_valor", tipo_origem="inferido")
            set_field("origem_valor", "ANEEL_SIGA_NAO_CAPEX", caminho="meta.origem_valor", tipo_origem="inferido")
            set_field("metodo_estimativa", "NAO_DECLARADO_COMO_CAPEX", caminho="meta.metodo", tipo_origem="inferido")
        set_field("papel_empresa", "AGENTE_PROPRIETARIO", caminho="meta.papel", tipo_origem="inferido")

    elif fam == "aneel_transmissao":
        cnpj_c = normalize_cnpj(cnpj_raw)
        if cnpj_c:
            set_field("cnpj_concessionaria", cnpj_c, caminho="cnpj")
            set_field("nome_concessionaria", empresa, caminho="empresa")
            entidades.append({"cnpj": cnpj_c, "nome": empresa, "papel": "CONCESSIONARIA", "campo_cnpj": "cnpj_concessionaria", "campo_nome": "nome_concessionaria"})
        if valor is not None:
            set_field("valor_investimento_previsto", valor, caminho="valor_estimado")
            set_field("tipo_valor", "INVESTIMENTO_PREVISTO", caminho="meta.tipo_valor", tipo_origem="inferido")
            set_field("origem_valor", "ANEEL_TRANSMISSAO", caminho="meta.origem_valor", tipo_origem="inferido")
        set_field("numero_licitacao_leilao", payload_get(payload, "numero_leilao", "leilao"), caminho="numero_leilao")
        set_field("numero_lote", payload_get(payload, "lote", "numero_lote"), caminho="lote")
        set_field("papel_empresa", "CONCESSIONARIA", caminho="meta.papel", tipo_origem="inferido")

    elif fam == "antaq":
        cnpj_t = normalize_cnpj(cnpj_raw)
        if cnpj_t:
            set_field("cnpj_entidade_terminal", cnpj_t, caminho="cnpj")
            set_field("nome_terminal", empresa, caminho="empresa")
            entidades.append({"cnpj": cnpj_t, "nome": empresa, "papel": "ENTIDADE_TERMINAL", "campo_cnpj": "cnpj_entidade_terminal", "campo_nome": "nome_terminal"})
        if valor is not None:
            set_field("montante_investimento_antaq", valor, caminho="valor_estimado")
            set_field("tipo_valor", "INVESTIMENTO_ANTAQ", caminho="meta.tipo_valor", tipo_origem="inferido")
            set_field("origem_valor", "ANTAQ", caminho="meta.origem_valor", tipo_origem="inferido")
        set_field("papel_empresa", "ENTIDADE_TERMINAL", caminho="meta.papel", tipo_origem="inferido")
        set_field("requer_validacao", True, caminho="meta.requer_validacao", tipo_origem="inferido")

    elif fam == "cvm":
        cnpj_e = normalize_cnpj(cnpj_raw)
        if cnpj_e:
            set_field("cnpj_emissor", cnpj_e, caminho="cnpj")
            set_field("nome_emissor", empresa, caminho="empresa")
            entidades.append({"cnpj": cnpj_e, "nome": empresa, "papel": "EMISSOR", "campo_cnpj": "cnpj_emissor", "campo_nome": "nome_emissor"})
        set_field("categoria_documento", payload_get(payload, "categoria"), caminho="categoria")
        set_field("tipo_documento", payload_get(payload, "tipo"), caminho="tipo")
        set_field("requer_validacao", True, caminho="meta.requer_validacao", tipo_origem="inferido")

    elif fam in {"dou", "noticias"}:
        set_field("metodo_extracao", "LLM_OU_TEXTO", caminho="meta.metodo", tipo_origem="inferido")
        set_field("requer_validacao", True, caminho="meta.requer_validacao", tipo_origem="inferido")
        set_field("resumo_original", payload_get(payload, "resumo", "snippet", "descricao"), caminho="resumo|descricao")
        set_field("texto_original", payload_get(payload, "texto", "texto_original"), caminho="texto")
        set_field("tipo_publicacao", payload_get(payload, "tipo_publicacao", "secao"), caminho="tipo_publicacao")
        if valor is not None:
            set_field("capex_extraido_llm", valor, caminho="valor_estimado")
            set_field("tipo_valor", "CAPEX_LLM", caminho="meta.tipo_valor", tipo_origem="inferido")
            set_field("origem_valor", "EXTRAIDO_TEXTO_NAO_DECLARADO", caminho="meta.origem_valor", tipo_origem="inferido")
            set_field("capex_suspeito", True, caminho="meta.capex_suspeito", tipo_origem="inferido")
        # CNPJ so como hint — nunca promover a executora
        cnpj_h = normalize_cnpj(payload_get(payload, "cnpj_hint", "cnpj"))
        if cnpj_h:
            set_field("cnpj_hint", cnpj_h, caminho="cnpj_hint|cnpj")
            set_field("nome_entidade_extraida", empresa, caminho="empresa")
            entidades.append({"cnpj": cnpj_h, "nome": empresa, "papel": "HINT", "campo_cnpj": "cnpj_hint", "campo_nome": "nome_entidade_extraida"})

    elif fam == "licenciamento":
        cnpj_r = normalize_cnpj(cnpj_raw)
        if cnpj_r:
            set_field("cnpj_requerente", cnpj_r, caminho="cnpj")
            set_field("nome_requerente", empresa, caminho="empresa")
            entidades.append({"cnpj": cnpj_r, "nome": empresa, "papel": "REQUERENTE", "campo_cnpj": "cnpj_requerente", "campo_nome": "nome_requerente"})
        set_field("numero_processo", payload_get(payload, "numero_processo", "processo"), caminho="numero_processo")
        set_field("numero_alvara", payload_get(payload, "numero_alvara", "alvara"), caminho="numero_alvara")
        set_field("endereco_obra", payload_get(payload, "endereco", "endereco_obra"), caminho="endereco")
        set_field("papel_empresa", "REQUERENTE", caminho="meta.papel", tipo_origem="inferido")

    elif fam == "debentures":
        cnpj_e = normalize_cnpj(cnpj_raw)
        if cnpj_e:
            set_field("cnpj_emissor", cnpj_e, caminho="cnpj")
            set_field("nome_emissor", empresa, caminho="empresa")
            entidades.append({"cnpj": cnpj_e, "nome": empresa, "papel": "EMISSOR", "campo_cnpj": "cnpj_emissor", "campo_nome": "nome_emissor"})
        if valor is not None:
            set_field("valor_emissao_debenture", valor, caminho="valor_estimado")
            set_field("tipo_valor", "EMISSAO_DEBENTURE", caminho="meta.tipo_valor", tipo_origem="inferido")
            set_field("origem_valor", "DEBENTURES_INFRA", caminho="meta.origem_valor", tipo_origem="inferido")

    else:
        # setorial / default: preservar cnpj generico sem promover papel errado
        cnpj_g = normalize_cnpj(cnpj_raw)
        role = CNPJ_ROLE_BY_FAMILY.get(fam, "NAO_CLASSIFICADO")
        if cnpj_g:
            set_field("cnpj_hint", cnpj_g, caminho="cnpj")
            set_field("nome_entidade_extraida", empresa, caminho="empresa")
            entidades.append({"cnpj": cnpj_g, "nome": empresa, "papel": role, "campo_cnpj": "cnpj_hint", "campo_nome": "nome_entidade_extraida"})
        if valor is not None:
            set_field("valor_referencia", valor, caminho="valor_estimado")
            set_field("tipo_valor", "REFERENCIA", caminho="meta.tipo_valor", tipo_origem="inferido")
            set_field("origem_valor", "FONTE_SETORIAL", caminho="meta.origem_valor", tipo_origem="inferido")

    # Payload bruto sempre referenciado (nao como CAPEX)
    campos["payload_original"] = payload if isinstance(payload, (dict, list)) else {"raw": payload}
    campos["hash_payload"] = hash_payload(payload)
    campos["hash_conteudo"] = campos["hash_payload"]
    campos["quantidade_fontes"] = 1
    campos["fontes_encontradas"] = fonte
    preenchidos = sum(1 for k, v in campos.items() if v not in (None, "", [], {}) and k not in {"payload_original"})
    campos["completude_percentual"] = round(100.0 * preenchidos / max(len(CAMPOS_288), 1), 2)
    campos["qualidade_registro"] = "PIPELINE_V2"
    campos["moeda"] = campos.get("moeda") or "BRL"

    return campos, evidencias, entidades


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

def _db_connect_kwargs() -> Dict[str, Any]:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DB_DSN")
    if dsn:
        return {"dsn": dsn}
    return {
        "host": os.environ.get("DB_HOST", "db"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ.get("DB_NAME", "wins_hub"),
        "user": os.environ.get("DB_USER", "wins_app"),
        "password": os.environ.get("DB_PASSWORD", ""),
    }


def _connect():
    import psycopg2

    kw = _db_connect_kwargs()
    if "dsn" in kw:
        return psycopg2.connect(kw["dsn"])
    return psycopg2.connect(**kw)


def _ensure_fonte_captador(cur, fonte: str, captador: str) -> Tuple[int, int]:
    cur.execute(
        """
        INSERT INTO wins_v2.fontes (nome, nome_curto, tipo, categoria, ativo)
        VALUES (%s, %s, 'Outro', 'C', true)
        ON CONFLICT (nome) DO UPDATE SET ativo = true
        RETURNING id
        """,
        (fonte, (fonte or "")[:32]),
    )
    fonte_id = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO wins_v2.captadores (nome, fonte_id, script_path, versao, ativo)
        VALUES (%s, %s, %s, 'pipeline-v2', true)
        ON CONFLICT (nome) DO UPDATE SET fonte_id = EXCLUDED.fonte_id, ativo = true
        RETURNING id
        """,
        (captador, fonte_id, f"pipeline/{captador}"),
    )
    # captadores may not have unique on nome — fallback
    row = cur.fetchone()
    if row:
        captador_id = row[0]
    else:
        cur.execute("SELECT id FROM wins_v2.captadores WHERE nome = %s LIMIT 1", (captador,))
        captador_id = cur.fetchone()[0]
    return int(fonte_id), int(captador_id)


def _ensure_campo(cur, campo_id: str, nome: str) -> None:
    cur.execute(
        """
        INSERT INTO wins_v2.campos_canonicos (id, nome, categoria, tipo_dado, ativo, versao)
        VALUES (%s, %s, 'PlanilhaMestre288', 'text', true, '2.0')
        ON CONFLICT (id) DO NOTHING
        """,
        (campo_id, nome),
    )


def _ensure_campos_288(cur) -> None:
    for campo in CAMPOS_288:
        _ensure_campo(cur, campo, campo)


def registrar_falha(
    *,
    fonte: str,
    captador: str,
    id_externo: str,
    payload: Any,
    erro: str,
    contexto: Optional[Mapping[str, Any]] = None,
    namespace: str = "default",
) -> None:
    try:
        conn = _connect()
    except Exception as exc:  # pragma: no cover
        logger.error("falha ao conectar para registrar pipeline_falhas: %s", exc)
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wins_v2.pipeline_falhas
                        (fonte, captador, id_externo, namespace, payload, contexto, erro, status)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, 'pendente')
                    """,
                    (
                        fonte,
                        captador,
                        id_externo,
                        namespace,
                        stable_json(payload),
                        stable_json(dict(contexto or {})),
                        str(erro)[:4000],
                    ),
                )
    except Exception as exc:  # pragma: no cover
        logger.error("nao foi possivel registrar falha do pipeline: %s", exc)
    finally:
        conn.close()


def _resolver_origem_marcador(
    cur: Any,
    *,
    fonte: str,
    id_externo: str,
    origem_solicitada: str,
    captura_id: Any,
    origem_explicita: bool,
) -> Tuple[str, Optional[str]]:
    """Resolve marcador semantico e v1_obra_id.

    Regras:
    - REPROCESSAMENTO / HISTORICO_IMPORTADO explicitos: preservados
    - CAPTURA_NOVA com captura_id (hook pos-INSERT V1): mantem CAPTURA_NOVA
    - CAPTURA_NOVA sem captura_id (UPSERT de adaptador): se V1 pre-existir ha
      mais de 5 min, marca HISTORICO_IMPORTADO; se V1 recente, CAPTURA_NOVA;
      se nao houver V1, mantem CAPTURA_NOVA (adaptador sem registro V1)
    """
    origem = origem_solicitada
    v1_uuid: Optional[str] = None

    if captura_id:
        try:
            v1_uuid = str(uuid.UUID(str(captura_id)))
        except (ValueError, TypeError, AttributeError):
            v1_uuid = None

    if origem == ORIGEM_REPROC or (origem_explicita and origem == ORIGEM_HISTORICO):
        return origem, v1_uuid

    # Com id V1 explicito de pos-INSERT: captura nova
    if v1_uuid and not origem_explicita:
        return ORIGEM_NOVA, v1_uuid
    if v1_uuid and origem == ORIGEM_NOVA:
        return ORIGEM_NOVA, v1_uuid

    # Sem captura_id: descobrir V1 por id_externo (adaptadores em UPSERT)
    if not v1_uuid and id_externo:
        cur.execute(
            """
            SELECT id::text, criado_em
              FROM public.obras
             WHERE id_externo = %s
               AND (
                    fonte = %s
                 OR (%s = 'EM_EXECUCAO' AND fonte LIKE 'bndes%%')
                 OR (%s LIKE 'aneel%%' AND fonte LIKE 'aneel%%')
                 OR (%s IN ('cvm_ipe','cvm') AND fonte LIKE 'cvm%%')
                 OR (%s IN ('antaq_tup','antaq') AND fonte LIKE 'antaq%%')
                 OR fonte = %s
               )
             ORDER BY
               CASE WHEN fonte = %s THEN 0 ELSE 1 END,
               criado_em ASC
             LIMIT 1
            """,
            (id_externo, fonte, fonte, fonte, fonte, fonte, fonte, fonte),
        )
        row = cur.fetchone()
        if row:
            v1_uuid = row[0]
            criado_em = row[1]
            # obra V1 antiga reenviada pelo adaptador ≠ captura nova
            if origem == ORIGEM_NOVA and not origem_explicita:
                cur.execute(
                    "SELECT (%s < now() - interval '5 minutes')",
                    (criado_em,),
                )
                is_old = bool(cur.fetchone()[0])
                if is_old:
                    origem = ORIGEM_HISTORICO

    return origem, v1_uuid


def processar_captura_para_master(
    fonte: str,
    captador: str,
    id_externo: str,
    payload: Any,
    contexto: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Processa uma captura real para wins_v2.

    contexto opcional:
      - captura_id / v1_obra_id
      - namespace (default 'default')
      - origem_marcador (CAPTURA_NOVA|HISTORICO_IMPORTADO|REPROCESSAMENTO)
      - connection (psycopg2 connection reutilizavel)
    """
    if not is_master_pipeline_enabled():
        return {"status": "DISABLED", "motivo": "MASTER_PIPELINE_V2_ENABLED=false"}

    if not fonte or not captador or not id_externo:
        raise ValueError("fonte, captador e id_externo sao obrigatorios")

    ctx = dict(contexto or {})
    namespace = str(ctx.get("namespace") or "default")
    origem_explicita = "origem_marcador" in ctx and ctx.get("origem_marcador") is not None
    origem = str(ctx.get("origem_marcador") or ORIGEM_NOVA)
    if origem not in {ORIGEM_HISTORICO, ORIGEM_NOVA, ORIGEM_REPROC}:
        origem = ORIGEM_NOVA
    captura_id = ctx.get("captura_id") or ctx.get("v1_obra_id")
    external_conn = ctx.get("connection")

    if not isinstance(payload, Mapping):
        payload_map: Dict[str, Any] = {"_raw": payload}
    else:
        payload_map = dict(payload)

    h = hash_payload(payload_map)

    own_conn = external_conn is None
    conn = external_conn or _connect()
    result: Dict[str, Any] = {
        "status": "OK",
        "fonte": fonte,
        "captador": captador,
        "id_externo": id_externo,
        "hash_payload": h,
        "duplicado": False,
        "versao": 1,
        "captura_bruta_id": None,
        "campos_preenchidos": 0,
        "entidades": 0,
        "publicado_v2": False,
    }

    try:
        with conn.cursor() as cur:
            fonte_id, captador_id = _ensure_fonte_captador(cur, fonte, captador)
            _ensure_campos_288(cur)

            origem, v1_resolved = _resolver_origem_marcador(
                cur,
                fonte=fonte,
                id_externo=id_externo,
                origem_solicitada=origem,
                captura_id=captura_id,
                origem_explicita=origem_explicita,
            )
            if v1_resolved and not captura_id:
                captura_id = v1_resolved
            result["origem_marcador"] = origem

            campos, evidencias, entidades = mapear_para_288(
                fonte,
                captador,
                id_externo,
                payload_map,
                captura_id=captura_id,
                origem_marcador=origem,
            )
            campos["hash_payload"] = h
            campos["hash_conteudo"] = h
            result["campos_preenchidos"] = sum(
                1 for v in campos.values() if v not in (None, "", [], {})
            )

            # Dedup por fonte+namespace+id_externo+hash
            cur.execute(
                """
                SELECT id, versao FROM wins_v2.capturas_brutas
                WHERE fonte_id = %s AND namespace = %s AND id_externo = %s AND hash_conteudo = %s
                LIMIT 1
                """,
                (fonte_id, namespace, id_externo, h),
            )
            existing = cur.fetchone()
            if existing:
                result.update(
                    {
                        "status": "DUPLICADO",
                        "duplicado": True,
                        "captura_bruta_id": str(existing[0]),
                        "versao": existing[1],
                    }
                )
                if own_conn:
                    conn.commit()
                return result

            # Versao: se mesmo id_externo com hash diferente, incrementa
            cur.execute(
                """
                SELECT id, versao, payload, hash_conteudo
                FROM wins_v2.capturas_brutas
                WHERE fonte_id = %s AND namespace = %s AND id_externo = %s
                ORDER BY versao DESC NULLS LAST, capturado_em DESC
                LIMIT 1
                """,
                (fonte_id, namespace, id_externo),
            )
            prev = cur.fetchone()
            versao = 1
            prev_id = None
            if prev:
                prev_id, prev_ver, prev_payload, prev_hash = prev
                versao = int(prev_ver or 1) + 1
                result["versao"] = versao

            v1_uuid = None
            if captura_id:
                try:
                    v1_uuid = str(uuid.UUID(str(captura_id)))
                except (ValueError, TypeError, AttributeError):
                    v1_uuid = None

            cur.execute(
                """
                INSERT INTO wins_v2.capturas_brutas (
                    captador_id, fonte_id, payload, id_externo, url_origem,
                    hash_conteudo, capturado_em, processado_em, versao_captador,
                    status, metadados, namespace, origem_marcador, v1_obra_id,
                    campos_canonicos, versao
                ) VALUES (
                    %s, %s, %s::jsonb, %s, %s,
                    %s, now(), now(), %s,
                    'normalizado', %s::jsonb, %s, %s, %s,
                    %s::jsonb, %s
                )
                RETURNING id
                """,
                (
                    captador_id,
                    fonte_id,
                    stable_json(payload_map),
                    id_externo,
                    campos.get("url_fonte"),
                    h,
                    "pipeline-v2",
                    stable_json(
                        {
                            "origem_marcador": origem,
                            "familia": family_of(fonte, captador),
                            "publicado_v2": False,
                            "portao_intocado": True,
                        }
                    ),
                    namespace,
                    origem,
                    v1_uuid,
                    stable_json(campos),
                    versao,
                ),
            )
            captura_bruta_id = cur.fetchone()[0]
            result["captura_bruta_id"] = str(captura_bruta_id)

            if prev_id and prev_hash and prev_hash != h:
                cur.execute(
                    """
                    INSERT INTO wins_v2.capturas_versoes (
                        captura_bruta_id, payload_anterior, payload_novo,
                        hash_anterior, hash_novo, motivo
                    ) VALUES (%s, %s::jsonb, %s::jsonb, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        captura_bruta_id,
                        stable_json(prev_payload) if prev_payload is not None else None,
                        stable_json(payload_map),
                        prev_hash,
                        h,
                        "payload_alterado",
                    ),
                )

            # valores_normalizados + evidencias
            for ev in evidencias:
                campo = ev["campo"]
                _ensure_campo(cur, campo, campo)
                val_norm = ev["valor_normalizado"]
                val_orig = ev["valor_original"]
                if isinstance(val_norm, (dict, list)):
                    val_norm = stable_json(val_norm)
                if isinstance(val_orig, (dict, list)):
                    val_orig = stable_json(val_orig)
                else:
                    val_orig = None if val_orig is None else str(val_orig)
                val_norm = None if val_norm is None else str(val_norm)

                cur.execute(
                    """
                    INSERT INTO wins_v2.valores_normalizados (
                        captura_bruta_id, campo_canonico_id, valor_original,
                        valor_normalizado, campo_origem, fonte_id, tipo_origem,
                        confianca, evidencia
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        captura_bruta_id,
                        campo,
                        val_orig,
                        val_norm,
                        ev["caminho"][:500],
                        fonte_id,
                        ev.get("tipo_origem") or "nativo",
                        ev.get("confianca") or 1.0,
                        stable_json(ev),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO wins_v2.evidencias_campos (
                        captura_bruta_id, campo_canonico_id, valor_extraido,
                        caminho_origem, transformacao, tipo_origem, confianca, fonte_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        captura_bruta_id,
                        campo,
                        val_norm,
                        ev["caminho"][:500],
                        "pipeline_v2",
                        ev.get("tipo_origem") or "nativo",
                        ev.get("confianca") or 1.0,
                        fonte_id,
                    ),
                )

            # Entidades + papeis + lookup
            entidades_ok = 0
            for ent in entidades:
                cnpj = ent.get("cnpj")
                if not cnpj or not is_valid_cnpj(cnpj):
                    continue
                nome = (ent.get("nome") or cnpj)[:500]
                papel = ent.get("papel") or "NAO_CLASSIFICADO"
                cur.execute(
                    """
                    INSERT INTO wins_v2.entidades (cnpj, nome, nome_original, tipo_pessoa, ativo)
                    VALUES (%s, %s, %s, 'JURIDICA', true)
                    ON CONFLICT (cnpj) DO UPDATE SET
                        nome = COALESCE(NULLIF(EXCLUDED.nome, ''), wins_v2.entidades.nome),
                        ativo = true
                    RETURNING id
                    """,
                    (cnpj, nome, nome),
                )
                ent_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO wins_v2.captura_entidades (
                        captura_bruta_id, entidade_id, papel, campo_origem, confianca, evidencia, ativo
                    ) VALUES (%s, %s, %s, %s, 1.0, %s::jsonb, true)
                    ON CONFLICT (captura_bruta_id, entidade_id, papel) DO UPDATE SET ativo = true
                    """,
                    (
                        captura_bruta_id,
                        ent_id,
                        papel,
                        ent.get("campo_cnpj"),
                        stable_json(ent),
                    ),
                )
                # entidades_lookup via funcao SECURITY DEFINER
                cur.execute(
                    """
                    SELECT wins_v2.upsert_entidade_lookup_minimo(
                        %s, %s, NULL, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        cnpj,
                        nome,
                        papel,
                        campos.get("municipio_obra"),
                        campos.get("uf_obra"),
                        fonte,
                        captador,
                    ),
                )
                entidades_ok += 1

            result["entidades"] = entidades_ok
            result["publicado_v2"] = False  # nunca publica

            # Marcar inbox relacionado como processado (se houver)
            if v1_uuid:
                cur.execute(
                    """
                    UPDATE wins_v2.pipeline_inbox
                       SET status = 'processado', processado_em = now()
                     WHERE v1_obra_id = %s AND status = 'pendente'
                    """,
                    (v1_uuid,),
                )

        if own_conn:
            conn.commit()
        return result
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def processar_captura_para_master_safe(
    fonte: str,
    captador: str,
    id_externo: str,
    payload: Any,
    contexto: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Wrapper a prova de falhas: nunca propaga excecao para a V1."""
    try:
        if not is_master_pipeline_enabled():
            return {"status": "DISABLED"}
        return processar_captura_para_master(fonte, captador, id_externo, payload, contexto)
    except Exception as exc:
        logger.exception(
            "pipeline V2 falhou (V1 preservada) fonte=%s captador=%s id_externo=%s",
            fonte,
            captador,
            id_externo,
        )
        registrar_falha(
            fonte=fonte,
            captador=captador,
            id_externo=id_externo,
            payload=payload,
            erro=repr(exc),
            contexto=contexto,
            namespace=str((contexto or {}).get("namespace") or "default"),
        )
        return {"status": "ERRO_ENFILEIRADO", "erro": str(exc)[:500]}


def apos_gravacao_v1(
    *,
    fonte: str,
    captador: Optional[str] = None,
    id_externo: str,
    payload: Any,
    captura_id: Any = None,
    contexto: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Ponto unico de integracao pos-INSERT V1."""
    ctx = dict(contexto or {})
    if captura_id is not None:
        ctx.setdefault("captura_id", captura_id)
        ctx.setdefault("v1_obra_id", captura_id)
    ctx.setdefault("origem_marcador", ORIGEM_NOVA)
    return processar_captura_para_master_safe(
        fonte=fonte,
        captador=captador or f"captar_{fonte}",
        id_externo=id_externo,
        payload=payload,
        contexto=ctx,
    )


__all__ = [
    "processar_captura_para_master",
    "processar_captura_para_master_safe",
    "apos_gravacao_v1",
    "is_master_pipeline_enabled",
    "mapear_para_288",
    "CAMPOS_288",
    "ORIGEM_HISTORICO",
    "ORIGEM_NOVA",
    "ORIGEM_REPROC",
]
