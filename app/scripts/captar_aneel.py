"""
Captador ANEEL v2 - SIGA (empreendimentos) + Agentes (CEG -> CNPJ/Empresa).
Combina 2 datasets via CEG pra trazer obras com empresa estruturada.

Datasets:
  SIGA:    siga-empreendimentos-geracao.csv (mensal, ~21k usinas)
  Agentes: agentes-geracao-energia-eletrica.csv (mensal, ~30k linhas)

Chave de join: CodCEG (SIGA) <-> CEG ou CodCEG (Agentes)
Cada usina pode ter N agentes (consorcio); pegamos o agente com maior participacao.
"""
import os, csv, re, logging, sys, io
from pathlib import Path
import requests
import certifi
import psycopg2
from psycopg2.extras import execute_values

# ANEEL serve cadeia SSL incompleta (só leaf, sem intermediate Sectigo R36).
# Workaround: bundle = certifi roots + intermediate baixado em sectigo_intermediate.pem.
# Reconstruído quando o intermediate é atualizado (mtime mais recente que o bundle).
_ANEEL_INTERMEDIATE = Path(__file__).parent / "sectigo_intermediate.pem"
_ANEEL_BUNDLE = Path(__file__).parent / "aneel_ca_bundle.pem"

def _aneel_verify_path() -> str:
    if not _ANEEL_INTERMEDIATE.exists():
        return certifi.where()
    if (not _ANEEL_BUNDLE.exists()
            or _ANEEL_BUNDLE.stat().st_mtime < _ANEEL_INTERMEDIATE.stat().st_mtime):
        _ANEEL_BUNDLE.write_text(
            Path(certifi.where()).read_text() + "\n" + _ANEEL_INTERMEDIATE.read_text()
        )
    return str(_ANEEL_BUNDLE)

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


URL_SIGA    = "https://dadosabertos.aneel.gov.br/dataset/6d90b77c-c5f5-4d81-bdec-7bc619494bb9/resource/11ec447d-698d-4ab8-977f-b424d5deee6a/download/siga-empreendimentos-geracao.csv"
URL_AGENTES = "https://dadosabertos.aneel.gov.br/dataset/283a0172-3966-49e7-ae45-d2885ad17b03/resource/20ef769f-a072-489d-9df4-c834529f8a78/download/agentes-geracao-energia-eletrica.csv"

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def baixar_csv(url, label):
    log.info(f"Baixando {label}: {url[:80]}...")
    r = requests.get(url, timeout=300, verify=_aneel_verify_path())
    r.raise_for_status()
    log.info(f"  baixado: {len(r.content):,} bytes")
    text = None
    for enc in ['utf-8-sig', 'utf-8', 'latin-1']:
        try:
            text = r.content.decode(enc)
            log.info(f"  encoding: {enc}")
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise RuntimeError(f"Nao consegui decodificar {label}")
    sample = text[:5000]
    delim = ';' if sample.count(';') > sample.count(',') else ','
    log.info(f"  delimitador: '{delim}'")
    return text, delim


def normalizar_potencia(s):
    if not s: return None
    s = str(s).replace('.', '').replace(',', '.').strip() if ',' in str(s) else str(s).strip()
    try: return float(s)
    except: return None


def normalizar_ceg(s):
    """CEG canonico - remove espacos."""
    if not s: return None
    return str(s).strip().replace(' ', '')


def find_col(headers, *needles):
    h_lower = [(i, h, h.lower()) for i, h in enumerate(headers)]
    for needle in needles:
        for i, h, hl in h_lower:
            if needle in hl:
                return h
    return None


def calcular_score(potencia_kw, fase):
    score = 50
    if potencia_kw:
        mw = potencia_kw / 1000
        if mw >= 100: score += 25
        elif mw >= 30: score += 15
        elif mw >= 5: score += 8
    if fase == 'EM_EXECUCAO': score += 15
    elif fase == 'PLANEJAMENTO': score += 10
    return min(score, 100)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # === FASE 1: Baixa Agentes e cria index CEG -> [empresa, cnpj, participacao] ===
    text_agentes, delim_a = baixar_csv(URL_AGENTES, "ANEEL Agentes")
    reader_a = csv.DictReader(io.StringIO(text_agentes), delimiter=delim_a)
    headers_a = reader_a.fieldnames
    log.info(f"  Agentes - colunas ({len(headers_a)}): {headers_a}")

    col_ceg_a       = find_col(headers_a, 'codceg', 'ceg')
    col_empresa_a   = find_col(headers_a, 'nomagente', 'nomempresa', 'agente', 'empresa', 'razaosocial')
    col_cnpj_a      = find_col(headers_a, 'numcnpj', 'cnpj')
    col_partic      = find_col(headers_a, 'numpercentparticipa', 'percentparticipa', 'participacao')

    log.info(f"  Agentes mapeamento:")
    log.info(f"    CEG       -> {col_ceg_a}")
    log.info(f"    Empresa   -> {col_empresa_a}")
    log.info(f"    CNPJ      -> {col_cnpj_a}")
    log.info(f"    Particip. -> {col_partic}")

    if not col_ceg_a or not col_empresa_a:
        log.error("Agentes: faltam colunas essenciais"); sys.exit(1)

    # CEG -> melhor agente (maior % participacao)
    agente_por_ceg = {}
    total_agentes = 0
    for r in reader_a:
        ceg = normalizar_ceg(r.get(col_ceg_a))
        empresa = (r.get(col_empresa_a) or '').strip()
        if not ceg or not empresa: continue
        cnpj = (r.get(col_cnpj_a) or '').strip() if col_cnpj_a else None
        partic_raw = (r.get(col_partic) or '0').strip() if col_partic else '0'
        try:
            partic = float(partic_raw.replace(',', '.'))
        except:
            partic = 0
        total_agentes += 1
        prev = agente_por_ceg.get(ceg)
        if prev is None or partic > prev[2]:
            agente_por_ceg[ceg] = (empresa, cnpj, partic)

    log.info(f"  Total linhas Agentes: {total_agentes}")
    log.info(f"  CEGs unicos com agente: {len(agente_por_ceg)}")

    # === FASE 2: Baixa SIGA e cruza ===
    text_siga, delim_s = baixar_csv(URL_SIGA, "ANEEL SIGA")
    reader_s = csv.DictReader(io.StringIO(text_siga), delimiter=delim_s)
    headers_s = reader_s.fieldnames
    log.info(f"  SIGA - colunas ({len(headers_s)}): {headers_s}")

    col_nome      = find_col(headers_s, 'nomempreendimento', 'empreendimento')
    col_tipo      = find_col(headers_s, 'sigtipogeracao', 'tipogeracao', 'siglafonte')
    col_fase      = find_col(headers_s, 'descfaseusina', 'fase_usina', 'fasesituacao', 'fase')
    col_potencia  = find_col(headers_s, 'mdapotenciaoutorgadakw', 'potenciaoutorgada')
    col_uf        = find_col(headers_s, 'sigufprincipal', 'uf')
    col_municipio = find_col(headers_s, 'dscmuninicpios', 'dscmunicipios', 'municipio')  # typo no portal
    col_data_op   = find_col(headers_s, 'datentradaoperacao', 'entradaoperacao')
    col_ceg_s     = find_col(headers_s, 'codceg')
    col_ceg_alt   = find_col(headers_s, 'idenucleoceg', 'nucleoceg')

    log.info(f"  SIGA mapeamento:")
    log.info(f"    nome      -> {col_nome}")
    log.info(f"    tipo      -> {col_tipo}")
    log.info(f"    fase      -> {col_fase}")
    log.info(f"    potencia  -> {col_potencia}")
    log.info(f"    uf        -> {col_uf}")
    log.info(f"    municipio -> {col_municipio}")
    log.info(f"    data_op   -> {col_data_op}")
    log.info(f"    CEG       -> {col_ceg_s} (nucleo: {col_ceg_alt})")

    if not col_nome or not col_ceg_s or not col_fase:
        log.error("SIGA: faltam colunas essenciais"); sys.exit(1)

    obras = []
    contagem_fase = {}
    sem_agente = 0
    skipped_operacao = 0

    for r in reader_s:
        nome = (r.get(col_nome) or '').strip()
        if not nome: continue
        ceg = normalizar_ceg(r.get(col_ceg_s))
        fase_aneel = (r.get(col_fase) or '').strip()
        contagem_fase[fase_aneel] = contagem_fase.get(fase_aneel, 0) + 1

        # Filtrar fases - queremos obras (planejada, construcao), nao operacao antiga
        fase_lower = fase_aneel.lower()
        fase_normalizada = None
        if 'construção' in fase_lower and 'não' in fase_lower:
            fase_normalizada = 'PLANEJAMENTO'
        elif 'construção' in fase_lower:
            fase_normalizada = 'EM_EXECUCAO'
        elif 'operação' in fase_lower:
            skipped_operacao += 1
            continue
        elif fase_aneel:
            fase_normalizada = 'PLANEJAMENTO'
        else:
            continue

        # Busca agente por CEG
        agente = agente_por_ceg.get(ceg) if ceg else None
        if agente is None:
            sem_agente += 1
            empresa = None
            cnpj = None
        else:
            empresa, cnpj, _ = agente

        tipo = (r.get(col_tipo) or '').strip().upper()
        uf = (r.get(col_uf) or '').strip() if col_uf else None
        municipio = (r.get(col_municipio) or '').strip() if col_municipio else None
        if municipio:
            # DscMuninicpios as vezes vem com lista separada por ;
            municipio = municipio.split(';')[0].strip()
        data_op = (r.get(col_data_op) or '').strip() if col_data_op else None
        potencia = normalizar_potencia(r.get(col_potencia)) if col_potencia else None

        id_externo = f"ANEEL-{ceg}" if ceg else f"ANEEL-{nome[:60]}"

        valor = None
        valor_fmt = None
        if potencia:
            valor = (potencia / 1000) * 4_000_000
            valor_fmt = f"R$ {valor/1e6:.0f} mi" if valor >= 1e6 else f"R$ {valor/1e3:.0f} k"

        score = calcular_score(potencia, fase_normalizada)

        descricao_partes = []
        if tipo: descricao_partes.append(f"Tipo: {tipo}")
        descricao_partes.append(f"Fase: {fase_aneel}")
        if potencia: descricao_partes.append(f"Potencia: {potencia/1000:.1f} MW")
        if data_op: descricao_partes.append(f"Operacao prevista: {data_op}")
        if ceg: descricao_partes.append(f"CEG: {ceg}")
        descricao = " | ".join(descricao_partes)

        obras.append((
            id_externo[:200],
            nome[:500],
            empresa[:300] if empresa else None,
            cnpj if cnpj else None,
            'ENERGIA',
            municipio[:200] if municipio else None,
            uf if uf else None,
            valor,
            valor_fmt,
            fase_normalizada,
            fase_aneel[:200],
            2,  # urgencia: 2 = default importer (ver docs/issues/ISSUE-001)
            score,
            ['CIVIL_TECNICA', 'ELETRICA'],
            descricao[:1000],
            "aneel_siga",
            "https://www.aneel.gov.br/siga",
            None,
        ))

    log.info(f"=== ESTATISTICAS ===")
    log.info(f"  Em operacao (descartados): {skipped_operacao}")
    log.info(f"  Sem agente cruzado: {sem_agente}")
    log.info(f"  Para inserir/upsert: {len(obras)}")
    log.info(f"  Top fases SIGA:")
    for f, n in sorted(contagem_fase.items(), key=lambda x: -x[1])[:8]:
        log.info(f"    {n:>6} | {f}")

    # Dedup
    dedup = {}
    for obra in obras:
        dedup[obra[0]] = obra
    obras = list(dedup.values())
    log.info(f"  Apos dedup: {len(obras)}")
    _STATS["buscados"] = len(obras)

    if not obras:
        log.warning("Nenhuma obra para inserir."); return

    com_empresa = sum(1 for o in obras if o[2])
    log.info(f"  Com empresa cruzada: {com_empresa}/{len(obras)} ({100*com_empresa/len(obras):.1f}%)")

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
    log.info("=== FIM ANEEL ===")


if __name__ == "__main__":
    main()
