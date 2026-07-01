"""
Captador ANM v2 (Mineracao) — via SIGMINE/PROCESSOS_MINERARIOS (shapefile .dbf por UF).

MUDANCA vs v1 (CFEM): v1 lia CFEM_Arrecadacao = royalty/producao (nao obra) -> 100%% ruido,
desativado 16/06 + 18.727 registros purgados. v2 le SIGMINE e filtra SO eventos que sinalizam
OBRA REAL de construcao:

  - PORTARIA CONCESSAO DE LAVRA ... PUBL   -> NOVA_CONCESSAO (nova mina outorgada = vai construir/operar)
  - BARRAGENS REQUERIMENTO DEFERIDO        -> BARRAGEM       (nova barragem de rejeito aprovada = obra pesada)
  - BARRAGENS ANALISE PROCESSUAL CONCLUIDA -> BARRAGEM

Filtros de qualidade (zero-ruido):
  - evento na whitelist acima
  - data do evento (ULT_EVENTO) >= corte (default 24 meses)
  - substancia metalica ou industrial-cimento/fertilizante (exclui agua mineral/areia/argila/gema/quartzo)
  - titular PJ (exclui pessoa fisica/garimpo)

Valor: SIGMINE nao tem capex. Estima por (tipo evento x substancia) e marca capex_fonte='ESTIMATIVA_TIPOLOGIA'
(mesma convencao do captar_ibama; headline filtra esse flag, nao soma como CAPEX confirmado).
Valor real fica para enriquecimento posterior (WebSearch/Haiku).

CNPJ: SIGMINE so traz nome do titular. Resolve CNPJ por match de nome contra a base (obras/fornecedores).
Dedup: titular que ja tem obra MINERACAO visivel entra OCULTO (motivo anm_sig_dup_revisar, reversivel).

Fonte: https://dadosabertos.anm.gov.br/SIGMINE/PROCESSOS_MINERARIOS/{UF}.zip

Uso:
  python captar_anm.py            # DRY-RUN (so relata, nao escreve)
  python captar_anm.py --commit   # insere/upsert no banco
  python captar_anm.py --uf MG,PA,GO --meses 24 --commit
"""
import os, re, io, sys, zipfile, struct, logging, argparse, unicodedata, tempfile
from datetime import datetime, timedelta
from collections import defaultdict, Counter
import requests
import psycopg2
from psycopg2.extras import execute_values
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("captar_anm")

# STATS_JSON — orquestrador parseia ultima linha
import atexit as _atexit, json as _json
_STATS = {"buscados": 0, "novos": 0, "erros": 0}
@_atexit.register
def _emit_stats():
    try: print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)
    except Exception: pass

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

BASE_URL = "https://dadosabertos.anm.gov.br/SIGMINE/PROCESSOS_MINERARIOS/{uf}.zip"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "*/*",
           "Referer": "https://dadosabertos.anm.gov.br/SIGMINE/PROCESSOS_MINERARIOS/"}
UFS_BR = ["AC","AL","AP","AM","BA","CE","DF","ES","GO","MA","MT","MS","MG","PA",
          "PB","PR","PE","PI","RJ","RN","RS","RO","RR","SC","SP","SE","TO"]

# ── Whitelist de evento (sinal de obra) ───────────────────────────────────────
EVT_NOVA   = "PORTARIA CONCESSAO DE LAVRA"
EVT_DAM    = ("BARRAGENS REQUERIMENTO DEFERIDO", "BARRAGENS ANALISE PROCESSUAL CONCLUIDA")

# ── Substancias aceitas ───────────────────────────────────────────────────────
SUBS_METALICO = {
    "FERRO","MINERIO DE FERRO","COBRE","MINERIO DE COBRE","OURO","BAUXITA","ALUMINIO",
    "MINERIO DE ALUMINIO","NIQUEL","MINERIO DE NIQUEL","MANGANES","MINERIO DE MANGANES",
    "ESTANHO","MINERIO DE ESTANHO","LITIO","MINERIO DE LITIO","ZINCO","CHUMBO","NIOBIO",
    "TITANIO","MINERIO DE TITANIO","COBALTO","PRATA","CROMO","TUNGSTENIO","VANADIO",
    "TANTALO","GRAFITA","GRAFITE","TERRAS RARAS","MOLIBDENIO","FERRO E MANGANES",
}
SUBS_INDUSTRIAL = {
    "CALCARIO","FOSFATO","ROCHA FOSFATICA","POTASSIO","POTASSA","SALGEMA","ENXOFRE",
    "GIPSITA","MAGNESITA","FLUORITA","BARITA","CAULIM","FELDSPATO",
}

PJ_MARK = ("LTDA","S.A","S/A"," SA"," S A","S A ","MINERA","COMPANHIA"," CIA","INDUSTRIA",
           "EIRELI"," ME ","METAIS","MINERIOS","RECURSOS MINERAIS","CIMENTOS","FERTILIZANTES",
           "SIDERURGICA","MINING","EPP","S.A.")


def na(s):
    if s is None: return ""
    return unicodedata.normalize("NFKD", str(s)).encode("ASCII", "ignore").decode().upper().strip()


def estimar_valor(tipo, cat):
    if tipo == "BARRAGEM":
        return 500_000_000          # barragem de rejeito (real: R$0,3-2bi)
    if cat == "METALICO":
        return 300_000_000          # nova mina metalica
    return 150_000_000              # nova mina industrial/cimento/fertilizante


# ── Leitor DBF puro (so campos Character; SIGMINE e tudo texto) ────────────────
def ler_dbf(raw):
    """Generator de dicts a partir de bytes .dbf. Decodifica utf-8 com fallback latin-1."""
    if len(raw) < 32:
        return
    num_rec  = struct.unpack("<I", raw[4:8])[0]
    hdr_len  = struct.unpack("<H", raw[8:10])[0]
    rec_len  = struct.unpack("<H", raw[10:12])[0]
    # descritores de campo: de 32 ate 0x0D
    fields = []
    off = 32
    while off < hdr_len - 1 and raw[off] != 0x0D:
        name = raw[off:off+11].split(b"\x00")[0].decode("ascii", "replace")
        flen = raw[off+16]
        fields.append((name, flen))
        off += 32
    pos = hdr_len
    for _ in range(num_rec):
        rec = raw[pos:pos+rec_len]
        pos += rec_len
        if not rec or rec[:1] == b"\x2a":   # deletado
            continue
        vals = {}
        p = 1   # pula flag de delecao
        for name, flen in fields:
            chunk = rec[p:p+flen]; p += flen
            try: v = chunk.decode("utf-8")
            except UnicodeDecodeError: v = chunk.decode("latin-1", "replace")
            vals[name] = v.strip()
        yield vals


def baixar_uf(uf):
    r = requests.get(BASE_URL.format(uf=uf), headers=HEADERS, timeout=300, verify=False)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    dbf_name = next((n for n in zf.namelist() if n.lower().endswith(".dbf")), None)
    if not dbf_name:
        raise RuntimeError(f"{uf}: .dbf nao encontrado no zip")
    return zf.read(dbf_name)


DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def classificar(rec):
    """Retorna (tipo, cat, data_evt) se o registro e candidato a obra, senao None."""
    ev = na(rec.get("ULT_EVENTO"))
    if EVT_NOVA in ev:
        tipo = "NOVA_CONCESSAO"
    elif any(k in ev for k in EVT_DAM):
        tipo = "BARRAGEM"
    else:
        return None
    subs = na(rec.get("SUBS"))
    # casa por igualdade ou por token (ex. "MINERIO DE FERRO E MANGANES")
    cat = None
    if subs in SUBS_METALICO or any(t in SUBS_METALICO for t in subs.split(" E ")):
        cat = "METALICO"
    elif subs in SUBS_INDUSTRIAL or any(t in SUBS_INDUSTRIAL for t in subs.split(" E ")):
        cat = "INDUSTRIAL"
    if not cat:
        return None
    nome_tit = na(rec.get("NOME"))
    if "CPF" in nome_tit or not any(m in nome_tit for m in PJ_MARK):
        return None     # pessoa fisica / garimpo
    m = DATE_RE.search(ev)
    if not m:
        return None
    try:
        d = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return tipo, cat, d


def carregar_base_cnpj_e_dups(conn):
    """Mapa nome_norm->cnpj (obras+fornecedores) e set de titulares com obra MINERACAO visivel."""
    nome2cnpj = {}
    dup_titulares = set()
    with conn.cursor() as cur:
        cur.execute("SELECT empresa, cnpj FROM obras WHERE cnpj IS NOT NULL AND empresa IS NOT NULL")
        for emp, cnpj in cur.fetchall():
            nome2cnpj.setdefault(na(emp), cnpj)
        cur.execute("SELECT razao_social, cnpj FROM fornecedores WHERE cnpj IS NOT NULL AND razao_social IS NOT NULL")
        for nm, cnpj in cur.fetchall():
            nome2cnpj.setdefault(na(nm), cnpj)
        cur.execute("SELECT DISTINCT empresa FROM obras WHERE setor='MINERACAO' AND visivel AND empresa IS NOT NULL")
        for (emp,) in cur.fetchall():
            dup_titulares.add(na(emp))
    return nome2cnpj, dup_titulares


def resolver_cnpj(titular_norm, nome2cnpj):
    if titular_norm in nome2cnpj:
        return nome2cnpj[titular_norm]
    # containment (nomes >= 8 chars pra evitar falso-positivo)
    if len(titular_norm) >= 8:
        for nm, cnpj in nome2cnpj.items():
            if len(nm) >= 8 and (titular_norm in nm or nm in titular_norm):
                return cnpj
    return None


def eh_dup(titular_norm, dup_titulares):
    if titular_norm in dup_titulares:
        return True
    if len(titular_norm) >= 8:
        for d in dup_titulares:
            if len(d) >= 8 and (titular_norm in d or d in titular_norm):
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="escreve no banco (default: dry-run)")
    ap.add_argument("--uf", default="", help="lista CSV de UFs (default: todas)")
    ap.add_argument("--meses", type=int, default=24, help="janela do evento em meses")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    ufs = [u.strip().upper() for u in args.uf.split(",") if u.strip()] or UFS_BR
    corte = datetime.now() - timedelta(days=30 * args.meses)
    log.info(f"SIGMINE v2 | UFs={len(ufs)} | corte evento >= {corte.date()} | commit={args.commit}")

    candidatos = []
    for uf in ufs:
        try:
            raw = baixar_uf(uf)
        except Exception as e:
            log.warning(f"  {uf}: download falhou: {e}")
            _STATS["erros"] += 1
            continue
        n_uf = 0
        for rec in ler_dbf(raw):
            c = classificar(rec)
            if not c:
                continue
            tipo, cat, d = c
            if d < corte:
                continue
            candidatos.append({
                "uf": uf, "tipo": tipo, "cat": cat, "data": d,
                "titular": (rec.get("NOME") or "").strip()[:300],
                "subs": (rec.get("SUBS") or "").strip(),
                "processo": (rec.get("PROCESSO") or "").strip(),
                "evento": (rec.get("ULT_EVENTO") or "").strip(),
            })
            n_uf += 1
        log.info(f"  {uf}: {n_uf} candidatos")

    log.info(f"=== Total candidatos (obra-signal): {len(candidatos)} ===")
    by_tipo = Counter(c["tipo"] for c in candidatos)
    log.info(f"  por tipo: {dict(by_tipo)}")

    if not candidatos:
        _STATS["buscados"] = 0
        return

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    nome2cnpj, dup_titulares = carregar_base_cnpj_e_dups(conn)
    log.info(f"  base: {len(nome2cnpj)} nomes->cnpj | {len(dup_titulares)} titulares MINERACAO visiveis")

    TIPO_LABEL = {"NOVA_CONCESSAO": "Nova mina (concessao de lavra)", "BARRAGEM": "Barragem de rejeitos"}
    NECESS = ["CIVIL_TECNICA", "TERRAPLANAGEM", "ELETRICA_INDUSTRIAL"]
    obras = []
    n_dup = n_cnpj = 0
    for c in candidatos:
        tn = na(c["titular"])
        cnpj = resolver_cnpj(tn, nome2cnpj)
        if cnpj: n_cnpj += 1
        dup = eh_dup(tn, dup_titulares)
        if dup: n_dup += 1
        valor = estimar_valor(c["tipo"], c["cat"])
        proc_clean = re.sub(r"[^0-9A-Za-z]", "_", c["processo"])[:30] or "np"
        id_ext = f"ANMSIG-{proc_clean}-{c['tipo'][:4]}"[:200]
        tit_title = c["titular"].title() if c["titular"].isupper() else c["titular"]
        nome = f"{TIPO_LABEL[c['tipo']]} - {tit_title} ({c['subs'].title()}/{c['uf']})"[:300]
        if valor >= 1e9: vfmt = f"R$ {valor/1e9:.1f} bi (estimativa ANM)"
        else: vfmt = f"R$ {valor/1e6:.0f} mi (estimativa ANM)"
        fase = "CONSTRUCAO" if c["tipo"] == "BARRAGEM" else "IMPLANTACAO"
        desc = (f"Sinal ANM/SIGMINE: {c['evento']}. Substancia: {c['subs']}. "
                f"Processo ANM {c['processo']}. Titular: {c['titular']}. "
                f"Tipo: {TIPO_LABEL[c['tipo']]}. Valor estimado por tipologia (nao confirmado).")[:1000]
        score = 60 + (10 if c["cat"] == "METALICO" else 0) + (10 if c["tipo"] == "BARRAGEM" else 0)
        obras.append((
            id_ext, nome, c["titular"], cnpj, "MINERACAO",
            None, c["uf"], valor, vfmt, fase,
            c["evento"][:200], 2, min(score, 100), NECESS, desc,
            "anm_sigmine", BASE_URL.format(uf=c["uf"]), c["data"].date().isoformat(),
            "OFICIAL", "ESTIMATIVA_TIPOLOGIA",
            ("anm_sig_dup_revisar" if dup else None),
            (not dup),   # visivel: dup entra oculto p/ revisao
        ))

    log.info(f"  obras montadas: {len(obras)} | com CNPJ resolvido: {n_cnpj} | marcadas dup (ocultas): {n_dup}")
    _STATS["buscados"] = len(obras)

    # Preview dos visiveis (net-new)
    log.info("  --- NET-NEW (entram visiveis) ---")
    for o in obras:
        if o[20] is None:
            log.info(f"    [{o[9]:11s}] {o[6]} {o[8]:24s} {o[1][:60]}  cnpj={o[3] or '-'}")

    if not args.commit:
        log.info("DRY-RUN: nada escrito. Use --commit para inserir.")
        return

    sql = """
        INSERT INTO obras (
            id_externo, nome, empresa, cnpj, setor, municipio, uf,
            valor_estimado, valor_formatado, fase, status_licenca,
            urgencia, lead_score, necessidades, descricao, fonte, url_fonte, data_publicacao,
            fonte_tipo, capex_fonte, motivo_invisivel, visivel
        ) VALUES %s
        ON CONFLICT (id_externo) DO UPDATE SET
            empresa = COALESCE(obras.empresa, EXCLUDED.empresa),
            cnpj = COALESCE(obras.cnpj, EXCLUDED.cnpj),
            uf = COALESCE(obras.uf, EXCLUDED.uf),
            valor_estimado = COALESCE(obras.valor_estimado, EXCLUDED.valor_estimado),
            valor_formatado = COALESCE(obras.valor_formatado, EXCLUDED.valor_formatado),
            data_publicacao = COALESCE(obras.data_publicacao, EXCLUDED.data_publicacao),
            descricao = EXCLUDED.descricao,
            status_licenca = EXCLUDED.status_licenca,
            lead_score = EXCLUDED.lead_score
    """
    with conn.cursor() as cur:
        total = 0
        for i in range(0, len(obras), 500):
            execute_values(cur, sql, obras[i:i+500])
            total += cur.rowcount or 0
        _STATS["novos"] = total
        log.info(f"  UPSERT: {total} linhas afetadas")
    conn.commit()
    conn.close()
    log.info("=== FIM ANM SIGMINE v2 ===")


if __name__ == "__main__":
    main()
