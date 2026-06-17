"""
Captacao de obras do IBAMA (SISLIC - Licencas Ambientais).
URL: https://dadosabertos.ibama.gov.br/dados/SISLIC/sislic-licencas.json

Filtros:
- Tipos de licenca: LP, LI, LIO, LO, Renovacoes, Prorrogacoes
- Vencimento >= hoje OU emissao nos ultimos 18 meses

Enriquecimento:
- Setor inferido pela tipologia
- Fase derivada do tipo de licenca
- Valor estimado por tipologia (medias de mercado)
- Municipio/UF via regex no campo empreendimento
"""
import os
import re
import json
import logging
import requests
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import execute_values

log = logging.getLogger(__name__)

# STATS_JSON — orquestrador parseia última linha pra contadores em log_captacao
import atexit as _atexit
import json as _stats_json
_STATS = {"buscados": 0, "novos": 0, "erros": 0}
def _emit_stats_json():
    try:
        print(f"STATS_JSON: {_stats_json.dumps(_STATS)}", flush=True)
    except Exception:
        pass
_atexit.register(_emit_stats_json)


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

URL_IBAMA = "https://dadosabertos.ibama.gov.br/dados/SISLIC/sislic-licencas.json"
URL_CONSULTA = "https://dadosabertos.ibama.gov.br/dados/SISLIC/sislic-licencas.html"

# Tipos de licenca a INCLUIR (oportunidade ativa)
TIPOS_INCLUIR = {
    "Licenca Previa", "Licenca Previa para Perfuracao",
    "Licenca Previa de Producao para Pesquisa",
    "Prorrogacao de Licenca Previa",
    "Renovacao de Licenca Previa para Perfuracao",
    "Licenca de Instalacao", "Licenca de Instalacao e Operacao",
    "Prorrogacao de Licenca de Instalacao",
    "Renovacao de Licenca de Instalacao",
    "Licenca de Operacao", "Renovacao de Licenca de Operacao",
    "Licenca de Operacao - Regularizacao",
    "Renovacao de Licenca de Operacao - Regularizacao",
}

# Mapeamento Tipologia IBAMA -> Setor
TIPOLOGIA_SETOR = {
    "Estruturas Rodoviarias": "INFRAESTRUTURA",
    "Estruturas Ferroviarias": "INFRAESTRUTURA",
    "Cabo Optico": "INFRAESTRUTURA",
    "Sistema de Transmissao": "ENERGIA",
    "Usina Hidreletrica": "ENERGIA",
    "Pequena Central Hidreletrica": "ENERGIA",
    "Usina Termeletrica": "ENERGIA",
    "Petroleo e Gas - Producao": "ENERGIA",
    "Petroleo e Gas - Perfuracao": "ENERGIA",
    "Petroleo e Gas - Pesquisa Sismica": "ENERGIA",
    "Duto Terrestre": "ENERGIA",
    "Instalacao Nuclear/Radiativa": "ENERGIA",
    "Mineracao": "MINERACAO",
    "Transporte Hidroviario Maritimo": "LOGISTICO",
    "Usina Eolica": "ENERGIA",
    "Usina eolica offshore": "ENERGIA",
    "Usina fotovoltaica": "ENERGIA",
    "Outras Fontes de Geracao": "ENERGIA",
    "Sistema de Distribuicao": "ENERGIA",
    "Usina Termonuclear": "ENERGIA",
    "Central de Geracao Hidreletrica": "ENERGIA",
    "Petroleo e Gas - Onshore": "ENERGIA",
    "Mineroduto": "ENERGIA",
    "Antenas": "INFRAESTRUTURA",
    "Transposicao": "INFRAESTRUTURA",
    "Sistema de Abastecimento de Agua": "INFRAESTRUTURA",
    "Sistema de Esgotamento Sanitario": "INFRAESTRUTURA",
    "Aeroporto": "INFRAESTRUTURA",
    "Base Aeroespacial": "INFRAESTRUTURA",
    "Irrigacao": "INFRAESTRUTURA",
    "Transporte Hidroviario Fluvial": "LOGISTICO",
    "Posto de Abastecimento": "LOGISTICO",
    "Empreendimento agropecuario": "INDUSTRIAL",
    "Complexo turistico": "INDUSTRIAL",
}

# Mapeamento tipo licenca -> fase
LICENCA_FASE = {
    "Licenca Previa": "LICENCA_PREVIA",
    "Licenca Previa para Perfuracao": "LICENCA_PREVIA",
    "Licenca Previa de Producao para Pesquisa": "LICENCA_PREVIA",
    "Prorrogacao de Licenca Previa": "LICENCA_PREVIA",
    "Renovacao de Licenca Previa para Perfuracao": "LICENCA_PREVIA",
    "Licenca de Instalacao": "LICENCA_INSTALACAO",
    "Licenca de Instalacao e Operacao": "LICENCA_INSTALACAO",
    "Prorrogacao de Licenca de Instalacao": "LICENCA_INSTALACAO",
    "Renovacao de Licenca de Instalacao": "LICENCA_INSTALACAO",
    "Licenca de Operacao": "EM_EXECUCAO",
    "Renovacao de Licenca de Operacao": "EM_EXECUCAO",
    "Licenca de Operacao - Regularizacao": "EM_EXECUCAO",
    "Renovacao de Licenca de Operacao - Regularizacao": "EM_EXECUCAO",
}

# Estimativa de valor por tipologia (R$)
VALOR_ESTIMADO = {
    "Usina Hidreletrica": 5_000_000_000,
    "Pequena Central Hidreletrica": 500_000_000,
    "Usina Termeletrica": 2_000_000_000,
    "Sistema de Transmissao": 300_000_000,
    "Petroleo e Gas - Producao": 3_000_000_000,
    "Petroleo e Gas - Perfuracao": 800_000_000,
    "Petroleo e Gas - Pesquisa Sismica": 200_000_000,
    "Mineracao": 1_500_000_000,
    "Estruturas Rodoviarias": 250_000_000,
    "Estruturas Ferroviarias": 800_000_000,
    "Transporte Hidroviario Maritimo": 400_000_000,
    "Duto Terrestre": 600_000_000,
    "Instalacao Nuclear/Radiativa": 5_000_000_000,
    "Cabo Optico": 50_000_000,
    "Usina Eolica": 800_000_000,
    "Usina eolica offshore": 3_000_000_000,
    "Usina fotovoltaica": 400_000_000,
    "Outras Fontes de Geracao": 300_000_000,
    "Sistema de Distribuicao": 200_000_000,
    "Usina Termonuclear": 8_000_000_000,
    "Central de Geracao Hidreletrica": 200_000_000,
    "Petroleo e Gas - Onshore": 1_500_000_000,
    "Mineroduto": 800_000_000,
    "Antenas": 30_000_000,
    "Transposicao": 2_000_000_000,
    "Sistema de Abastecimento de Agua": 500_000_000,
    "Sistema de Esgotamento Sanitario": 600_000_000,
    "Aeroporto": 1_500_000_000,
    "Base Aeroespacial": 3_000_000_000,
    "Irrigacao": 400_000_000,
    "Transporte Hidroviario Fluvial": 300_000_000,
    "Posto de Abastecimento": 20_000_000,
    "Empreendimento agropecuario": 80_000_000,
    "Complexo turistico": 400_000_000,
}
VALOR_DEFAULT = 100_000_000  # fallback

# Necessidades inferidas por setor (alinhado com setor_categorias)
SETOR_NECESSIDADES = {
    "ENERGIA": ["CIVIL_TECNICA", "ELETRICA_INDUSTRIAL", "TI_INFRAESTRUTURA"],
    "INFRAESTRUTURA": ["CIVIL_TECNICA", "TERRAPLANAGEM", "TOPOGRAFIA"],
    "MINERACAO": ["CIVIL_TECNICA", "TERRAPLANAGEM", "ELETRICA_INDUSTRIAL"],
    "LOGISTICO": ["CIVIL_TECNICA", "ESTRUTURA_METALICA"],
    "INDUSTRIAL": ["CIVIL_TECNICA", "ELETRICA_INDUSTRIAL", "HIDRAULICA"],
}

# UFs brasileiras
UFS_BR = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
          "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
          "SP", "SE", "TO"}


def remover_acentos(texto):
    """Remove acentos pra matching."""
    if not texto:
        return ""
    mapa = str.maketrans(
        "aaaaaeeeeiiiooooouuuucnAAAAAEEEEIIIOOOOOUUUUCN",
        "ÀÁÂÃÄÈÉÊËÌÍÎÒÓÔÕÖÙÚÛÜçñÀÁÂÃÄÈÉÊËÌÍÎÒÓÔÕÖÙÚÛÜçÑ"
    )
    # invertemos: tira acento (substitui acentuado por sem acento)
    out = texto
    for c, sem in [
        ("á","a"),("à","a"),("â","a"),("ã","a"),("ä","a"),
        ("é","e"),("è","e"),("ê","e"),("ë","e"),
        ("í","i"),("ì","i"),("î","i"),("ï","i"),
        ("ó","o"),("ò","o"),("ô","o"),("õ","o"),("ö","o"),
        ("ú","u"),("ù","u"),("û","u"),("ü","u"),
        ("ç","c"),("ñ","n"),
        ("Á","A"),("À","A"),("Â","A"),("Ã","A"),
        ("É","E"),("Ê","E"),("Í","I"),("Ó","O"),("Ô","O"),
        ("Õ","O"),("Ú","U"),("Ç","C"),
    ]:
        out = out.replace(c, sem)
    return out


def parse_data(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y").date()
    except Exception:
        return None


def filtrar_obra(reg, hoje, limite_emissao):
    """Aplica filtro: tipo de licenca + vigencia/recencia."""
    tipo_orig = reg.get("desTipoLicenca", "")
    tipo_norm = remover_acentos(tipo_orig)

    if tipo_norm not in TIPOS_INCLUIR:
        return False

    venc = parse_data(reg.get("dataVencimento"))
    emiss = parse_data(reg.get("dataEmissao"))

    # Aceita se: vence no futuro OU foi emitida nos ultimos 18 meses
    if venc and venc >= hoje:
        return True
    if emiss and emiss >= limite_emissao:
        return True
    return False


def carregar_municipios(conn):
    """Carrega lista de municipios IBGE pra extracao via regex."""
    municipios = []
    with conn.cursor() as cur:
        cur.execute("SELECT nome, uf, codigo_ibge FROM municipios_ibge WHERE nome IS NOT NULL")
        for nome, uf, codigo in cur.fetchall():
            nome_norm = remover_acentos(nome).upper()
            municipios.append((nome_norm, nome, uf, codigo))
    # Ordena: maiores primeiro (evita match parcial errado tipo "Sao Paulo" virar "Sao Pa")
    municipios.sort(key=lambda x: -len(x[0]))
    return municipios


def extrair_local(empreendimento, municipios_idx):
    """
    Extrai (municipio, uf) de string tipo:
      'BR-163/MT'                       -> ('', 'MT')
      'UHE Corumba IV'                  -> ('Corumba', 'GO')
      'Trecho divisa SC/RS'             -> ('', 'SC')
      'Linha Aracruz-Vitoria'           -> ('Aracruz', 'ES')
    """
    if not empreendimento:
        return (None, None)

    texto = remover_acentos(empreendimento).upper()

    # 1. Tenta achar /UF (ex: BR-163/MT) ou -UF
    match_uf = re.search(r'[/-](' + '|'.join(UFS_BR) + r')\b', texto)
    uf_extraida = match_uf.group(1) if match_uf else None

    # 2. Tenta achar nome de municipio
    municipio_nome = None
    municipio_uf = None
    for nome_norm, nome_real, uf, _ in municipios_idx:
        # palavras curtas (<5 chars) sao perigosas - so aceita se for word boundary
        if len(nome_norm) < 5:
            if re.search(r'\b' + re.escape(nome_norm) + r'\b', texto):
                municipio_nome = nome_real
                municipio_uf = uf
                break
        else:
            if nome_norm in texto:
                municipio_nome = nome_real
                municipio_uf = uf
                break

    # Resolve conflito UF
    uf_final = uf_extraida or municipio_uf
    return (municipio_nome, uf_final)


def calcular_lead_score(setor, fase, tipologia, dataEmissao):
    """Score 0-100 indicando probabilidade de virar oportunidade real."""
    score = 50
    # Setor premium
    if setor in ("ENERGIA", "MINERACAO"):
        score += 15
    elif setor == "INFRAESTRUTURA":
        score += 10
    # Fase
    if fase == "LICENCA_INSTALACAO":
        score += 15  # ja tem alvara, vai construir
    elif fase == "LICENCA_PREVIA":
        score += 10
    # Tipologia premium
    if tipologia in ("Usina Hidreletrica", "Mineracao", "Petroleo e Gas - Producao"):
        score += 10
    # Recencia
    if dataEmissao:
        idade_meses = (datetime.now().date() - dataEmissao).days / 30
        if idade_meses <= 6:
            score += 10
        elif idade_meses <= 12:
            score += 5
    return min(score, 100)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # 1. Download
    log.info(f"Baixando IBAMA SISLIC: {URL_IBAMA}")
    r = requests.get(URL_IBAMA, timeout=120)
    r.raise_for_status()
    payload = r.json()
    registros = payload.get("data", payload if isinstance(payload, list) else [])
    log.info(f"  baixados {len(registros)} registros")

    # 2. Filtra
    hoje = datetime.now().date()
    limite_emissao = hoje - timedelta(days=18 * 30)
    aceitos = [r for r in registros if filtrar_obra(r, hoje, limite_emissao)]
    log.info(f"  apos filtro: {len(aceitos)} registros")

    # 3. Conecta DB e carrega municipios
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    municipios_idx = carregar_municipios(conn)
    log.info(f"  municipios IBGE em memoria: {len(municipios_idx)}")

    # 4. Transforma cada registro em obra
    obras_para_inserir = []
    com_municipio = com_uf = sem_local = 0

    for reg in aceitos:
        tipo_lic_norm = remover_acentos(reg.get("desTipoLicenca", ""))
        tipologia_norm = remover_acentos(reg.get("tipologia", ""))

        empreend = reg.get("empreendimento", "").strip()
        empresa = reg.get("nomePessoa", "").strip()
        num_processo = reg.get("numeroProcesso", "")
        num_licenca = reg.get("numLicenca", "")
        emissao = parse_data(reg.get("dataEmissao"))
        venc = parse_data(reg.get("dataVencimento"))
        is_pac = reg.get("pac", "No") == "Sim"

        # id_externo: usa numero do processo + numero da licenca
        id_externo = "IBAMA-" + num_processo + "-" + num_licenca

        setor = TIPOLOGIA_SETOR.get(tipologia_norm, "INDUSTRIAL")
        fase = LICENCA_FASE.get(tipo_lic_norm, "EM_EXECUCAO")
        valor = VALOR_ESTIMADO.get(tipologia_norm, VALOR_DEFAULT)

        # Valor formatado
        if valor >= 1_000_000_000:
            valor_fmt = f"R$ {valor/1_000_000_000:.1f} Bi"
        elif valor >= 1_000_000:
            valor_fmt = f"R$ {valor/1_000_000:.0f} Mi"
        else:
            valor_fmt = f"R$ {valor:,.0f}"

        # Local
        municipio, uf = extrair_local(empreend, municipios_idx)
        if municipio:
            com_municipio += 1
        if uf:
            com_uf += 1
        if not municipio and not uf:
            sem_local += 1

        # Urgencia: 1 = mais urgente (ver docs/issues/ISSUE-001).
        # PAC eleva para 1 (top); senão 2 (default importer).
        urgencia = 1 if is_pac else 2
        lead_score = calcular_lead_score(setor, fase, tipologia_norm, emissao)
        if is_pac:
            lead_score = min(lead_score + 5, 100)

        necessidades = SETOR_NECESSIDADES.get(setor, ["CIVIL_TECNICA"])

        nome_obra = empreend if empreend else f"Empreendimento {num_processo}"

        descricao_partes = [
            f"Tipo: {reg.get('desTipoLicenca', '')}",
            f"Tipologia: {reg.get('tipologia', '')}",
            f"Emissao: {reg.get('dataEmissao', 'n/d')}",
            f"Vencimento: {reg.get('dataVencimento', 'n/d')}",
        ]
        if is_pac:
            descricao_partes.append("PAC: Sim")
        descricao = " | ".join(descricao_partes)

        obras_para_inserir.append((
            id_externo,
            nome_obra[:500],
            empresa[:300] if empresa else None,
            None,  # cnpj - sera preenchido depois (opcional)
            setor,
            municipio,
            uf,
            valor,
            valor_fmt,
            fase,
            reg.get("desTipoLicenca", "")[:200],  # status_licenca
            urgencia,
            lead_score,
            necessidades,
            descricao[:1000],
            "ibama_sislic",
            URL_CONSULTA,
            emissao,
            "ESTIMATIVA_TIPOLOGIA",  # capex_fonte: valor e placeholder por tipologia (medias de mercado), NAO somar como CAPEX confirmado
        ))

    log.info(f"  com municipio: {com_municipio} | com UF: {com_uf} | sem local: {sem_local}")
    log.info(f"  obras antes de dedup: {len(obras_para_inserir)}")

    # Dedup por id_externo (mantem o mais recente por dataEmissao)
    dedup = {}
    for obra in obras_para_inserir:
        id_ext = obra[0]
        emissao = obra[17]  # data_publicacao na tupla
        if id_ext not in dedup:
            dedup[id_ext] = obra
        else:
            existing_emissao = dedup[id_ext][17]
            if emissao and (not existing_emissao or emissao > existing_emissao):
                dedup[id_ext] = obra
    obras_para_inserir = list(dedup.values())
    log.info(f"  obras apos dedup: {len(obras_para_inserir)}")
    _STATS["buscados"] = len(obras_para_inserir)

    # 5. UPSERT em obras (dedup por id_externo)
    sql = """
        INSERT INTO obras (
            id_externo, nome, empresa, cnpj, setor, municipio, uf,
            valor_estimado, valor_formatado, fase, status_licenca,
            urgencia, lead_score, necessidades, descricao, fonte, url_fonte, data_publicacao, capex_fonte
        ) VALUES %s
        ON CONFLICT (id_externo) DO UPDATE SET
            municipio = COALESCE(obras.municipio, EXCLUDED.municipio),
            uf = COALESCE(obras.uf, EXCLUDED.uf),
            setor = COALESCE(obras.setor, EXCLUDED.setor),
            empresa = COALESCE(obras.empresa, EXCLUDED.empresa),
            valor_estimado = COALESCE(obras.valor_estimado, EXCLUDED.valor_estimado),
            valor_formatado = COALESCE(obras.valor_formatado, EXCLUDED.valor_formatado),
            data_publicacao = COALESCE(obras.data_publicacao, EXCLUDED.data_publicacao),
            fase = EXCLUDED.fase,
            status_licenca = EXCLUDED.status_licenca,
            descricao = EXCLUDED.descricao,
            lead_score = EXCLUDED.lead_score,
            urgencia = EXCLUDED.urgencia,
            necessidades = EXCLUDED.necessidades
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, obras_para_inserir)
        log.info(f"  UPSERT executado: {cur.rowcount} linhas afetadas")
        _STATS["novos"] = cur.rowcount
    conn.commit()
    conn.close()

    log.info("=== FIM ===")


if __name__ == "__main__":
    main()
