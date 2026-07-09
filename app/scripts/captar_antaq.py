"""
Captador ANTAQ v3 - le XLSX exportado manualmente do painel Aquarela.
Substitui versoes anteriores que tentavam URLs descontinuadas.

Fonte: https://aquarela.antaq.gov.br/single/?appid=5944b812-2cbd-4680-a7b6-004d0ce476dc
       (botao direito -> Exportar dados)

Arquivo esperado: /app/antaq_export.xlsx (ou path passado como argumento)

Atualizacao: manual quando William re-exportar do painel.
"""
import os, re, logging, sys
from datetime import datetime
import openpyxl
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


DEFAULT_PATH = "/app/antaq_export.xlsx"

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

CORTE_ANO_OBRA = 5

# Piso obra valida: ANTAQ TUP sem investimento >= R$10mi e cadastro regulatorio,
# nao obra de construcao/expansao (criterio definitivo 16/06/2026).
PISO_INVESTIMENTO = 100000


def find_col(headers, *needles):
    h_lower = [(i, h, (h or '').lower()) for i, h in enumerate(headers)]
    for needle in needles:
        for i, h, hl in h_lower:
            if needle in hl:
                return i
    return None


def normalizar_cnpj(s):
    if not s: return None
    s = re.sub(r'\D', '', str(s))
    if len(s) == 14: return s
    return None


def extrair_ano(s):
    """Extrai ano de TLO/Processo. Padroes tipicos:
       'TLO 11/2020-SOG', '50300.000674/2018-35', '50000.014545/2002'
    """
    if not s: return None
    s = str(s).strip()
    # Padrao /AAAA[-XX] (mais especifico - data do processo)
    m = re.search(r'/(\d{4})(?:[-/]\d|$)', s)
    if not m:
        # Padrao /AAAA generico
        m = re.search(r'/(\d{4})\b', s)
    if not m:
        # Fallback: qualquer ano de 4 digitos
        m = re.search(r'\b(\d{4})\b', s)
    if m:
        ano = int(m.group(1))
        if 1990 <= ano <= 2030:
            return ano
    return None


def calcular_score(ano_outorga, valor):
    score = 50
    if ano_outorga:
        idade = datetime.now().year - ano_outorga
        if idade <= 1: score += 25
        elif idade <= 3: score += 15
        elif idade <= 5: score += 8
    if valor:
        if valor >= 1e9: score += 15  # > 1Bi
        elif valor >= 1e8: score += 10  # > 100Mi
        elif valor >= 1e7: score += 5
    return min(score, 100)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    log.info(f"Lendo XLSX: {path}")

    if not os.path.exists(path):
        log.error(f"Arquivo nao encontrado: {path}")
        sys.exit(1)

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    log.info(f"  Sheet: {wb.sheetnames[0]}, dimensoes {ws.dimensions}")

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        log.error("Sheet vazia"); sys.exit(1)

    headers = rows[0]
    log.info(f"  Colunas ({len(headers)}): {headers}")

    # Mapeamento por substring (case-insensitive)
    idx_codigo  = find_col(headers, 'codigo do terminal', 'codterminal', 'codigo')
    idx_nome    = find_col(headers, 'nome do terminal', 'nominstalacao', 'nome', 'instalacao')
    idx_tipo    = find_col(headers, 'tipo do terminal', 'tipoinstalacao', 'tipo')
    idx_cnpj    = find_col(headers, 'cnpj')
    idx_uf      = find_col(headers, 'uf', 'estado')
    idx_municip = find_col(headers, 'municipio', 'cidade')
    idx_end     = find_col(headers, 'endereco', 'endereço', 'localizacao')
    idx_tlo     = find_col(headers, 'tlo')
    idx_outorga = find_col(headers, 'numero do processo', 'processo de outorga', 'outorga')
    idx_carga   = find_col(headers, 'carga')
    idx_invest  = find_col(headers, 'montante', 'investimento', 'valor')

    log.info("Mapeamento detectado:")
    log.info(f"  codigo    -> col {idx_codigo}")
    log.info(f"  nome      -> col {idx_nome}")
    log.info(f"  tipo      -> col {idx_tipo}")
    log.info(f"  cnpj      -> col {idx_cnpj}")
    log.info(f"  uf        -> col {idx_uf}")
    log.info(f"  municipio -> col {idx_municip}")
    log.info(f"  endereco  -> col {idx_end}")
    log.info(f"  tlo       -> col {idx_tlo}")
    log.info(f"  outorga   -> col {idx_outorga}")
    log.info(f"  carga     -> col {idx_carga}")
    log.info(f"  investim. -> col {idx_invest}")

    if idx_nome is None or idx_cnpj is None:
        log.error("Faltam colunas essenciais"); sys.exit(1)

    obras = []
    contagem_tipo = {}
    contagem_uf = {}
    sem_empresa = 0
    sem_data = 0
    ano_atual = datetime.now().year

    for r in rows[1:]:  # pula header
        # Pula linha de "Totais"
        codigo_val = r[idx_codigo] if idx_codigo is not None else None
        if codigo_val and str(codigo_val).strip().lower() == 'totais':
            continue

        nome = str(r[idx_nome]).strip() if r[idx_nome] else ''
        if not nome:
            sem_empresa += 1
            continue

        cnpj = normalizar_cnpj(r[idx_cnpj]) if idx_cnpj is not None and r[idx_cnpj] else None
        uf = str(r[idx_uf]).strip().upper()[:2] if idx_uf is not None and r[idx_uf] else None
        municipio = str(r[idx_municip]).strip() if idx_municip is not None and r[idx_municip] else None
        tipo = str(r[idx_tipo]).strip() if idx_tipo is not None and r[idx_tipo] else None
        codigo = str(codigo_val).strip() if codigo_val else None
        endereco = str(r[idx_end]).strip() if idx_end is not None and r[idx_end] else None
        tlo = str(r[idx_tlo]).strip() if idx_tlo is not None and r[idx_tlo] else None
        outorga = str(r[idx_outorga]).strip() if idx_outorga is not None and r[idx_outorga] else None
        carga = str(r[idx_carga]).strip() if idx_carga is not None and r[idx_carga] else None

        # Investimento - vem como float
        invest_raw = r[idx_invest] if idx_invest is not None else None
        valor = None
        valor_fmt = None
        if invest_raw and isinstance(invest_raw, (int, float)) and invest_raw > 0:
            valor = float(invest_raw)
            if valor >= 1e9:
                valor_fmt = f"R$ {valor/1e9:.1f} bi"
            elif valor >= 1e6:
                valor_fmt = f"R$ {valor/1e6:.0f} mi"
            elif valor >= 1e3:
                valor_fmt = f"R$ {valor/1e3:.0f} k"

        # Pula cadastros regulatorios sem investimento relevante (criterio obra valida)
        if valor is None or valor < PISO_INVESTIMENTO:
            continue

        # Ano de outorga - tenta extrair do numero do processo (tipo "50300.025962/2024-41")
        # ou do TLO (tipo "TLO 28/2018-SOG")
        ano = None
        if outorga:
            ano = extrair_ano(outorga)
        if ano is None and tlo:
            ano = extrair_ano(tlo)

        # Filtro de data: apenas a partir de 2025
        if ano is not None and ano < 2025:
            continue

        contagem_tipo[tipo or 'SEM_TIPO'] = contagem_tipo.get(tipo or 'SEM_TIPO', 0) + 1
        if uf: contagem_uf[uf] = contagem_uf.get(uf, 0) + 1

        # Outorga ANTAQ é permissão regulatória, NÃO sinal de operação efetiva.
        # PLANEJAMENTO evita misclass sistêmico (bug corrigido 18/05/2026).
        if ano is None:
            sem_data += 1
            fase_normalizada = 'PLANEJAMENTO'
            status_lic = 'Outorga sem data'
        elif (ano_atual - ano) <= CORTE_ANO_OBRA:
            fase_normalizada = 'EM_EXECUCAO'
            status_lic = f'Outorgada em {ano}'
        else:
            fase_normalizada = 'PLANEJAMENTO'
            status_lic = f'Outorgada em {ano}'

        # ID externo
        if codigo:
            id_externo = f"ANTAQ-{codigo}"
        elif cnpj:
            id_externo = f"ANTAQ-{cnpj}-{nome[:30].replace(' ', '_')}"
        else:
            id_externo = f"ANTAQ-{nome[:60].replace(' ', '_')}"

        score = calcular_score(ano, valor)

        # Empresa = nome do terminal (campo 'autorizataria' nao existe nesse export, mas o nome do terminal frequentemente eh do controlador)
        # Estrategia: usa nome como ate aparecer "VALE" "CARGILL" "ARCELORMITTAL" etc
        empresa = nome[:300]

        descricao_partes = ["Instalacao Portuaria ANTAQ"]
        if tipo: descricao_partes.append(f"Tipo: {tipo}")
        if ano: descricao_partes.append(f"Outorga: {ano}")
        if endereco: descricao_partes.append(f"End: {endereco[:100]}")
        if carga and carga != '-': descricao_partes.append(f"Carga: {carga[:200]}")
        if codigo: descricao_partes.append(f"Cod: {codigo}")
        descricao = " | ".join(descricao_partes)

        obras.append((
            id_externo[:200],
            nome[:500],
            empresa,
            cnpj,
            'PORTUARIO',
            municipio[:200] if municipio else None,
            uf if uf else None,
            valor,
            valor_fmt,
            fase_normalizada,
            status_lic[:200],
            2,  # urgencia: 2 = default importer (ver docs/issues/ISSUE-001)
            score,
            ['CIVIL_TECNICA', 'INSTALACOES'],
            descricao[:1000],
            "antaq_tup",
            "https://aquarela.antaq.gov.br/single/?appid=5944b812-2cbd-4680-a7b6-004d0ce476dc",
            None,
        ))

    log.info(f"=== ESTATISTICAS ===")
    log.info(f"  Sem empresa (descartados): {sem_empresa}")
    log.info(f"  Sem data outorga: {sem_data}")
    log.info(f"  Para inserir/upsert: {len(obras)}")
    log.info(f"  Top tipos:")
    for t, n in sorted(contagem_tipo.items(), key=lambda x: -x[1])[:10]:
        log.info(f"    {n:>4} | {t}")
    log.info(f"  Top UFs:")
    for u, n in sorted(contagem_uf.items(), key=lambda x: -x[1])[:10]:
        log.info(f"    {n:>4} | {u}")

    dedup = {}
    for obra in obras:
        dedup[obra[0]] = obra
    obras = list(dedup.values())
    log.info(f"  Apos dedup: {len(obras)}")
    _STATS["buscados"] = len(obras)

    if not obras:
        log.warning("Nenhuma obra para inserir."); return

    em_execucao = sum(1 for o in obras if o[9] == 'EM_EXECUCAO')
    com_valor = sum(1 for o in obras if o[7])
    log.info(f"  Obras EM_EXECUCAO (ult {CORTE_ANO_OBRA}a): {em_execucao}")
    log.info(f"  Obras OPERACAO: {len(obras) - em_execucao}")
    log.info(f"  Obras com valor de investimento: {com_valor}")

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
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
            valor_estimado = COALESCE(obras.valor_estimado, EXCLUDED.valor_estimado),
            valor_formatado = COALESCE(obras.valor_formatado, EXCLUDED.valor_formatado),
            fase = EXCLUDED.fase,
            status_licenca = EXCLUDED.status_licenca,
            descricao = EXCLUDED.descricao,
            lead_score = EXCLUDED.lead_score,
            urgencia = EXCLUDED.urgencia,
            necessidades = EXCLUDED.necessidades
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, obras)
        log.info(f"  UPSERT: {cur.rowcount} linhas afetadas")
        _STATS["novos"] = cur.rowcount
    conn.commit()
    conn.close()
    log.info("=== FIM ANTAQ ===")


if __name__ == "__main__":
    main()
