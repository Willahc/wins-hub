"""
Captador CVM — Debentures de infraestrutura (Lei 12.431/2011).
Fonte: dados.cvm.gov.br/dataset/oferta-distrib (zip com 2 CSVs).

Filtro chave: Titulo_incentivado='S' em oferta_resolucao_160.csv.
Essas sao debentures que financiam OBRAS REAIS de infraestrutura.
Sao registradas/emitidas — debenture aprovada = obra com funding garantido.

Setor inferido por palavras do nome do emissor (energia, rodovia, saneamento, etc).
"""
import os, csv, io, zipfile, re, sys, logging
import requests
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

# STATS_JSON
import atexit as _atexit
import json as _stats_json
_STATS = {"buscados": 0, "novos": 0, "erros": 0}
def _emit_stats_json():
    try:
        print(f"STATS_JSON: {_stats_json.dumps(_STATS)}", flush=True)
    except Exception:
        pass
_atexit.register(_emit_stats_json)

URL_ZIP = "https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip"
ARQ_INTERNO = "oferta_resolucao_160.csv"
VALOR_MINIMO = 100_000_000  # R$100mi — corte conservador
ANO_MINIMO = 2022
FONTE = "debentures_infra"

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Setor heuristico por palavra no nome do emissor.
SETOR_KEYWORDS = [
    ("ENERGIA",        ["TRANSMISSAO", "TRANSMISSÃO", "ENERGIA", "ELETRICIDADE", "EOLIC", "SOLAR",
                        "HIDR", "GERACAO", "GERAÇÃO", "RENOVAVE", "ENEL", "NEOENERGIA", "ENGIE",
                        "COPEL", "CEMIG", "COELBA", "ENERGISA", "ELETROBRAS", "CHESF", "FURNAS",
                        "EQUATORIAL", "AES", "TAESA", "ISA CTEEP", "ALUPAR", "OMEGA", "AURA"]),
    ("INFRAESTRUTURA", ["RODOVIA", "RODOVIAS", "CONCESSIONARIA DAS RODOVIAS", "CONCESSIONÁRIA",
                        "AUTOBAN", "ECORODOVIAS", "ECOPISTAS", "ECOSUL", "ECONOROESTE",
                        "ARTERIS", "CCR", "MOTIVA", "RUMO", "MRS", "FERROVIA", "AEROPORT",
                        "PORTO", "TERMINAL", "VLI"]),
    ("SANEAMENTO",     ["SANEAMENTO", "AGUAS", "ÁGUAS", "SABESP", "COPASA", "EMBASA",
                        "BRK AMBIENTAL", "AEGEA", "IGUA"]),
    ("TELECOM",        ["TELECOM", "FIBRA", "INTERNET", "VIVO", "TIM ", "CLARO ", "OI ",
                        "AMERICANET", "BRISANET", "ALGAR"]),
    ("OLEO_E_GAS",     ["PETROLEO", "PETRÓLEO", "GAS ", "GÁS ", "PETROBRAS", "ULTRAGAZ",
                        "REFINARIA", "ETANOL", "BIOCOMBUST"]),
]


def inferir_setor(nome_emissor):
    nome_up = (nome_emissor or "").upper()
    for setor, kws in SETOR_KEYWORDS:
        for kw in kws:
            if kw in nome_up:
                return setor
    return "INFRAESTRUTURA"  # default (debenture incentivada = infra por definicao legal)


def baixar_csv():
    log.info(f"Baixando {URL_ZIP}...")
    r = requests.get(URL_ZIP, timeout=300, verify=False)
    r.raise_for_status()
    log.info(f"  baixado: {len(r.content):,} bytes")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    if ARQ_INTERNO not in z.namelist():
        raise RuntimeError(f"{ARQ_INTERNO} nao encontrado no zip. Files: {z.namelist()}")
    with z.open(ARQ_INTERNO) as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def parse_data(s):
    if not s: return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_dec(s):
    """CVM usa formato US (1180000000.00). Se vier com virgula, formato BR (1.234,56)."""
    if not s: return None
    s = str(s).strip()
    if not s: return None
    try:
        if "," in s:
            return float(s.replace(".", "").replace(",", "."))
        return float(s)
    except (ValueError, TypeError):
        return None


def normalizar_cnpj(s):
    if not s: return None
    digs = re.sub(r"\D", "", s)
    return digs[:14] if len(digs) == 14 else None


def _main_impl():
    text = baixar_csv()
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    headers = reader.fieldnames or []
    log.info(f"  Colunas ({len(headers)})")

    obras = []
    cont_setor = {}
    pulados_nao_deb = 0
    pulados_nao_incent = 0
    pulados_ano = 0
    pulados_valor = 0
    pulados_status = 0
    agora = datetime.now(timezone.utc)
    for row in reader:
        valor_mob = (row.get("Valor_Mobiliario") or "").upper()
        if "DEB" not in valor_mob:
            pulados_nao_deb += 1
            continue

        incent = (row.get("Titulo_incentivado") or "").strip().upper()
        if incent != "S":
            pulados_nao_incent += 1
            continue

        # Status: aceitar Oferta Encerrada e Registrada (= debenture viva)
        status = (row.get("Status_Requerimento") or "").strip()
        if status in ("Oferta Revogada", "Requerimento Expirado", "Oferta Cancelada"):
            pulados_status += 1
            continue

        data_reg = parse_data(row.get("Data_Registro") or row.get("Data_requerimento"))
        if data_reg and data_reg.year < ANO_MINIMO:
            pulados_ano += 1
            continue

        valor = parse_dec(row.get("Valor_Total_Registrado"))
        # Se valor ausente, ainda aceita (oferta registrada que ainda nao bookbuildou)
        if valor and valor < VALOR_MINIMO:
            pulados_valor += 1
            continue

        cnpj = normalizar_cnpj(row.get("CNPJ_Emissor"))
        nome_emissor = (row.get("Nome_Emissor") or "").strip()
        if not nome_emissor:
            continue

        num_proc = (row.get("Numero_Processo") or "").strip()
        num_req = (row.get("Numero_Requerimento") or "").strip()
        emissao = (row.get("Emissao") or "").strip() or "?"
        site_emissor = (row.get("Endereco_emissor_rede_mundial_computadores") or "").strip()

        setor = inferir_setor(nome_emissor)
        cont_setor[setor] = cont_setor.get(setor, 0) + 1

        nome = f"Debenture Infra {emissao}a emissao - {nome_emissor}"[:500]

        id_externo = f"CVM-DEB-{re.sub(r'[^0-9A-Za-z]', '_', num_proc or num_req)}"[:200]

        destinacao = (row.get("Destinacao_recursos") or "").strip()[:400]

        valor_fmt = None
        if valor:
            valor_fmt = (
                f"R$ {valor/1e9:.2f} bi" if valor >= 1e9
                else f"R$ {valor/1e6:.0f} mi"
            )

        descricao = (
            f"Debenture de Infraestrutura (Lei 12.431) — {emissao}a emissao. "
            f"Emissor: {nome_emissor}. "
            f"Processo CVM: {num_proc}. Status: {status}. "
            f"Destinacao: {destinacao}"
        )[:1000]

        url_fonte = site_emissor if site_emissor.startswith("http") else "https://dados.cvm.gov.br/dataset/oferta-distrib"

        obras.append((
            id_externo,
            nome,
            nome_emissor[:300],
            cnpj,
            setor,
            None,                       # municipio: nao tem
            None,                       # uf: nao tem
            valor,
            valor_fmt,
            "EM_EXECUCAO",              # debenture registrada = obra com funding
            f"CVM {status}"[:200],
            2,
            65,                         # debenture incentivada ICP bom
            ["CIVIL_TECNICA"],
            descricao,
            FONTE,
            url_fonte[:500],
            data_reg,
            "OFICIAL",
            agora,
        ))

    log.info("=== ESTATISTICAS ===")
    log.info(f"  Pulados nao-debenture: {pulados_nao_deb}")
    log.info(f"  Pulados nao-incentivados: {pulados_nao_incent}")
    log.info(f"  Pulados status revogado/expirado: {pulados_status}")
    log.info(f"  Pulados ano < {ANO_MINIMO}: {pulados_ano}")
    log.info(f"  Pulados valor < {VALOR_MINIMO:,}: {pulados_valor}")
    log.info(f"  Para upsert: {len(obras)}")
    log.info(f"  Por setor: {sorted(cont_setor.items(), key=lambda x:-x[1])}")

    dedup = {o[0]: o for o in obras}
    obras = list(dedup.values())
    log.info(f"  Apos dedup: {len(obras)}")
    _STATS["buscados"] = len(obras)

    if not obras:
        log.warning("Nada para inserir."); return

    dry_run = "--dry-run" in sys.argv
    if dry_run:
        for o in obras[:3]:
            log.info(f"DRY: {o[0]} | {o[1][:80]} | {o[3]} | setor={o[4]} | R${o[7]}")
        return

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    sql = """
        INSERT INTO obras (
            id_externo, nome, empresa, cnpj, setor, municipio, uf,
            valor_estimado, valor_formatado, fase, status_licenca,
            urgencia, lead_score, necessidades, descricao, fonte, url_fonte,
            data_publicacao, fonte_tipo, validacao_obra_at
        ) VALUES %s
        ON CONFLICT (id_externo) DO UPDATE SET
            empresa = COALESCE(obras.empresa, EXCLUDED.empresa),
            cnpj = COALESCE(obras.cnpj, EXCLUDED.cnpj),
            valor_estimado = COALESCE(obras.valor_estimado, EXCLUDED.valor_estimado),
            valor_formatado = COALESCE(obras.valor_formatado, EXCLUDED.valor_formatado),
            descricao = EXCLUDED.descricao,
            setor = COALESCE(obras.setor, EXCLUDED.setor),
            data_publicacao = COALESCE(obras.data_publicacao, EXCLUDED.data_publicacao),
            fonte_tipo = COALESCE(obras.fonte_tipo, EXCLUDED.fonte_tipo),
            validacao_obra_at = COALESCE(obras.validacao_obra_at, EXCLUDED.validacao_obra_at)
    """
    with conn.cursor() as cur:
        # PORTÃO (Fase 3): dedup cross-source/interno por CNPJ_raiz+nome_norm antes do insert.
        # Fail-open + kill-switch internos; nunca zera o captador por bug. Layout idx padrão.
        try:
            import sys as _s
            if "/app/scripts/portao" not in _s.path:
                _s.path.insert(0, "/app/scripts/portao")
            import portao as _pt
            try:
                from web_search_serper import web_search_fn as _ws
            except Exception:
                _ws = None
            obras = _pt.filtrar_e_enriquecer(obras, "debentures_infra", conn, web_search_fn=_ws, log=log)
        except Exception as _e:
            log.warning(f"[PORTAO] enforce off (erro): {_e!r}")
        execute_values(cur, sql, obras)
        log.info(f"  UPSERT: {cur.rowcount} linhas afetadas")
        _STATS["novos"] = cur.rowcount
    conn.commit()
    conn.close()
    log.info("=== FIM DEBENTURES INFRA ===")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        _main_impl()
    except Exception as e:
        log.exception(f"ERRO: {e}")
        _STATS["erros"] = 1
        sys.exit(1)


if __name__ == "__main__":
    main()
