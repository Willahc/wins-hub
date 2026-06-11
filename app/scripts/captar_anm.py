"""
Captador ANM (Mineração) - via CFEM_Arrecadacao.csv

Estrategia: stream do CSV (~200 MB) processando em chunks. Filtra ultimos 2 anos.
Agrupa por (CNPJ, ProcessoOriginal, Substancia, UF, Municipio) e cria 1 obra
por agrupamento, com soma do valor CFEM nos ultimos 12 meses.

Fonte: https://dadosabertos.anm.gov.br/CFEM/CFEM_Arrecadacao_2022_2026.csv
"""
import os, re, csv, io, sys, logging, urllib3
from datetime import datetime, date
from collections import defaultdict
import requests
import psycopg2
from psycopg2.extras import execute_values

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
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


URL_CFEM = "https://dadosabertos.anm.gov.br/CFEM/CFEM_Arrecadacao_2022_2026.csv"
ANO_CORTE = datetime.now().year - 2  # ultimos 2 anos

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def normalizar_cnpj(s):
    if not s: return None
    s = re.sub(r'\D', '', str(s))
    if len(s) == 14: return s
    if len(s) == 11: return None  # CPF, ignora
    return None


def _norm_col(s):
    """Lower + sem acento, pra comparar nomes de coluna."""
    import unicodedata
    if not s: return ''
    s = unicodedata.normalize('NFKD', str(s)).encode('ASCII', 'ignore').decode('ASCII')
    return s.lower()

def find_col(headers, *needles):
    for needle in needles:
        n_norm = _norm_col(needle)
        for i, h in enumerate(headers):
            if h and n_norm in _norm_col(h):
                return i, h
    return None, None


def parse_valor(s):
    if not s: return 0.0
    s = str(s).strip().replace('.', '').replace(',', '.')
    try: return float(s)
    except: return 0.0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    log.info(f"Baixando CFEM Arrecadacao (~200MB): {URL_CFEM}")
    log.info(f"Filtro: ano arrecadacao >= {ANO_CORTE}")

    # 11/06: download UNICO pro disco. O servidor ANM serve a ~0.9MB/s; segurar a
    # conexao HTTP aberta por ~8min durante o parse e fragil (gov dropa stream
    # longo). Baixa pro /tmp e processa local: mais resiliente + parse mais rapido
    # (1 csv.reader sobre o arquivo, em vez de csv.reader([line]) por linha).
    import tempfile
    tmp_path = os.path.join(tempfile.gettempdir(), "cfem_anm_arrecadacao.csv")
    _t_dl = datetime.now()
    r = requests.get(URL_CFEM, stream=True, timeout=600, verify=False)
    r.raise_for_status()
    _baixado = 0
    with open(tmp_path, "wb") as _f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            if chunk:
                _f.write(chunk)
                _baixado += len(chunk)
    r.close()
    log.info(f"  download: {_baixado/1e6:.0f}MB em {int((datetime.now()-_t_dl).total_seconds())}s -> {tmp_path}")

    # Detecta encoding/delimitador no head do arquivo
    with open(tmp_path, "rb") as _f:
        raw_head = _f.read(5000)
    text_head = None
    enc_used = "utf-8"
    for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            text_head = raw_head.decode(enc); enc_used = enc; break
        except UnicodeDecodeError:
            continue
    if not text_head:
        log.error("Nao decodificou inicio"); sys.exit(1)
    sample = text_head[:3000]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    primeiro_lf = text_head.find("\n")
    header_line = text_head[:primeiro_lf].strip().lstrip("\ufeff")
    headers = [h.strip().strip('"') for h in header_line.split(delim)]
    log.info(f"  encoding={enc_used} delimitador='{delim}' colunas ({len(headers)}): {headers}")

    # Mapeamento
    idx_ano,    n_ano    = find_col(headers, 'anoarrec', 'ano arrec', 'ano')
    idx_mes,    n_mes    = find_col(headers, 'mes')
    idx_cnpj,   n_cnpj   = find_col(headers, 'cpf_cnpj', 'cnpj')
    idx_titul,  n_titul  = find_col(headers, 'titular', 'nomedoexplorador', 'razao')
    idx_proc,   n_proc   = find_col(headers, 'processooriginal', 'processo')
    idx_subst,  n_subst  = find_col(headers, 'substancia')
    idx_uf,     n_uf     = find_col(headers, 'uf', 'estado')
    idx_munic,  n_munic  = find_col(headers, 'município', 'município', 'nomemunicipio')
    # se pegou CodigoMunicipio, descarta
    if n_munic and 'codigo' in _norm_col(n_munic):
        idx_munic, n_munic = None, None
    idx_valor,  n_valor  = find_col(headers, 'valorrecolhido', 'valor cfem', 'valor')
    
    log.info(f"  Mapeamento:")
    log.info(f"    ano={idx_ano}({n_ano}) cnpj={idx_cnpj}({n_cnpj}) titular={idx_titul}({n_titul})")
    log.info(f"    processo={idx_proc}({n_proc}) substancia={idx_subst}({n_subst})")
    log.info(f"    uf={idx_uf}({n_uf}) municipio={idx_munic}({n_munic}) valor={idx_valor}({n_valor})")

    if idx_cnpj is None or idx_subst is None:
        log.error("Faltam colunas essenciais (cnpj ou substancia)"); sys.exit(1)
    if idx_titul is None:
        log.warning("Coluna 'titular' nao existe nesse CSV - vamos resolver via empresas_receita")
        TITULAR_VIA_RECEITA = True
    else:
        TITULAR_VIA_RECEITA = False

    # Processa o arquivo local com UM csv.reader (antes: csv.reader([line]) por
    # linha + decode manual, em milhoes de linhas).
    log.info("Processando CSV local...")
    linhas_processadas = 0
    linhas_filtradas = 0
    sem_cnpj = 0

    agg = defaultdict(lambda: {
        'titular': None, 'cnpj': None, 'processo': None, 'substancia': None,
        'uf': None, 'municipio': None, 'valor_total': 0.0,
        'num_meses': 0, 'ultimo_ano': 0, 'primeiro_ano': 9999,
    })

    with open(tmp_path, "r", encoding=enc_used, errors="replace", newline="") as _fcsv:
        reader = csv.reader(_fcsv, delimiter=delim)
        try:
            next(reader)  # pula header
        except StopIteration:
            reader = iter(())
        for campos in reader:
            if len(campos) < len(headers):
                continue
            linhas_processadas += 1

            ano_str = campos[idx_ano].strip() if idx_ano is not None else ''
            ano_match = re.search(r'(\d{4})', ano_str)
            if not ano_match:
                continue
            ano = int(ano_match.group(1))
            if ano < ANO_CORTE:
                continue
            if ano > datetime.now().year + 1:
                continue
            linhas_filtradas += 1

            cnpj = normalizar_cnpj(campos[idx_cnpj])
            if not cnpj:
                sem_cnpj += 1
                continue

            if idx_titul is not None:
                titular = campos[idx_titul].strip() if campos[idx_titul] else ''
            else:
                titular = ''
            processo = campos[idx_proc].strip() if idx_proc is not None and campos[idx_proc] else ''
            substancia = campos[idx_subst].strip() if idx_subst is not None and campos[idx_subst] else ''
            uf = campos[idx_uf].strip().upper()[:2] if idx_uf is not None and campos[idx_uf] else ''
            municipio = campos[idx_munic].strip() if idx_munic is not None and campos[idx_munic] else ''
            valor = parse_valor(campos[idx_valor]) if idx_valor is not None else 0.0

            if not substancia:
                continue

            chave = (cnpj, processo, substancia.upper(), uf, municipio.upper())
            d = agg[chave]
            if not d['titular']:
                d['titular'] = titular[:300]
                d['cnpj'] = cnpj
                d['processo'] = processo
                d['substancia'] = substancia
                d['uf'] = uf if uf else None
                d['municipio'] = municipio
            d['valor_total'] += valor
            d['num_meses'] += 1
            d['ultimo_ano'] = max(d['ultimo_ano'], ano)
            d['primeiro_ano'] = min(d['primeiro_ano'], ano)

            if linhas_processadas % 200000 == 0:
                log.info(f"  ...{linhas_processadas:,} linhas, {linhas_filtradas:,} no periodo, "
                         f"{len(agg):,} agrupamentos")

    try:
        os.remove(tmp_path)
    except OSError:
        pass
    log.info(f"=== Stream completo ===")
    log.info(f"  Total linhas:    {linhas_processadas:,}")
    log.info(f"  Filtradas (>={ANO_CORTE}): {linhas_filtradas:,}")
    log.info(f"  Sem CNPJ:        {sem_cnpj:,}")
    log.info(f"  Agrupamentos:    {len(agg):,}")
    
    # Estatistica
    valor_total_global = sum(d['valor_total'] for d in agg.values())
    log.info(f"  Valor CFEM total no periodo: R$ {valor_total_global/1e9:.2f} bilhoes")
    
    # Top substancias
    by_subst = defaultdict(int)
    by_uf = defaultdict(int)
    for d in agg.values():
        by_subst[d['substancia']] += 1
        if d['uf']: by_uf[d['uf']] += 1
    
    log.info(f"  Top 15 substancias:")
    for s, n in sorted(by_subst.items(), key=lambda x: -x[1])[:15]:
        log.info(f"    {n:>5} | {s}")
    log.info(f"  Top 10 UFs:")
    for u, n in sorted(by_uf.items(), key=lambda x: -x[1])[:10]:
        log.info(f"    {n:>5} | {u}")
    
    # Filtra: so agrupamentos com valor > 0 (mineradoras realmente ativas)
    agg_com_valor = {k: v for k, v in agg.items() if v['valor_total'] > 0}
    log.info(f"  Com valor > 0: {len(agg_com_valor):,}")
    
    # Constroi obras
    ano_atual = datetime.now().year
    obras = []
    for chave, d in agg_com_valor.items():
        # ID externo: ANM-{cnpj}-{processo}-{substancia[:20]}
        proc_clean = re.sub(r'[^0-9A-Za-z]', '_', d['processo'])[:30] if d['processo'] else 'np'
        subs_clean = re.sub(r'[^A-Za-z]', '', d['substancia'])[:20]
        uf_clean = (d['uf'] or 'XX')[:2]
        muni_clean = re.sub(r'[^0-9A-Za-z]', '', d['municipio'] or 'sm')[:20]
        id_externo = f"ANM-{d['cnpj']}-{proc_clean}-{subs_clean}-{uf_clean}-{muni_clean}"[:200]
        
        # Lead score: 50 base + bonus por valor
        v = d['valor_total']
        score = 50
        if v >= 1e8: score += 30   # > 100M (Vale Carajas, Samarco etc)
        elif v >= 1e7: score += 20  # > 10M
        elif v >= 1e6: score += 10  # > 1M
        elif v >= 1e5: score += 5
        # Bonus se ainda atual (recolhimento ano atual ou anterior)
        if d['ultimo_ano'] >= ano_atual: score += 5
        
        # Valor formatado
        if v >= 1e9: vfmt = f"R$ {v/1e9:.1f} bi (CFEM 2a)"
        elif v >= 1e6: vfmt = f"R$ {v/1e6:.0f} mi (CFEM 2a)"
        elif v >= 1e3: vfmt = f"R$ {v/1e3:.0f}k (CFEM 2a)"
        else: vfmt = f"R$ {v:.0f} (CFEM 2a)"
        
        # Nome da "obra" = mineração de X em Y
        nome = f"Mineração de {d['substancia']} - {d['municipio']}/{d['uf']}"
        if d['processo']:
            nome += f" (Proc. {d['processo']})"
        nome = nome[:300]
        
        # Descricao
        desc = (
            f"Mineracao ativa - CFEM agregado {d['primeiro_ano']}-{d['ultimo_ano']} "
            f"({d['num_meses']} meses). Substancia: {d['substancia']}. "
            f"Processo ANM: {d['processo']}. "
            f"Total CFEM: R$ {v:,.2f}."
        )[:1000]
        
        obras.append((
            id_externo,
            nome,
            d['titular'],
            d['cnpj'],
            'MINERACAO',
            d['municipio'][:200] if d['municipio'] else None,
            d['uf'],
            v,  # valor_estimado = total CFEM no periodo (proxy de tamanho)
            vfmt,
            'OPERACAO',  # CFEM = produzindo
            f"Em produção - últ. CFEM {d['ultimo_ano']}",
            2,     # urgencia: 2 = default importer (ver docs/issues/ISSUE-001)
            min(score, 100),
            ['MANUTENCAO', 'OPEX', 'EQUIPAMENTOS'],
            desc,
            'anm_cfem',
            'https://dadosabertos.anm.gov.br/CFEM/CFEM_Arrecadacao_2022_2026.csv',
            None,  # data_publicacao
            'OFICIAL',
            'royalty_indicador',  # CFEM = pagamento de royalty, nao obra construcao; marca pra esconder do gargalo
        ))
    
    log.info(f"  Para inserir/upsert: {len(obras):,}")
    _STATS["buscados"] = len(obras)
    if not obras:
        log.warning("Nada pra inserir."); return
    
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    sql = """
        INSERT INTO obras (
            id_externo, nome, empresa, cnpj, setor, municipio, uf,
            valor_estimado, valor_formatado, fase, status_licenca,
            urgencia, lead_score, necessidades, descricao, fonte, url_fonte, data_publicacao,
            fonte_tipo, motivo_invisivel
        ) VALUES %s
        ON CONFLICT (id_externo) DO UPDATE SET
            empresa = COALESCE(obras.empresa, EXCLUDED.empresa),
            cnpj = COALESCE(obras.cnpj, EXCLUDED.cnpj),
            valor_estimado = EXCLUDED.valor_estimado,
            valor_formatado = EXCLUDED.valor_formatado,
            municipio = COALESCE(EXCLUDED.municipio, obras.municipio),
            uf = COALESCE(EXCLUDED.uf, obras.uf),
            descricao = EXCLUDED.descricao,
            lead_score = EXCLUDED.lead_score,
            status_licenca = EXCLUDED.status_licenca,
            urgencia = EXCLUDED.urgencia,
            necessidades = EXCLUDED.necessidades
    """
    with conn.cursor() as cur:
        # Insere em batches de 1000
        _rowcount_total = 0
        for i in range(0, len(obras), 1000):
            batch = obras[i:i+1000]
            execute_values(cur, sql, batch)
            _rowcount_total += cur.rowcount or 0
        _STATS["novos"] = _rowcount_total
        log.info(f"  UPSERT executado: {_rowcount_total} linhas afetadas")
    conn.commit()
    conn.close()
    log.info("=== FIM ANM CFEM ===")


if __name__ == "__main__":
    main()
