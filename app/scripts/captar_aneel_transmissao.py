"""
Captador ANEEL — Resultado de leiloes de transmissao.
Fonte: dadosabertos.aneel.gov.br — CSV publico.

Distinto de captar_aneel (geracao/SIGA): aqui sao LTs, subestacoes, lotes de transmissao.
Filtra leiloes >= 2020 (obras recentes) e descarta "Incorporacao de ativos em servico"
(que sao ativos ja construidos transferidos por portaria, nao obras novas).
"""
import os, csv, io, re, sys, logging
from pathlib import Path
import requests, certifi
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

URL_CSV = (
    "https://dadosabertos.aneel.gov.br/dataset/"
    "593537c6-9e0e-4ed9-817a-2c5d5de05147/resource/"
    "453cb742-8089-4c16-aaf2-42088b5553dc/download/"
    "resultado-leiloes-transmissao.csv"
)
ANO_MINIMO = 2020
FONTE = "aneel_transmissao"

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Reusa bundle SSL do captar_aneel.py (Sectigo intermediate)
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


class ANEELPortalOffline(Exception):
    """Mesma logica de captar_aneel.py — exit-0 quando portal volta IIS/404."""
    pass


# Concessionarias/grupos de transmissao com cobertura conhecida.
# Lookup conservador — CNPJ so quando exato no nome vencedor.
# Keys = substring (lowercase, sem acento simples) procurado em NomVencedorLeilao.
# CNPJs validados via cnpj_valido() em 03/06/2026.
# IMPORTANTE: ordem importa — match para no primeiro hit. Coloque substrings mais
# especificos antes dos genericos (ex.: "edp - energias do brasil" antes de "edp").
EMPRESA_CNPJ_MAP = [
    ("state grid",                              "11938558000139"),  # State Grid Brazil Holding S.A.
    ("isa cteep",                               "02998611000104"),
    ("cteep",                                   "02998611000104"),  # CTEEP - Cia Transmissao Energia Eletrica Paulista (ISA Energia BR)
    ("isa energia brasil",                      "02998611000104"),
    ("centrais eletricas do norte do brasil",   "00357038000116"),
    ("eletronorte",                             "00357038000116"),
    ("edp - energias do brasil",                "03983431000103"),
    ("edp energias do brasil",                  "03983431000103"),
    ("energisa transmissao",                    "28201130000101"),
    ("energisa transmissão",                    "28201130000101"),
    ("alupar",                                  "08364948000138"),
    ("taesa",                                   "07859971000130"),
    ("transmissora alianca",                    "07859971000130"),  # TAESA
    ("transmissora aliança",                    "07859971000130"),
    ("chesf",                                   "33541368000116"),
    ("furnas",                                  "23274194000119"),
    ("neoenergia",                              "01083200000118"),
    ("engie brasil",                            "02474103000119"),
    ("eletrobras",                              "00001180000126"),
]


def baixar_csv():
    log.info(f"Baixando {URL_CSV[:90]}...")
    r = requests.get(URL_CSV, timeout=300, verify=_aneel_verify_path())
    if r.status_code == 404 or "Meu Site no IIS" in r.text[:500]:
        raise ANEELPortalOffline(f"ANEEL portal offline (HTTP {r.status_code}).")
    r.raise_for_status()
    log.info(f"  baixado: {len(r.content):,} bytes")
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return r.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.content.decode("latin-1", errors="replace")


def parse_dec(s):
    if not s: return None
    try:
        return float(str(s).replace(".", "").replace(",", "."))
    except (ValueError, TypeError):
        return None


def parse_data(s):
    if not s: return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
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
    cont_uf = {}
    cont_ano = {}
    pulados_ano = 0
    pulados_incorp = 0
    pulados_sem_valor = 0
    agora = datetime.now(timezone.utc)
    for row in reader:
        ano = (row.get("AnoLeilao") or "").strip()
        try:
            ano_i = int(ano)
        except (ValueError, TypeError):
            pulados_ano += 1
            continue
        if ano_i < ANO_MINIMO:
            pulados_ano += 1
            continue

        nome_emp = (row.get("NomEmpreendimento") or "").strip()
        nome_low = nome_emp.lower()
        if (
            "incorpora" in nome_low and "ativos em servi" in nome_low
        ):
            pulados_incorp += 1
            continue
        if not nome_emp:
            pulados_incorp += 1
            continue

        num_leilao = (row.get("NumLeilao") or "").strip()
        num_lote   = (row.get("NumLoteLeilao") or "").strip()
        uf         = (row.get("SigUFPrincipal") or "").strip() or None
        prazo_m    = (row.get("QtdPrazoConstrucaoMeses") or "").strip()
        ext_km     = parse_dec(row.get("MdaExtensaoLinhaTransmissaoKm"))
        sub_mva    = parse_dec(row.get("MdaSubEstacoesMVA"))
        valor      = parse_dec(row.get("VlrInvestimentoPrevisto"))
        vencedor   = (row.get("NomVencedorLeilao") or "").strip()
        data_leil  = parse_data(row.get("DatLeilao"))

        if not valor or valor <= 0:
            pulados_sem_valor += 1
            continue

        empresa = vencedor or None
        cnpj = None
        if vencedor:
            venc_low = vencedor.lower()
            for substr, cnpj_canon in EMPRESA_CNPJ_MAP:
                if substr in venc_low:
                    cnpj = cnpj_canon
                    break

        nome = f"ANEEL Transmissao Leilao {num_leilao} Lote {num_lote}: {nome_emp}"[:500]

        id_externo = f"ANEEL-TRANS-{re.sub(r'[^0-9A-Za-z]', '_', num_leilao)}-LOTE{num_lote}"[:200]

        valor_fmt = (
            f"R$ {valor/1e9:.2f} bi" if valor >= 1e9
            else f"R$ {valor/1e6:.0f} mi"
        )

        partes_desc = [
            f"Leilao ANEEL {num_leilao} (Lote {num_lote})",
            f"Vencedor: {vencedor or 's/d'}",
        ]
        if data_leil: partes_desc.append(f"Leilao em {data_leil.isoformat()}")
        if prazo_m: partes_desc.append(f"Prazo construcao: {prazo_m} meses")
        if ext_km: partes_desc.append(f"Extensao: {ext_km:.1f} km")
        if sub_mva: partes_desc.append(f"Subestacoes: {sub_mva:.0f} MVA")
        descricao = " | ".join(partes_desc)[:1000]

        obras.append((
            id_externo,
            nome,
            empresa[:300] if empresa else None,
            cnpj,
            "ENERGIA",
            None,                       # municipio: nao tem coluna
            uf,
            valor,
            valor_fmt,
            "EM_EXECUCAO",              # leilao realizado = obra autorizada
            f"Leilao ANEEL {num_leilao}"[:200],
            2,
            70,                         # ENERGIA transmissao tier alto
            ["CIVIL_TECNICA", "ELETRICA"],
            descricao,
            FONTE,
            f"https://www.aneel.gov.br/leiloes-de-transmissao",
            data_leil,
            "OFICIAL",
            agora,
        ))
        cont_uf[uf or "?"] = cont_uf.get(uf or "?", 0) + 1
        cont_ano[ano] = cont_ano.get(ano, 0) + 1

    log.info("=== ESTATISTICAS ===")
    log.info(f"  Pulados ano < {ANO_MINIMO}: {pulados_ano}")
    log.info(f"  Pulados incorporacao/sem nome: {pulados_incorp}")
    log.info(f"  Pulados sem valor: {pulados_sem_valor}")
    log.info(f"  Para upsert: {len(obras)}")
    log.info(f"  Top UFs: {sorted(cont_uf.items(), key=lambda x:-x[1])[:10]}")
    log.info(f"  Por ano: {sorted(cont_ano.items())}")

    # Dedup
    dedup = {o[0]: o for o in obras}
    obras = list(dedup.values())
    log.info(f"  Apos dedup: {len(obras)}")
    _STATS["buscados"] = len(obras)

    if not obras:
        log.warning("Nada para inserir."); return

    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log.info(f"DRY-RUN: 2 primeiras: {obras[:2]}")
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
            uf = COALESCE(obras.uf, EXCLUDED.uf),
            valor_estimado = COALESCE(obras.valor_estimado, EXCLUDED.valor_estimado),
            valor_formatado = COALESCE(obras.valor_formatado, EXCLUDED.valor_formatado),
            descricao = EXCLUDED.descricao,
            data_publicacao = COALESCE(obras.data_publicacao, EXCLUDED.data_publicacao),
            fonte_tipo = COALESCE(obras.fonte_tipo, EXCLUDED.fonte_tipo),
            validacao_obra_at = COALESCE(obras.validacao_obra_at, EXCLUDED.validacao_obra_at)
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, obras)
        log.info(f"  UPSERT: {cur.rowcount} linhas afetadas")
        _STATS["novos"] = cur.rowcount
    conn.commit()

    # master pipeline v2 (fail-safe)
    try:
        from _master_hook import notificar_master_v2
        _cols = ['id_externo', 'nome', 'empresa', 'cnpj', 'setor', 'municipio', 'uf', 'valor_estimado', 'valor_formatado', 'fase', 'status_licenca', 'urgencia', 'lead_score', 'necessidades', 'descricao', 'fonte', 'url_fonte', 'data_publicacao', 'fonte_tipo', 'validacao_obra_at']
        for _obra in obras:
            try:
                if isinstance(_obra, dict):
                    _payload = dict(_obra)
                else:
                    _payload = {_cols[_i]: _obra[_i] for _i in range(min(len(_cols), len(_obra)))}
                _id_ext = _payload.get("id_externo")
                _fonte = _payload.get("fonte") or "aneel_transmissao"
                if _id_ext:
                    notificar_master_v2(
                        fonte=_fonte,
                        captador="captar_aneel_transmissao",
                        id_externo=str(_id_ext),
                        payload=_payload,
                    )
            except Exception:
                pass
    except Exception:
        pass


    conn.close()
    log.info("=== FIM ANEEL TRANSMISSAO ===")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        _main_impl()
    except ANEELPortalOffline as e:
        log.warning(f"ANEEL_OFFLINE_SKIP: {e}")
        sys.exit(0)
    except Exception as e:
        log.exception(f"ERRO: {e}")
        _STATS["erros"] = 1
        sys.exit(1)


if __name__ == "__main__":
    main()
