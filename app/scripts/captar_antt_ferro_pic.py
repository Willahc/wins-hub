"""
Captador ANTT — PIC ferroviário (Projetos de Interesse da Concessionária).
Fonte: dados.antt.gov.br — CSV publico, sem autenticacao.
Atualizacao semestral na fonte; orquestrador roda diario, UPSERT idempotente.

Sem coluna de capex no CSV — todas as PICs autorizadas entram com valor NULL.
Concessionarias mapeadas para CNPJ canonico quando ha match exato.
"""
import os, csv, io, re, sys, logging
import requests
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

# STATS_JSON — orquestrador parseia ultima linha pra log_captacao
import atexit as _atexit
import json as _stats_json
_STATS = {"buscados": 0, "novos": 0, "erros": 0}
def _emit_stats_json():
    try:
        print(f"STATS_JSON: {_stats_json.dumps(_STATS)}", flush=True)
    except Exception:
        pass
_atexit.register(_emit_stats_json)

URL_CSV = (
    "https://dados.antt.gov.br/dataset/"
    "7c6bc8ab-69fb-4955-9cf3-7481cfa089f9/resource/"
    "64a0f446-f67c-412d-8b87-17587a8e9f78/download/"
    "investimentos_autorizados_gpfer_pip.csv"
)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Concessionaria (codigo curto no CSV) -> (razao social canonica, CNPJ)
# Mapeamentos verificados em 03/06/2026. Codigos sem match seguro ficam empresa-only (cnpj=None).
CONCESS_MAP = {
    "MRS":             ("MRS LOGISTICA S/A", "01416968000183"),
    "FCA":             ("FERROVIA CENTRO-ATLANTICA S.A.", "00924429000175"),
    "RMP":             ("RUMO MALHA PAULISTA S.A.", "02502844000166"),
    "RMN":             ("RUMO MALHA NORTE S.A.", "24962466000136"),
    "RMS":             ("RUMO MALHA SUL S.A.", "02387241000160"),
    "RMC":             ("RUMO MALHA CENTRAL S.A.", "33744709000179"),
    "VALE/EFVM (FICO)": ("VALE S.A.", "33592510000154"),
    "EFVM":            ("VALE S.A.", "33592510000154"),
    "BAFER":           ("BAHIA MINERACAO LTDA", None),  # codigo ambiguo, sem cnpj
    "FTL":             ("FERROVIA TEREZA CRISTINA S.A.", "86902845000122"),
    "FNS":             ("VLI LOGISTICA INTEGRADA S.A.", "12563794000180"),  # FNS atualmente VLI
    "FTC":             ("FERROVIA TEREZA CRISTINA S.A.", "86902845000122"),
}

FONTE = "antt_ferro_pic"


def baixar_csv():
    log.info(f"Baixando {URL_CSV[:90]}...")
    r = requests.get(URL_CSV, timeout=120, verify=False)
    r.raise_for_status()
    log.info(f"  baixado: {len(r.content):,} bytes")
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return r.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.content.decode("latin-1", errors="replace")


def parse_data_pub(s):
    if not s: return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _main_impl():
    text = baixar_csv()
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    headers = reader.fieldnames or []
    log.info(f"  Colunas ({len(headers)}): {headers}")

    obras = []
    cont_conces = {}
    pulados_sem_proc = 0
    pulados_antigos = 0
    agora = datetime.now(timezone.utc)
    for row in reader:
        titulo = (row.get("titulo") or "").strip()
        num_proc = (row.get("numero_processo") or "").strip()
        conces = (row.get("concessionaria") or "").strip()
        uf = (row.get("estado") or "").strip() or None
        data_pub = parse_data_pub((row.get("data_da_publicacao") or "").strip())

        if not num_proc or not titulo:
            pulados_sem_proc += 1
            continue
        # Filtra obras pre-2023 (ja foi probe; nao deve ter, mas defensivo)
        if data_pub and data_pub.year < 2023:
            pulados_antigos += 1
            continue

        empresa, cnpj = CONCESS_MAP.get(conces, (conces, None))
        cont_conces[conces] = cont_conces.get(conces, 0) + 1

        # Nome curto: "PIC MRS — Conselheiro Lafaiete/MG (titulo)"
        nome = f"ANTT PIC {conces}: {titulo}"[:500]

        # Municipio: tenta extrair do titulo (padrao ".../Municipio/UF")
        municipio = None
        m = re.search(r",\s*([A-Za-zÀ-ÿ\s\-\']+)/(?:[A-Z]{2})\s*$", titulo)
        if m:
            municipio = m.group(1).strip()[:200]

        id_externo = f"ANTT-PIC-{re.sub(r'[^0-9A-Za-z]', '_', num_proc)}"[:200]

        descricao = (
            f"Projeto de Interesse da Concessionaria (PIC) — ANTT. "
            f"Processo {num_proc}. Publicado {data_pub.isoformat() if data_pub else 's/d'}. "
            f"Concessionaria: {conces}. {titulo[:300]}"
        )[:1000]

        obras.append((
            id_externo,
            nome,
            empresa[:300] if empresa else None,
            cnpj,
            "INFRAESTRUTURA",
            municipio,
            uf,
            None,                       # valor_estimado (sem dado no CSV)
            None,                       # valor_formatado
            "EM_EXECUCAO",              # PIC autorizado = obra em andamento
            "Autorizado ANTT",
            2,                          # urgencia padrao importer
            60,                         # lead_score: PIC ferroviario ICP medio
            ["CIVIL_TECNICA"],
            descricao,
            FONTE,
            "https://dados.antt.gov.br/dataset/investimentos-pic-ferroviarios",
            data_pub,
            "OFICIAL",                  # fonte_tipo
            agora,                      # validacao_obra_at (elegivel enrichment)
        ))

    log.info(f"=== ESTATISTICAS ===")
    log.info(f"  Pulados sem proc/titulo: {pulados_sem_proc}")
    log.info(f"  Pulados pre-2023: {pulados_antigos}")
    log.info(f"  Para upsert: {len(obras)}")
    log.info("  Top concessionarias:")
    for c, n in sorted(cont_conces.items(), key=lambda x: -x[1])[:15]:
        log.info(f"    {n:>4} | {c}")

    # Dedup por id_externo
    dedup = {}
    for o in obras:
        dedup[o[0]] = o
    obras = list(dedup.values())
    log.info(f"  Apos dedup: {len(obras)}")
    _STATS["buscados"] = len(obras)

    if not obras:
        log.warning("Nada para inserir."); return

    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log.info(f"DRY-RUN: 3 primeiras: {obras[:3]}")
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
            municipio = COALESCE(obras.municipio, EXCLUDED.municipio),
            uf = COALESCE(obras.uf, EXCLUDED.uf),
            descricao = EXCLUDED.descricao,
            data_publicacao = COALESCE(obras.data_publicacao, EXCLUDED.data_publicacao),
            fonte_tipo = COALESCE(obras.fonte_tipo, EXCLUDED.fonte_tipo),
            validacao_obra_at = COALESCE(obras.validacao_obra_at, EXCLUDED.validacao_obra_at)
    """
    with conn.cursor() as cur:
        try:
            if os.getenv("PORTAO_SHADOW") == "1":
                import sys as _s
                if "/app/scripts/portao" not in _s.path:
                    _s.path.insert(0, "/app/scripts/portao")
                import portao as _pt
                _pt.shadow_hook(
                    [{"nome": o[1], "empresa": o[2], "cnpj": o[3], "setor": o[4],
                      "municipio": o[5], "uf": o[6], "valor_estimado": o[7]}
                     for o in obras],
                    {"fonte": FONTE, "fonte_tipo": "OFICIAL"}, conn, log)
        except Exception as _e:
            log.warning(f"[PORTAO-SHADOW] {_e!r}")
        execute_values(cur, sql, obras)
        log.info(f"  UPSERT: {cur.rowcount} linhas afetadas")
        _STATS["novos"] = cur.rowcount
    conn.commit()
    conn.close()
    log.info("=== FIM ANTT PIC ===")


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
