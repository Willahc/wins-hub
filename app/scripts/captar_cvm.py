"""
Captador CVM - Fato Relevante / Comunicado ao Mercado de Cias Abertas.
Fonte: https://dados.cvm.gov.br/dataset/cia_aberta-doc-ipe
ZIP por ano: ipe_cia_aberta_AAAA.zip

Estrategia: filtra documentos por categoria + palavras-chave no Assunto
indicando investimento/obra/expansao. Resultado tem ruido inicial - 
revisao manual recomendada antes de promover pra produto final.

Filtros aplicados:
- Categorias relevantes: Fato Relevante, Comunicado ao Mercado, Aviso aos Acionistas
- Palavras-chave OBRA no Assunto: invest, expans, ampli, construc, nova planta,
  nova fabrica, retrofit, modernizac, fabril, capex
- Blacklist: capital social, recompra, dividend, divida, debenture, oferta 
  publica, distribu (exceto se tiver palavras de obra fortes)
"""
import os, re, csv, io, zipfile, sys, logging, urllib3
from datetime import datetime, date
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


ANO_ATUAL = datetime.now().year
URL_TEMPLATE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip"

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Palavras-chave que indicam obra real (peso alto)
PALAVRAS_OBRA_FORTES = [
    # Plantas/fabricas
    'expansao fabril', 'expansao da planta', 'expansao da unidade', 'expansao da fabrica',
    'nova fabrica', 'nova planta', 'nova unidade industrial', 'nova unidade fabril',
    'novo complexo', 'novo complexo industrial',
    'construcao de planta', 'construcao da fabrica', 'construcao de unidade',
    'modernizacao da planta', 'modernizacao fabril', 'retrofit',
    'projeto greenfield', 'projeto brownfield', 'duplicacao de capacidade',
    'ampliacao da capacidade produtiva', 'expansao da capacidade produtiva',
    'investimento em ativos', 'investimento em planta', 'planta industrial',
    # Capex / programa de investimento
    'capex', 'plano de investimento', 'plano de capex', 'guidance de capex',
    'aprovacao de investimento', 'aprovou investimento', 'aprovou novos investimentos',
    'aprova investimento', 'aprovou o investimento',
    # Energia (eolica, solar, hidreletrica, termoeletrica)
    'usina hidreletrica', 'usina solar', 'usina eolica', 'usina termeletrica',
    'parque eolico', 'parque solar', 'complexo eolico', 'complexo solar',
    'novo parque', 'nova usina',
    # Mineracao / petroleo / oleo e gas
    'nova mina', 'expansao da mina', 'projeto de mineracao', 'mineracao greenfield',
    'descoberta de hidrocarbonetos', 'pre-sal', 'pré-sal', 'pocos de petroleo',
    'novo poco', 'novos pocos', 'aguas profundas',
    # Portos / logistica
    'novo terminal', 'expansao do terminal', 'construcao do terminal',
    'porto greenfield',
    # Aquisicao de ativos produtivos (NAO de empresa)
    'aquisicao de planta', 'aquisicao de fabrica', 'aquisicao de ativos industriais',
    'aquisicao de unidade fabril', 'aquisicao de mina',
    # Obras civis
    'inicio das obras', 'inicio da construcao', 'conclusao das obras',
    'inauguracao da planta', 'inauguracao da fabrica', 'inauguracao da unidade',
]

# Palavras-chave fracas (precisam contexto industrial pra valer)
PALAVRAS_OBRA_FRACAS = [
    # 'nova' e 'investimento' sozinhos foram REMOVIDOS pois geravam muito
    # ruido (nova diretoria, fundo de investimento, etc).
    # Agora so qualifica termos com contexto industrial/produtivo:
    'expansao', 'ampliacao', 'construcao', 'modernizacao',
    'unidade fabril', 'fabrica de', 'planta de', 'planta industrial',
    'complexo industrial', 'complexo logistico', 'parque industrial',
    'obras civis', 'obra civil', 'instalacoes industriais',
]

# Blacklist: se aparecer, descarta a menos que tenha palavra forte
BLACKLIST = [
    # Capital / financeiro
    'capital social', 'aumento de capital', 'reducao de capital',
    'recompra de acoes', 'recompra de açoes', 'plano de recompra',
    'dividend', 'jcp', 'juros sobre capital',
    'oferta publica', 'oferta pública', 'distribuicao publica',
    'oferta restrita', 'oferta primaria', 'oferta secundaria',
    'desdobramento de acoes', 'grupamento',
    'debenture', 'debênture', 'cri ', 'cra ', 'eurobond',
    # Governanca / Cargos / Eleicoes
    'aprovacao das contas', 'aprovação das contas',
    'eleicao de membros', 'eleição',
    'remuneracao dos administradores', 'remuneração',
    'politica de', 'política de', 'codigo de conduta',
    'criterios de divulgacao', 'critérios',
    'acordo de acionistas', 'acordo entre acionistas',
    'estatuto social', 'reforma estatutaria',
    'convocacao', 'convocação', 'edital de convocacao',
    'parcial reapresent', 'reapresentacao',
    # NOVOS - Governanca/diretoria
    'nova diretoria', 'nova administracao', 'nova administração',
    'novo presidente', 'novo diretor', 'novo conselho',
    'renuncia', 'renúncia', 'eleicao do conselho',
    'composicao do conselho', 'composição do conselho',
    'mudanca na administracao', 'mudança na administração',
    # NOVOS - M&A financeiro (nao operacional)
    'fundo de investimento', 'fundos de investimento',
    'memorando de entendimento', 'protocolo de intencoes',
    'termo de compromisso', 'novo termo',
    'aquisicao de participacao', 'aquisição de participação',
    'venda de participacao', 'cessao de cotas',
    'oferta de aquisicao', 'oferta de aquisição',
    # NOVOS - Calendario / divulgacao
    'data divulgacao', 'data divulgação', 'nova data',
    'data da assembleia', 'data de assembleia',
    'data da reuniao', 'data da reunião',
    'cronograma de divulgacao', 'agenda',
    # NOVOS - Reapresentacoes / esclarecimentos
    'esclarecimentos', 'esclarecimento sobre',
    'consulta sobre', 'comentarios sobre', 'comentários sobre',
    'correspondencia', 'correspondência',
    'resposta ao oficio', 'resposta a consulta',
    # NOVOS - Programas e regulamentos (nao obra)
    'novo regulamento', 'novo programa de remuneracao',
    'plano de outorga de opcoes', 'plano de outorga de opções',
    'novo plano de remuneracao',
    # NOVOS - Diversos administrativos
    'aprovacao do orcamento', 'aprovação do orçamento',
    'orcamento de capital', 'orçamento de capital',
]

CATEGORIAS_INTERESSANTES = {
    'Fato Relevante', 
    'Comunicado ao Mercado',
    'Aviso aos Acionistas',
}


def normalizar_str(s):
    """Lower + remove acentos basicos pra match."""
    if not s: return ''
    import unicodedata
    s = unicodedata.normalize('NFKD', str(s)).encode('ASCII', 'ignore').decode('ASCII')
    return s.lower().strip()


def normalizar_cnpj(s):
    if not s: return None
    s = re.sub(r'\D', '', str(s))
    if len(s) == 14: return s
    return None


def find_col(headers, *needles):
    h_lower = [(i, h, (h or '').lower()) for i, h in enumerate(headers)]
    for needle in needles:
        for i, h, hl in h_lower:
            if needle in hl:
                return h
    return None


def avaliar_assunto(assunto):
    """Retorna (relevante, score, motivo).
    
    Logica:
      1. Se tem palavra forte -> relevante (score 75+)
      2. Se tem palavra fraca + nao tem blacklist -> relevante (score 55)  
      3. Se tem palavra fraca + tem blacklist -> nao relevante
      4. Se nao tem palavra de obra -> nao relevante
    """
    if not assunto: return (False, 0, "vazio")
    n = normalizar_str(assunto)
    
    # 1. Palavras fortes (maximo peso)
    for p in PALAVRAS_OBRA_FORTES:
        if p in n:
            return (True, 80, f"forte:{p}")
    
    # 2. Verifica blacklist primeiro
    bl_hits = [b for b in BLACKLIST if b in n]
    
    # 3. Palavras fracas
    fracas_hits = [p for p in PALAVRAS_OBRA_FRACAS if p in n]
    
    if not fracas_hits:
        return (False, 0, "sem_palavras")
    
    if bl_hits:
        return (False, 0, f"blacklist:{bl_hits[0]}")
    
    # Palavra fraca pura - score medio
    return (True, 55, f"fraca:{fracas_hits[0]}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    
    # Aceita lista de anos como argumento, default = ano corrente
    args = sys.argv[1:] if len(sys.argv) > 1 else [str(ANO_ATUAL)]
    anos = []
    for a in args:
        try: anos.append(int(a))
        except: pass
    if not anos:
        anos = [ANO_ATUAL]
    log.info(f"Anos a processar: {anos}")
    
    todas_obras = []
    contagem_categoria = {}
    contagem_motivo = {}
    sem_match = 0
    total_lidos = 0
    
    for ano in anos:
        url = URL_TEMPLATE.format(ano=ano)
        log.info(f"Baixando CVM IPE {ano}: {url}")
        try:
            r = requests.get(url, timeout=300, verify=False)
            r.raise_for_status()
        except Exception as e:
            log.error(f"  FALHA download {ano}: {e}")
            continue
        log.info(f"  baixado: {len(r.content):,} bytes")
        
        try:
            z = zipfile.ZipFile(io.BytesIO(r.content))
        except Exception as e:
            log.error(f"  FALHA zip {ano}: {e}")
            continue
        
        csv_names = [n for n in z.namelist() if n.lower().endswith('.csv')]
        log.info(f"  arquivos CSV no zip: {csv_names}")
        if not csv_names:
            log.error("  Sem CSV no zip"); continue
        
        for csv_name in csv_names:
            log.info(f"  processando {csv_name}")
            raw = z.read(csv_name)
            text = None
            for enc in ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']:
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                log.error(f"    nao decodificou"); continue
            
            sample = text[:5000]
            delim = ';' if sample.count(';') > sample.count(',') else ','
            
            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            headers = reader.fieldnames
            log.info(f"    delimitador '{delim}', colunas: {headers}")
            
            col_cnpj = find_col(headers, 'cnpj_companhia', 'cnpj')
            col_nome = find_col(headers, 'nome_companhia', 'nome')
            col_cat  = find_col(headers, 'categoria')
            col_tipo = find_col(headers, 'tipo')
            col_assunto = find_col(headers, 'assunto')
            col_data = find_col(headers, 'data_entrega', 'data_referencia', 'data')
            col_link = find_col(headers, 'link_download', 'link', 'url')
            
            log.info(f"    map: cnpj={col_cnpj} nome={col_nome} cat={col_cat} assunto={col_assunto} link={col_link}")
            
            if not col_assunto or not col_nome:
                log.error("    faltam colunas essenciais (assunto, nome)"); continue
            
            for r in reader:
                total_lidos += 1
                cat = (r.get(col_cat) or '').strip() if col_cat else ''
                contagem_categoria[cat] = contagem_categoria.get(cat, 0) + 1
                
                # Filtro 1: categoria
                if cat not in CATEGORIAS_INTERESSANTES:
                    continue
                
                assunto = (r.get(col_assunto) or '').strip()
                relevante, score, motivo = avaliar_assunto(assunto)
                contagem_motivo[motivo] = contagem_motivo.get(motivo, 0) + 1
                
                if not relevante:
                    sem_match += 1
                    continue
                
                # Coleta dados
                cnpj = normalizar_cnpj(r.get(col_cnpj)) if col_cnpj else None
                nome = (r.get(col_nome) or '').strip()
                if not nome: continue
                
                tipo = (r.get(col_tipo) or '').strip() if col_tipo else None
                data_str = (r.get(col_data) or '').strip() if col_data else None
                link = (r.get(col_link) or '').strip() if col_link else None
                
                # Parse data (formato pode ser AAAA-MM-DD ou DD/MM/AAAA)
                data_pub = None
                if data_str:
                    for fmt in ['%Y-%m-%d', '%d/%m/%Y', '%Y/%m/%d']:
                        try:
                            data_pub = datetime.strptime(data_str[:10], fmt).date()
                            break
                        except ValueError: continue
                
                # ID unico: empresa + data + assunto[:50]
                id_base = f"{cnpj or nome[:20]}-{data_str or 'sd'}-{assunto[:50]}"
                id_externo = f"CVM-{re.sub(r'[^A-Za-z0-9]', '_', id_base)[:180]}"
                
                descricao_partes = [
                    f"CVM IPE - {cat}",
                    f"Assunto: {assunto[:300]}",
                ]
                if tipo: descricao_partes.append(f"Tipo: {tipo}")
                if motivo: descricao_partes.append(f"Match: {motivo}")
                descricao = " | ".join(descricao_partes)
                
                todas_obras.append((
                    id_externo[:200],
                    assunto[:500] if assunto else nome[:500],  # usa assunto como nome da obra
                    nome[:300],  # razao social como empresa
                    cnpj,
                    'INDUSTRIAL',  # generico - usuario reclassifica
                    None,  # municipio: nao tem
                    None,  # uf: nao tem
                    None,  # valor: nao tem
                    None,
                    'EM_EXECUCAO',
                    f"Anuncio CVM - {motivo}"[:200],
                    2,  # urgencia: 2 = default importer (ver docs/issues/ISSUE-001)
                    score,
                    ['CIVIL_TECNICA'],
                    descricao[:1000],
                    "cvm_ipe",
                    link[:500] if link else "https://dados.cvm.gov.br/dataset/cia_aberta-doc-ipe",
                    data_pub,
                ))
    
    log.info(f"=== ESTATISTICAS ===")
    log.info(f"  Total documentos lidos: {total_lidos}")
    log.info(f"  Sem match (descartados): {sem_match}")
    log.info(f"  Para inserir/upsert: {len(todas_obras)}")
    log.info(f"  Top categorias:")
    for c, n in sorted(contagem_categoria.items(), key=lambda x: -x[1])[:10]:
        log.info(f"    {n:>6} | {c}")
    log.info(f"  Motivos de match (Top 15):")
    for m, n in sorted(contagem_motivo.items(), key=lambda x: -x[1])[:15]:
        log.info(f"    {n:>6} | {m}")
    
    # Dedup
    dedup = {}
    for obra in todas_obras:
        dedup[obra[0]] = obra
    obras = list(dedup.values())
    log.info(f"  Apos dedup: {len(obras)}")
    _STATS["buscados"] = len(obras)
    
    if not obras:
        log.warning("Nenhuma obra para inserir."); return
    
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    sql = """
        INSERT INTO obras (
            id_externo, nome, empresa, cnpj, setor, municipio, uf,
            valor_estimado, valor_formatado, fase, status_licenca,
            urgencia, lead_score, necessidades, descricao, fonte, url_fonte, data_publicacao
        ) VALUES %s
        ON CONFLICT (id_externo) DO UPDATE SET
            empresa = COALESCE(obras.empresa, EXCLUDED.empresa),
            cnpj = COALESCE(obras.cnpj, EXCLUDED.cnpj),
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
    log.info("=== FIM CVM ===")


if __name__ == "__main__":
    main()
