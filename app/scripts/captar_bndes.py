"""
Captacao de obras do BNDES (Operacoes de Financiamento - Nao Automaticas).
URL: https://dadosabertos.bndes.gov.br/dataset/operacoes-financiamento

Filtros:
- situacao_do_contrato = 'ATIVO'
- data_da_contratacao >= 2020-01-01
- valor_contratado_reais >= R$ 1 Mi
- Exclui clientes bancarios (BNDES repassando)
- Exclui descricoes de mercado de capitais
- Dedup por (cnpj, descricao) mantendo MAIOR valor

Enriquecimento:
- Setor pelo subsetor_bndes
- Fase: EM_EXECUCAO (BNDES so financia obra ja em curso)
- Local: vem direto do CSV (uf + municipio)
"""
import argparse
import os
import csv
import io
import logging
import re
import requests
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

URL_BNDES = "https://dadosabertos.bndes.gov.br/dataset/10e21ad1-568e-45e5-a8af-43f2c05ef1a2/resource/6f56b78c-510f-44b6-8274-78a5b7e931f4/download/operacoes-financiamento-operacoes-nao-automaticas.csv"
URL_CONSULTA = "https://dadosabertos.bndes.gov.br/dataset/operacoes-financiamento"

DATA_MIN = '2020-01-01'
VALOR_MIN = 1_000_000

SUBSETOR_SETOR = {
    'ENERGIA ELÉTRICA': 'ENERGIA',
    'EXTRACAO DE PETROLEO E GAS': 'ENERGIA',
    'EXTRAÇÃO MINERAL': 'MINERACAO',
    'EXTRATIVA': 'MINERACAO',
    'QUÍMICA E PETROQUÍMICA': 'INDUSTRIAL',
    'ALIMENTO E BEBIDA': 'INDUSTRIAL',
    'MECÂNICA': 'INDUSTRIAL',
    'AGROPECUÁRIA': 'INDUSTRIAL',
    'METALURGIA E PRODUTOS': 'INDUSTRIAL',
    'MATERIAL DE TRANSPORTE': 'INDUSTRIAL',
    'CELULOSE E PAPEL': 'INDUSTRIAL',
    'TÊXTIL E CONFECÇÃO': 'INDUSTRIAL',
    'TÊXTIL E VESTUÁRIO': 'INDUSTRIAL',
    'COMPL. ELETRO-ELETRÔNICO': 'INDUSTRIAL',
    'OUTROS TRANSPORTES': 'LOGISTICO',
    'ATV. AUX. TRANSPORTES': 'LOGISTICO',
    'TRANSPORTE RODOVIÁRIO': 'LOGISTICO',
    'TRANSPORTE FERROVIÁRIO': 'LOGISTICO',
    'CONSTRUÇÃO': 'INFRAESTRUTURA',
    'SERV. UTILIDADE PÚBLICA': 'INFRAESTRUTURA',
    'TELECOMUNICAÇÕES': 'INFRAESTRUTURA',
    'COMÉRCIO E SERVIÇOS': 'INDUSTRIAL',
    'OUTRAS': 'INDUSTRIAL',
}

EXCLUIR_CLIENTES = ['BANCO REGIONAL', 'BNDES', 'CAIXA ECONOMICA',
                    'BANCO DO NORDESTE', 'BANCO DA AMAZONIA',
                    'BANCO DO BRASIL']

EXCLUIR_DESCRICAO = ['AGENTE FINANCEIRO', 'FUNDO SETORIAL',
                     'APORTE DE CAPITAL', 'SUBSCRICAO DE DEBENTURES',
                     'AUMENTO DE CAPITAL', 'EMISSAO DE DEBENTURES',
                     'CAPITAL DE GIRO']

# Keywords saneamento V2 (16/05 noite v4) — descricao_do_projeto + subsetor_bndes ILIKE.
# Refinada após review dos 38 FP da V1: removido 'sanitário' (sozinho — bate em
# "aterro sanitário"), 'agua' (sem acento — bate em "aguardente"). Mantém só tokens
# que indicam infraestrutura de água/esgoto/saneamento básico.
KEYWORDS_SANEAMENTO = (
    'esgoto', 'esgotamento sanitário', 'esgotamento sanitario',
    'abastecimento de água', 'abastecimento de agua', 'abastecimento água', 'abastecimento agua',
    'adutora', 'adutor',
    'água potável', 'agua potavel', 'água potavel',
    'rede de água', 'rede de agua', 'rede de esgoto',
    'estação de tratamento', 'estacao de tratamento',
    ' eta ', ' ete ',
    'saneamento básico', 'saneamento basico',
    'drenagem urbana',
    'dessalinização', 'dessalinizacao',
    'tratamento de água', 'tratamento de agua', 'tratamento de esgoto',
    'recursos hídricos', 'recursos hidricos',
)

# Exclude saneamento — se qualquer keyword bater, descarta mesmo passando o INCLUDE.
# Corta FP comuns: biogás/biometano de aterro (matchearam por "aterro sanitário"),
# usinas/destilarias industriais com efluente, embalagem/IoT genéricos.
EXCLUDE_SANEAMENTO = (
    'aterro sanitário', 'aterro sanitario', 'aterro sanit',
    'biogás', 'biogas',
    'biometano',
    'destilaria',
    'açúcar e álcool', 'acucar e alcool',
    'usina de cana',
    'embalagem',
    ' iot ', 'internet das coisas',
    'tecnologia da informação',
    'resíduo sólido', 'residuo solido', 'resíduos sólidos', 'residuos solidos',
    'coleta de lixo', 'coleta seletiva',
)

SETOR_NECESSIDADES = {
    "ENERGIA": ["CIVIL_TECNICA", "ELETRICA_INDUSTRIAL", "TI_INFRAESTRUTURA"],
    "INFRAESTRUTURA": ["CIVIL_TECNICA", "TERRAPLANAGEM", "TOPOGRAFIA"],
    "MINERACAO": ["CIVIL_TECNICA", "TERRAPLANAGEM", "ELETRICA_INDUSTRIAL"],
    "LOGISTICO": ["CIVIL_TECNICA", "ESTRUTURA_METALICA"],
    "INDUSTRIAL": ["CIVIL_TECNICA", "ELETRICA_INDUSTRIAL", "HIDRAULICA"],
}


def parse_valor(valor_str):
    """Converte '9090000,0' para 9090000.0"""
    if not valor_str:
        return 0.0
    try:
        return float(valor_str.replace(',', '.'))
    except (ValueError, AttributeError):
        return 0.0


def filtrar(reg, saneamento_only: bool = False):
    if reg.get('situacao_do_contrato', '').strip() != 'ATIVO':
        return False
    data = reg.get('data_da_contratacao', '')
    if not data or data < DATA_MIN:
        return False
    if parse_valor(reg.get('valor_contratado_reais', '0')) < VALOR_MIN:
        return False
    cliente_upper = reg.get('cliente', '').upper()
    if any(bad in cliente_upper for bad in EXCLUIR_CLIENTES):
        return False
    desc_upper = reg.get('descricao_do_projeto', '').upper()
    if any(bad in desc_upper for bad in EXCLUIR_DESCRICAO):
        return False
    if saneamento_only:
        haystack = (reg.get('descricao_do_projeto', '') + ' '
                    + reg.get('subsetor_bndes', '')).lower()
        if not any(k in haystack for k in KEYWORDS_SANEAMENTO):
            return False
        # Exclude tem prioridade: mesmo se houver INCLUDE, qualquer exclude descarta
        if any(k in haystack for k in EXCLUDE_SANEAMENTO):
            return False
    return True


def calcular_lead_score(setor, valor, ano):
    score = 50
    if setor in ("ENERGIA", "MINERACAO"):
        score += 15
    elif setor == "INFRAESTRUTURA":
        score += 10
    if valor >= 1_000_000_000:
        score += 15  # bilionaria
    elif valor >= 100_000_000:
        score += 10
    if ano >= '2024':
        score += 10  # recente
    elif ano >= '2022':
        score += 5
    return min(score, 100)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--saneamento", action="store_true",
                        help="Filtra por keywords de saneamento e usa fonte=bndes_saneamento")
    parser.add_argument("--dry", action="store_true", help="Não persiste; mostra amostra")
    args = parser.parse_args()

    fonte = "bndes_saneamento" if args.saneamento else "bndes_financiamento"
    log.info("Modo: %s | dry=%s", fonte, args.dry)
    log.info(f"Baixando BNDES: {URL_BNDES}")
    r = requests.get(URL_BNDES, timeout=180)
    r.raise_for_status()
    log.info(f"  baixados {len(r.content)} bytes")

    # Parse CSV (encoding latin-1)
    text = r.content.decode('latin-1')
    reader = csv.DictReader(io.StringIO(text), delimiter=';')
    rows = list(reader)
    log.info(f"  total registros: {len(rows)}")

    # Filtra
    aceitos = [r for r in rows if filtrar(r, saneamento_only=args.saneamento)]
    log.info(f"  apos filtro: {len(aceitos)}")

    # Dedup por (cnpj, descricao) mantendo MAIOR valor
    dedup = {}
    for reg in aceitos:
        cnpj = reg.get('cnpj', '').strip()
        desc = reg.get('descricao_do_projeto', '').strip()[:200]
        key = (cnpj, desc)
        valor = parse_valor(reg.get('valor_contratado_reais', '0'))
        if key not in dedup:
            dedup[key] = reg
        else:
            existing = parse_valor(dedup[key].get('valor_contratado_reais', '0'))
            if valor > existing:
                dedup[key] = reg
    aceitos_unicos = list(dedup.values())
    log.info(f"  apos dedup: {len(aceitos_unicos)}")

    # Transforma em obras
    obras_para_inserir = []
    for reg in aceitos_unicos:
        cliente = reg.get('cliente', '').strip()
        # CNPJ vem com pontuação no CSV BNDES — normaliza pra bater com
        # empresas_receita.cnpj (formato XXXXXXXXXXXXXX, 14 chars).
        cnpj = re.sub(r'[./-]', '', reg.get('cnpj', '').strip())
        desc = reg.get('descricao_do_projeto', '').strip()
        municipio = reg.get('municipio', '').strip()
        uf = reg.get('uf', '').strip()
        data_contrat = reg.get('data_da_contratacao', '')
        num_contrato = reg.get('numero_do_contrato', '').strip()
        sub = reg.get('subsetor_bndes', '').strip()

        valor = parse_valor(reg.get('valor_contratado_reais', '0'))
        setor = SUBSETOR_SETOR.get(sub, 'INDUSTRIAL')

        # Limpa "SEM MUNICÍPIO" e "DIVERSOS"
        if municipio.upper() in ('SEM MUNICÍPIO', 'DIVERSOS', '-', ''):
            municipio = None
        if uf == 'IE' or not uf or uf == '-':
            uf = None

        # id_externo: CNPJ + numero contrato.
        # Modo --saneamento usa prefixo distinto pra coexistir com a entrada do daily
        # (mesma operação BNDES, lente saneamento — segregada pra dashboards/matchmaking).
        if args.saneamento:
            id_externo = f"BNDES-SAN-{cnpj}-{num_contrato}"
        else:
            id_externo = f"BNDES-{cnpj}-{num_contrato}"

        # Valor formatado
        if valor >= 1_000_000_000:
            valor_fmt = f"R$ {valor/1_000_000_000:.1f} Bi"
        else:
            valor_fmt = f"R$ {valor/1_000_000:.0f} Mi"

        # Limita descricao do projeto pra nome
        nome_obra = desc[:300] if desc else f"Operacao BNDES {num_contrato}"

        ano = data_contrat[:4] if data_contrat else '2020'
        lead_score = calcular_lead_score(setor, valor, ano)

        # Urgencia: 1 = mais urgente (ver docs/issues/ISSUE-001).
        # >= R$ 500Mi = 1 (top); senão 2 (default importer).
        urgencia = 1 if valor >= 500_000_000 else 2

        descricao = (
            f"Subsetor BNDES: {sub} | "
            f"Produto: {reg.get('produto', '').strip()} | "
            f"Porte: {reg.get('porte_do_cliente', '').strip()} | "
            f"Contratacao: {data_contrat}"
        )

        necessidades = SETOR_NECESSIDADES.get(setor, ['CIVIL_TECNICA'])

        try:
            data_pub = data_contrat if data_contrat else None
        except Exception:
            data_pub = None

        obras_para_inserir.append((
            id_externo,
            nome_obra,
            cliente[:300] if cliente else None,
            cnpj if cnpj else None,
            setor,
            municipio,
            uf,
            valor,
            valor_fmt,
            'EM_EXECUCAO',  # BNDES financiou = obra em andamento
            'CONTRATO ATIVO',  # status_licenca
            urgencia,
            lead_score,
            necessidades,
            descricao[:1000],
            fonte,
            URL_CONSULTA,
            data_pub,
        ))

    log.info(f"  obras antes dedup id_externo: {len(obras_para_inserir)}")

    # Dedup por id_externo (mantem o de maior valor_estimado)
    dedup_id = {}
    for obra in obras_para_inserir:
        id_ext = obra[0]
        valor = obra[7]  # valor_estimado na tupla
        if id_ext not in dedup_id:
            dedup_id[id_ext] = obra
        else:
            existing_valor = dedup_id[id_ext][7]
            if valor and (not existing_valor or valor > existing_valor):
                dedup_id[id_ext] = obra
    obras_para_inserir = list(dedup_id.values())
    log.info(f"  obras a inserir (apos dedup id_externo): {len(obras_para_inserir)}")
    _STATS["buscados"] = len(obras_para_inserir)

    if args.dry:
        amostra = obras_para_inserir[:10]
        log.info("DRY — amostra dos %d primeiros (id_externo | nome[:60] | setor | uf | valor_fmt):", len(amostra))
        for o in amostra:
            log.info("  %s | %s | %s | %s | %s", o[0], (o[1] or '')[:60], o[4], o[6], o[8])
        log.info("DRY done — total que seria inserido: %d (fonte=%s)", len(obras_para_inserir), fonte)
        return

    # Conecta DB e UPSERT
    conn = psycopg2.connect(**DB_CONFIG)
    sql = """
        INSERT INTO obras (
            id_externo, nome, empresa, cnpj, setor, municipio, uf,
            valor_estimado, valor_formatado, fase, status_licenca,
            urgencia, lead_score, necessidades, descricao, fonte, url_fonte, data_publicacao
        ) VALUES %s
        ON CONFLICT (id_externo) DO UPDATE SET
            municipio = COALESCE(obras.municipio, EXCLUDED.municipio),
            uf = COALESCE(obras.uf, EXCLUDED.uf),
            setor = COALESCE(obras.setor, EXCLUDED.setor),
            empresa = COALESCE(obras.empresa, EXCLUDED.empresa),
            cnpj = COALESCE(obras.cnpj, EXCLUDED.cnpj),
            fase = COALESCE(obras.fase, EXCLUDED.fase),
            data_publicacao = COALESCE(obras.data_publicacao, EXCLUDED.data_publicacao),
            valor_estimado = EXCLUDED.valor_estimado,
            valor_formatado = EXCLUDED.valor_formatado,
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

    log.info("=== FIM BNDES ===")


if __name__ == "__main__":
    main()
