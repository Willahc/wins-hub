import os, re, time, secrets, logging, threading, io, json
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager

import httpx, psycopg2, psycopg2.extras, jwt, bcrypt
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Request, HTTPException, Depends, BackgroundTasks, Body, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from permissions import (
    pode_ver_decisores_obra, pode_ver_conteudo_pago,
    pode_acessar_painel_vendas, eh_admin,
)
from services.comissoes import eh_primeira_assinatura, calcular_comissao_lead
from slowapi.errors import RateLimitExceeded


def get_real_ip(request):
    """Lê IP real de X-Forwarded-For (passado por nginx). Fallback pro client.host."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


limiter = Limiter(key_func=get_real_ip)

DB_CONFIG = {
    "host": os.getenv("DB_HOST","localhost"),
    "port": int(os.getenv("DB_PORT","5432")),
    "dbname": os.getenv("DB_NAME","wins_hub"),
    "user": os.getenv("DB_USER","postgres"),
    "password": os.getenv("DB_PASSWORD",""),
}
JWT_SECRET  = os.getenv("JWT_SECRET", secrets.token_hex(32))
CRON_SECRET = os.environ["CRON_SECRET"]
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)

def get_conn(): return psycopg2.connect(**DB_CONFIG)

def init_db():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
        CREATE TABLE IF NOT EXISTS prestadores (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            nome_empresa TEXT NOT NULL, cnpj TEXT UNIQUE,
            email TEXT UNIQUE NOT NULL, senha_hash TEXT NOT NULL,
            telefone TEXT, segmento TEXT, uf TEXT,
            plano TEXT DEFAULT 'GRATUITO', plano_expira TIMESTAMPTZ,
            creditos INT DEFAULT 0, ativo BOOLEAN DEFAULT TRUE,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS obras (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            id_externo TEXT UNIQUE, nome TEXT NOT NULL,
            empresa TEXT, cnpj TEXT, setor TEXT,
            municipio TEXT, uf TEXT,
            valor_estimado NUMERIC, valor_formatado TEXT,
            fase TEXT, status_licenca TEXT,
            urgencia INT DEFAULT 3, lead_score INT DEFAULT 50,
            necessidades TEXT[], descricao TEXT,
            fonte TEXT, url_fonte TEXT, data_publicacao DATE,
            nivel1_nome TEXT, nivel1_cargo TEXT,
            nivel1_email TEXT, nivel1_linkedin TEXT,
            nivel2_nome TEXT, nivel2_cargo TEXT,
            nivel2_email TEXT, nivel2_telefone TEXT,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_obras_uf ON obras(uf);
        CREATE INDEX IF NOT EXISTS idx_obras_fase ON obras(fase);
        CREATE TABLE IF NOT EXISTS interacoes (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            obra_id UUID REFERENCES obras(id),
            prestador_id UUID REFERENCES prestadores(id),
            tipo TEXT, plano_momento TEXT,
            valor_cobrado NUMERIC, score_match INT,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS log_captacao (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            fonte TEXT, status TEXT, buscados INT DEFAULT 0,
            novos INT DEFAULT 0, erro TEXT, duracao_ms INT,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        );
        """)
    conn.commit(); conn.close()
    log.info("Banco inicializado")

def hash_senha(s): return bcrypt.hashpw(s.encode(), bcrypt.gensalt()).decode()
def verificar_senha(s, h): return bcrypt.checkpw(s.encode(), h.encode())
def criar_token(pid, plano, is_representante=False, email=None, nome=None):
    # is_admin computado do email contra permissions.ADMIN_EMAIL.
    # nome (nome_empresa) embeddado pra frontend mostrar "Olá, {primeiro_nome}".
    from permissions import ADMIN_EMAIL
    is_admin = bool(email and email == ADMIN_EMAIL)
    payload = {
        "sub": pid, "plano": plano,
        "is_representante": bool(is_representante),
        "is_admin": is_admin,
        "exp": datetime.utcnow() + timedelta(hours=24),
        "iat": datetime.utcnow()
    }
    if nome:
        payload["nome"] = nome
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")
def verificar_token(token):
    try: return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError: raise HTTPException(401,"Token expirado.")
    except: raise HTTPException(401,"Token inválido.")

def get_user(c: Optional[HTTPAuthorizationCredentials]=Depends(security)):
    # Optional auth: SE sem credentials → None (anônimo).
    # SE com credentials inválidas/expiradas → também None (não propaga 401),
    # pra endpoints públicos não quebrarem quando user tem token velho.
    # Endpoints que EXIGEM auth devem usar requer_auth() (propaga 401).
    if not c:
        return None
    try:
        return verificar_token(c.credentials)
    except HTTPException:
        return None
def requer_auth(c: Optional[HTTPAuthorizationCredentials]=Depends(security)):
    if not c: raise HTTPException(401,"Autenticação necessária.")
    return verificar_token(c.credentials)

def obter_usuario_completo(u=Depends(requer_auth)):
    """Enriquece u (JWT payload) com email + plano + is_representante do banco.
    Retorna dict com sub, plano, email, is_representante. Use em gates que precisam
    distinguir representante de cliente."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT email, COALESCE(plano,'GRATUITO') AS plano,
                       COALESCE(is_representante,false) AS is_representante
                FROM prestadores WHERE id=%s
            """, (u["sub"],))
            row = cur.fetchone()
            if not row:
                raise HTTPException(401, "Usuário não encontrado")
            return {**u, **dict(row)}
    finally:
        conn.close()

# Mockup aprovado (20/05): empresa + capex SEMPRE visíveis (público/gratuito/pago).
# Decisor (nivel1_*) continua só em PREMIUM/desbloqueada via filtrar_obra.
CAMPOS_GRATUITO = {"id","nome","setor","uf","municipio","fase","urgencia","lead_score","fonte_tipo","dias_desde_validacao","url_validacao_status","obra_listada_na_fonte","obra_dados_mudaram_at","classificacao_computed","empresa","valor_estimado","valor_formatado","status_licenca"}
CAMPOS_STANDARD = CAMPOS_GRATUITO|{"cnpj","necessidades","descricao","data_publicacao","fonte"}

PRATA_CUTOFF = 80

# === Critérios canônicos do funil (3 estados: Ouro / Prata / Pipeline) ===
# Cada SQL referencia colunas de `obras` (sem alias) para uso flexível.
# Definidos em SQL puro para serem reutilizáveis em endpoints e na query principal.

# OURO: tem decisor cadastrado em decisores_obra com tipo_cargo válido E (email OR linkedin)
OURO_DECISOR_SQL = (
    "EXISTS ("
    " SELECT 1 FROM decisores_obra d"
    " WHERE d.obra_id = obras.id AND d.excluido_em IS NULL"
    "   AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo <> 'OUTRO'"
    "   AND ((COALESCE(d.email,'') <> '') OR (COALESCE(d.linkedin_url,'') <> ''))"
    ")"
)

# PRATA (parte positiva): nivel1_nome populado + cargo decisor por área-função
# explícita (compras/suprimentos/engenh/projetos/manutenc/obras/industrial),
# excluindo cargos genéricos como "Coordenador" sem qualificação.
PRATA_MATCH_SQL = (
    "(nivel1_nome IS NOT NULL AND nivel1_nome <> ''"
    " AND lower(unaccent(nivel1_cargo)) ~"
    " '(compras|suprimentos|supply|procurement|sourcing|engenh|projetos|obras|manutenc|industrial)'"
    " AND COALESCE(fonte_tipo,'OFICIAL') <> 'NOTICIA')"
)

# PIPELINE: obra pré-operação com capex relevante e sem decisor canônico.
# V0.1.4: aceita LICITACAO_ABERTA + PROJETO (fases válidas que faltavam),
# capex_min reduzido 100M→10M (médias obras industriais entram).
PIPELINE_SQL = (
    "(fase IN ('EM_EXECUCAO','PLANEJAMENTO','LICENCA_INSTALACAO','LICENCA_PREVIA','LICITACAO_ABERTA','PROJETO')"
    " AND valor_estimado IS NOT NULL AND valor_estimado >= 10000000"
    " AND COALESCE(nivel1_nome,'') = ''"
    " AND COALESCE(fonte_tipo,'OFICIAL') <> 'NOTICIA'"
    " AND NOT EXISTS ("
    " SELECT 1 FROM decisores_obra d"
    " WHERE d.obra_id = obras.id AND d.excluido_em IS NULL"
    "   AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo <> 'OUTRO'"
    "))"
)

# SCORE_PROSPECCAO: 0-5 sinais binários (CNPJ valido, empresa ATIVA, tem QSA,
# dominio resolvido, tem email Hunter). Calculado via EXISTS correlatas para
# evitar conflitos de coluna na cláusula WHERE dinâmica.
SCORE_PROSPECCAO_SQL = (
    "("
    " (CASE WHEN COALESCE(obras.cnpj_status,'') = 'ok' THEN 1 ELSE 0 END)"
    " + (CASE WHEN EXISTS ("
    "     SELECT 1 FROM decisores_cache dc"
    "     WHERE dc.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g')"
    "       AND dc.status_cnpj = 'ATIVA'"
    "   ) THEN 1 ELSE 0 END)"
    " + (CASE WHEN EXISTS ("
    "     SELECT 1 FROM decisores_cache dc"
    "     WHERE dc.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g')"
    "       AND dc.socios IS NOT NULL"
    "       AND jsonb_typeof(dc.socios) = 'array'"
    "       AND jsonb_array_length(dc.socios) > 0"
    "   ) THEN 1 ELSE 0 END)"
    " + (CASE WHEN EXISTS ("
    "     SELECT 1 FROM empresa_dominios ed"
    "     WHERE ed.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g')"
    "       AND ed.dominio_status IN ('ok','validado','validado_holding','validado_rebrand_em_curso')"
    "   ) THEN 1 ELSE 0 END)"
    " + (CASE WHEN EXISTS ("
    "     SELECT 1 FROM decisores_cache dc"
    "     WHERE dc.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g')"
    "       AND dc.emails IS NOT NULL"
    "       AND jsonb_typeof(dc.emails) = 'array'"
    "       AND jsonb_array_length(dc.emails) > 0"
    "   ) THEN 1 ELSE 0 END)"
    ")"
)

# Cache de matches: rever se o último foi gerado nas últimas N horas → senão regenera
MATCHES_CACHE_HORAS = 24 * 7  # 7 dias

# Set de prestadores cuja geração está em curso, para evitar disparos duplicados
_matches_em_geracao: set[str] = set()
_matches_lock = threading.Lock()


def _disparar_matchmaking_prestador(prestador_id: str) -> None:
    """Roda gerar_matches_para_prestador em thread, com guard contra concorrência."""
    with _matches_lock:
        if prestador_id in _matches_em_geracao:
            return
        _matches_em_geracao.add(prestador_id)

    def _runner():
        try:
            from services.matchmaking import gerar_matches_para_prestador
            gerar_matches_para_prestador(prestador_id)
        except Exception:
            log.exception(f"matchmaking on-login falhou para prestador {prestador_id}")
        finally:
            with _matches_lock:
                _matches_em_geracao.discard(prestador_id)

    threading.Thread(target=_runner, daemon=True, name=f"mm-{prestador_id[:8]}").start()


def _matches_status(prestador_id: str, conn) -> dict:
    """Retorna {status, total_matches, ultima_atualizacao} para um prestador."""
    with _matches_lock:
        em_geracao = prestador_id in _matches_em_geracao
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT MAX(m.gerado_em) AS ultima, COUNT(*) AS total
            FROM matches_obra_prestador m
            JOIN prestadores p ON p.cnpj = m.cnpj
            WHERE p.id = %s
        """, (prestador_id,))
        r = cur.fetchone() or {}
    ultima = r.get("ultima")
    total = int(r.get("total") or 0)
    if em_geracao:
        return {"status": "gerando", "total_matches": total,
                "ultima_atualizacao": ultima.isoformat() if ultima else None}
    return {"status": "pronto", "total_matches": total,
            "ultima_atualizacao": ultima.isoformat() if ultima else None}

CARGO_KEYWORDS_OURO = (
    "compras", "suprimentos", "supply", "procurement", "sourcing",
    "engenh", "projetos", "obras", "manutenção", "manutencao",
    "industrial", "coordenador", "gerente", "gestor",
)

def _cargo_e_decisor(cargo: Optional[str]) -> bool:
    if not cargo: return False
    c = cargo.lower()
    return any(kw in c for kw in CARGO_KEYWORDS_OURO)

def _obra_score(obra: dict) -> int:
    """Espelha a função SQL obra_score(obras). Mantenha as duas em sincronia."""
    try:
        valor = float(obra.get("valor_estimado") or 0)
    except (TypeError, ValueError):
        valor = 0.0
    desc_len = len(obra.get("descricao") or "")
    if desc_len >= 400: pts_desc = 20
    elif desc_len >= 200: pts_desc = 15
    elif desc_len >= 100: pts_desc = 10
    elif desc_len >= 1:   pts_desc = 5
    else:                 pts_desc = 0
    return (
        (25 if (obra.get("cnpj")  or "").strip() else 0) +
        (15 if (obra.get("uf")    or "").strip() else 0) +
        (20 if valor > 0 else 0) +
        (10 if (obra.get("setor") or "").strip() else 0) +
        ( 5 if (obra.get("fase")  or "").strip() else 0) +
        ( 5 if (obra.get("nome")  or "").strip() else 0) +
        pts_desc
    )

def filtrar_obra(obra, plano, desbloqueada=False):
    # Sprint 1 Auditoria Dedup: se nivel1_nome bate com decisor FP marcado,
    # NULLify campos nivel1_* ANTES de qualquer paywall logic. Decisor replicado
    # (Francisco Antonio Rueda et al.) nunca vaza em card ou detalhe.
    if obra.get("decisor_replicado_fp"):
        obra = dict(obra)  # cópia defensiva — não mutar o dict original
        for k in ("nivel1_nome", "nivel1_cargo", "nivel1_email", "nivel1_linkedin"):
            obra[k] = None
    pode = plano=="PREMIUM" or desbloqueada
    if plano=="GRATUITO": r={k:obra.get(k) for k in CAMPOS_GRATUITO}
    elif plano=="STANDARD": r={k:obra.get(k) for k in CAMPOS_STANDARD}
    else: r={k:v for k,v in obra.items() if not k.startswith("nivel")}
    if pode:
        r["nivel1_clevel"]={"nome":obra.get("nivel1_nome"),"cargo":obra.get("nivel1_cargo"),"email":obra.get("nivel1_email"),"linkedin":obra.get("nivel1_linkedin")}
        r["nivel2_suprimentos"]={"nome":obra.get("nivel2_nome"),"cargo":obra.get("nivel2_cargo"),"email":obra.get("nivel2_email"),"telefone":obra.get("nivel2_telefone")}
    else:
        msg="Upgrade para Premium." if plano=="GRATUITO" else "Desbloqueie por R$ 49,90."
        # LinkedIn + email são dados públicos (Hunter SMTP-validado) — expor mesmo bloqueado para gerar engajamento e prova social
        lk_n1 = (obra.get("nivel1_linkedin") or "").strip() or None
        lk_n2 = (obra.get("nivel2_linkedin") or "").strip() or None
        em_n1 = (obra.get("nivel1_email") or "").strip() or None
        em_n2 = (obra.get("nivel2_email") or "").strip() or None
        r["nivel1_clevel"]={"bloqueado":True,"mensagem":msg,"linkedin":lk_n1,"email":em_n1}
        r["nivel2_suprimentos"]={"bloqueado":True,"mensagem":msg,"linkedin":lk_n2,"email":em_n2}
    tem_nome = bool((obra.get("nivel1_nome") or "").strip())
    tem_email_ou_linkedin = bool((obra.get("nivel1_email") or "").strip()) or bool((obra.get("nivel1_linkedin") or "").strip())
    cargo_valido = _cargo_e_decisor(obra.get("nivel1_cargo"))
    fonte_tipo_oficial = (obra.get("fonte_tipo") or "OFICIAL") != "NOTICIA"
    is_ouro_parcial = tem_nome and cargo_valido and not tem_email_ou_linkedin and fonte_tipo_oficial
    score = _obra_score(obra)
    # Flags do funil (3 estados). Preferimos os booleans vindos do SQL quando
    # presentes — fonte de verdade é a query (decisores_obra-based para Ouro,
    # área-função explícita para Prata, valor+EM_EXECUCAO+sem-decisor para Pipeline).
    is_ouro_sql = obra.get("is_ouro_sql")
    is_prata_match_sql = obra.get("is_prata_match_sql")
    is_pipeline_sql = obra.get("is_pipeline_sql")
    if is_ouro_sql is not None:
        is_ouro = bool(is_ouro_sql)
    else:
        is_ouro = tem_nome and tem_email_ou_linkedin and cargo_valido and fonte_tipo_oficial
    if is_prata_match_sql is not None:
        is_prata = bool(is_prata_match_sql) and not is_ouro
    else:
        is_prata = score >= PRATA_CUTOFF and not is_ouro and not is_ouro_parcial
    is_pipeline = bool(is_pipeline_sql) if is_pipeline_sql is not None else False
    r["is_ouro"] = is_ouro
    r["is_ouro_parcial"] = is_ouro_parcial
    r["is_prata"] = is_prata
    r["is_pipeline"] = is_pipeline
    r["is_bronze"] = obra.get("classificacao_computed") == "BRONZE"
    r["score_prospeccao"] = obra.get("score_prospeccao")
    r["janela_score"] = obra.get("janela_score", 50)
    r["score"] = score
    r["descricao_sintetica"] = bool(obra.get("descricao_sintetica"))
    r["acesso_completo"]=pode; r["pode_desbloquear"]=plano=="STANDARD" and not desbloqueada
    return r

def inferir_setor(n,t):
    txt=f"{n} {t}".lower()
    if any(k in txt for k in ["celulose","papel","klabin","suzano"]): return "Papel e Celulose"
    if any(k in txt for k in ["automot","montadora","gwm"]): return "Automotivo"
    if any(k in txt for k in ["petróleo","refinaria","petrobras"]): return "Petroquímica"
    if any(k in txt for k in ["energia","eólica","solar","hidro"]): return "Energia"
    if any(k in txt for k in ["mineraç","siderurgia","vale"]): return "Mineração"
    if any(k in txt for k in ["ferrovia","fico","fiol"]): return "Ferroviário"
    if any(k in txt for k in ["saneamento","água","esgoto"]): return "Saneamento"
    return "Industrial"

def calc_urgencia(fase):
    return {"LICITACAO_ABERTA":1,"CONTRATACAO":1,"LICENCA_INSTALACAO":2,"FINANCIAMENTO_BNDES":2,"LICENCA_PREVIA":3,"EM_EXECUCAO":3}.get(fase,3)

def calc_score(fase, valor):
    s=50+{"LICITACAO_ABERTA":30,"CONTRATACAO":25,"LICENCA_INSTALACAO":25,"FINANCIAMENTO_BNDES":20,"LICENCA_PREVIA":20,"EM_EXECUCAO":10}.get(fase,0)
    if valor:
        if valor>=5e9: s+=20
        elif valor>=1e9: s+=15
        elif valor>=1e8: s+=10
        elif valor>=1e7: s+=5
    return min(100,s)

def fmt_valor(v):
    if v>=1e9: return f"R$ {v/1e9:.1f} Bi"
    if v>=1e6: return f"R$ {v/1e6:.0f} M"
    return f"R$ {v:,.0f}"

def detectar_nec(setor, desc):
    t=f"{setor} {desc}".lower()
    nec=[]
    mapa={"TERRAPLANAGEM":["terraplenagem","escavação"],"ELETRICA_INDUSTRIAL":["elétrica","subestação"],"MONTAGEM_ELETROMEC":["montagem","eletromecânica"],"CIVIL_TECNICA":["civil","construção"],"TI_INFRAESTRUTURA":["TI","tecnologia","datacenter"],"CATERING":["alimentação","refeitório"],"RH_MOBILIZACAO":["RH","mobilização"]}
    for cat,pals in mapa.items():
        if any(p.lower() in t for p in pals): nec.append(cat)
    for o in ["CIVIL_TECNICA","ELETRICA_INDUSTRIAL"]:
        if o not in nec: nec.append(o)
    return list(set(nec))


class RegReq(BaseModel):
    nome_empresa: str; email: str; senha: str; cnpj: str
    telefone: str=None; segmento: str=None; uf: str=None
    lead_token: Optional[str] = None

class LoginReq(BaseModel):
    email: str; senha: str

class NewsletterSubscribeReq(BaseModel):
    email: str
    nome: Optional[str] = None
    origem: Optional[str] = "footer"

class ObraReq(BaseModel):
    nome: str; empresa: str=None; setor: str
    municipio: Optional[str]=None; uf: str=None; valor_estimado: float=None
    fase: str; descricao: str=None
    nivel1_nome: Optional[str]=None; nivel1_cargo: str=None; nivel1_email: str=None
    nivel2_nome: Optional[str]=None; nivel2_cargo: str=None; nivel2_email: str=None

@asynccontextmanager
async def lifespan(app):
    init_db()
    try:
        from routes.fornecedores import warmup_facetas_cache
        warmup_facetas_cache(get_conn)
        log.info("Facetas cache pre-warmed")
    except Exception as e:
        log.warning(f"Facetas warmup falhou (segue boot): {e}")
    log.info("WiNS Hub iniciado")
    yield

app = FastAPI(title="WiNS Hub", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(CORSMiddleware, allow_origins=["https://winshubcomercial.com.br","https://www.winshubcomercial.com.br","http://localhost:8000","http://127.0.0.1:8000"], allow_methods=["*"], allow_headers=["*"])
from routes.prestadores import build_router as build_prestadores_router
from routes.auto_match_demo import router as auto_match_demo_router
app.include_router(build_prestadores_router(get_conn, requer_auth))
from services.brasilapi import consultar_cnpj, consultar_cnpj_com_erro
from services.hunter import buscar_emails_dominio, buscar_emails_management
from services.cargos_decisores import filtrar_por_cargo_decisor
from sales_intelligence.decisor_gate import decisor_inserivel
from routes.cadastro_prestador import build_router as build_cadastro_router
app.include_router(build_cadastro_router(get_conn, consultar_cnpj))

from routes.dashboard import build_router as build_dashboard_router
app.include_router(build_dashboard_router(get_conn))

from routes.fornecedores import build_router as build_fornecedores_router
app.include_router(build_fornecedores_router(get_conn))

from routes.password_reset import build_router as build_password_reset_router
from routes.auto_match_real import build_auto_match_real_router
from routes.decisor_lookup import build_decisor_lookup_router
app.include_router(build_password_reset_router(get_conn, hash_senha))
app.include_router(auto_match_demo_router)
app.include_router(build_auto_match_real_router(get_conn, requer_auth))
app.include_router(build_decisor_lookup_router(get_conn, requer_auth))

CSP_LOG_PATH = os.getenv("CSP_LOG_PATH", "/var/log/wins_hub/csp-violations.jsonl")

@app.post("/api/csp-report")
@limiter.limit("50/minute")
async def csp_report(request: Request):
    try:
        body = await request.body()
        if not body:
            return JSONResponse({"ok": True})
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse({"ok": True})

        items = []
        if isinstance(payload, dict) and "csp-report" in payload:
            items.append(payload["csp-report"])
        elif isinstance(payload, list):
            for r in payload:
                if isinstance(r, dict) and r.get("type") == "csp-violation":
                    items.append(r.get("body", {}))
        elif isinstance(payload, dict):
            items.append(payload)

        ts = datetime.utcnow().isoformat() + "Z"
        ip = get_real_ip(request)
        ua = (request.headers.get("user-agent") or "")[:500]
        ct = (request.headers.get("content-type") or "").lower()

        try:
            os.makedirs(os.path.dirname(CSP_LOG_PATH), exist_ok=True)
            with open(CSP_LOG_PATH, "a", encoding="utf-8") as f:
                for it in items[:10]:
                    if not isinstance(it, dict):
                        continue
                    record = {
                        "timestamp": ts,
                        "document_uri": (it.get("document-uri") or it.get("documentURL") or "")[:500],
                        "blocked_uri":  (it.get("blocked-uri")  or it.get("blockedURL")  or "")[:500],
                        "violated_directive": (it.get("violated-directive") or it.get("effectiveDirective") or "")[:200],
                        "source_file": (it.get("source-file") or it.get("sourceFile") or "")[:500],
                        "line_number": it.get("line-number") or it.get("lineNumber"),
                        "user_agent": ua,
                        "ip": ip,
                        "content_type": ct,
                    }
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning(f"csp_report falhou ao escrever log: {e}")

        return JSONResponse({"ok": True})
    except Exception as e:
        log.warning(f"csp_report exception: {e}")
        return JSONResponse({"ok": True})

def _cnpj_digits(cnpj: str) -> str:
    return re.sub(r'\D', '', cnpj or '')


@app.get("/api/cnpj/{cnpj}/check")
def consultar_cnpj_check(cnpj: str):
    """Pre-flight do signup: confirma CNPJ + anti-duplicação + pre-fill.

    Retorna:
      - encontrado_fornecedores: bool (já em cache local)
      - ja_tem_prestador: bool (anti-dup; se True → cliente recupera senha)
      - dados_publicos: dict (razão, cnae, porte, uf, etc) ou null
      - wizard_simplificado: bool (true se source=descoberto_via_receita)
      - passos_wizard_estimado: int (5 se simplificado, 6 caso contrário)
    """
    digits = _cnpj_digits(cnpj)
    if len(digits) != 14:
        raise HTTPException(400, "CNPJ inválido. Verifique os dígitos.")

    encontrado = False
    dados_publicos = None
    ja_tem_prestador = False

    conn = get_conn()
    try:
        # 1) anti-dup
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM prestadores WHERE cnpj=%s AND excluido_em IS NULL LIMIT 1",
                (digits,),
            )
            ja_tem_prestador = cur.fetchone() is not None

        # 2) lookup local em fornecedores
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT razao_social, nome_fantasia, cnae_principal, cnae_descricao,
                       porte, porte_descricao, uf, municipio_nome, telefone_1,
                       capital_social, situacao
                FROM fornecedores WHERE cnpj = %s
            """, (digits,))
            row = cur.fetchone()
            if row:
                encontrado = True
                dados_publicos = {
                    "razao_social": row.get("razao_social"),
                    "nome_fantasia": row.get("nome_fantasia"),
                    "cnae_principal": row.get("cnae_principal"),
                    "cnae_descricao": row.get("cnae_descricao"),
                    "porte": row.get("porte"),
                    "porte_descricao": row.get("porte_descricao"),
                    "uf": row.get("uf"),
                    "municipio": row.get("municipio_nome"),
                    "telefone": row.get("telefone_1"),
                    "data_inicio_atividade": None,
                    "capital_social": float(row["capital_social"]) if row.get("capital_social") else None,
                    "situacao": row.get("situacao") or "ATIVA",
                }
    finally:
        conn.close()

    # 3) Se não está em fornecedores, tentar BrasilAPI (gratuito; vai popular fornecedores ao retornar OK)
    if not encontrado:
        dados_brasilapi, erro_tipo = consultar_cnpj_com_erro(digits)
        if erro_tipo == "NAO_ENCONTRADO":
            raise HTTPException(404, "CNPJ não cadastrado na Receita Federal.")
        if erro_tipo == "FORMATO_INVALIDO":
            raise HTTPException(400, "CNPJ inválido. Verifique os dígitos verificadores.")
        if erro_tipo == "RATE_LIMIT":
            raise HTTPException(429, "Muitas consultas. Aguarde alguns segundos.")
        if erro_tipo == "SERVICO_INDISPONIVEL":
            raise HTTPException(503, "Receita Federal temporariamente indisponível.")
        if erro_tipo == "ERRO_REDE" or not dados_brasilapi:
            raise HTTPException(500, "Erro ao consultar Receita Federal.")
        situacao = (dados_brasilapi.get("descricao_situacao_cadastral") or "").upper().strip()
        if situacao != "ATIVA":
            raise HTTPException(422, f"Empresa com situação '{situacao or 'desconhecida'}'. Apenas ATIVAS podem se cadastrar.")
        dados_publicos = {
            "razao_social": dados_brasilapi.get("razao_social"),
            "nome_fantasia": dados_brasilapi.get("nome_fantasia"),
            "cnae_principal": str(dados_brasilapi.get("cnae_fiscal") or ""),
            "cnae_descricao": dados_brasilapi.get("cnae_fiscal_descricao"),
            "porte": dados_brasilapi.get("descricao_porte") or dados_brasilapi.get("porte"),
            "porte_descricao": dados_brasilapi.get("descricao_porte"),
            "uf": dados_brasilapi.get("uf"),
            "municipio": dados_brasilapi.get("municipio"),
            "telefone": str(dados_brasilapi.get("ddd_telefone_1") or ""),
            "data_inicio_atividade": dados_brasilapi.get("data_inicio_atividade"),
            "capital_social": float(dados_brasilapi.get("capital_social") or 0) or None,
            "situacao": "ATIVA",
        }
        # consultar_cnpj_com_erro já fez _upsert_empresa_receita pra popular fornecedores
        encontrado = True

    if ja_tem_prestador:
        return JSONResponse(
            status_code=409,
            content={
                "cnpj": digits,
                "encontrado_fornecedores": encontrado,
                "ja_tem_prestador": True,
                "dados_publicos": dados_publicos,
                "wizard_simplificado": True,
                "passos_wizard_estimado": 5,
                "mensagem": "Este CNPJ já está vinculado a uma conta. Recupere a senha ou contate o suporte.",
            },
        )

    return {
        "cnpj": digits,
        "encontrado_fornecedores": encontrado,
        "ja_tem_prestador": False,
        "dados_publicos": dados_publicos,
        "wizard_simplificado": True,
        "passos_wizard_estimado": 5,
    }


@app.get("/api/cnpj/{cnpj}")
def consultar_cnpj_publico(cnpj: str, request: Request):
    """Consulta CNPJ — tenta cache local em fornecedores primeiro,
    fallback BrasilAPI. Local responde em ~1ms; BrasilAPI cobre o
    universo Brasil mas só expõe campos básicos."""
    digits = _cnpj_digits(cnpj)
    if len(digits) != 14:
        raise HTTPException(422, "CNPJ deve ter 14 dígitos.")

    # 1) Cache local em fornecedores
    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT razao_social, nome_fantasia, uf, municipio_nome,
                           bairro, cep, logradouro, numero, complemento,
                           endereco_completo,
                           telefone_1, telefone_2, email,
                           cnae_principal, cnae_descricao,
                           porte, porte_descricao, capital_social,
                           situacao, fonte_dump_rfb
                    FROM fornecedores
                    WHERE cnpj = %s
                """, (digits,))
                row = cur.fetchone()
        finally:
            conn.close()
        if row:
            endereco = row["endereco_completo"] or " ".join(
                p for p in [row["logradouro"], row["numero"], row["complemento"]] if p
            ) or None
            return {
                "cnpj": digits,
                "fonte": "local_industrial" if row["fonte_dump_rfb"] else "local",
                "razao_social": row["razao_social"],
                "nome_fantasia": row["nome_fantasia"],
                "uf": row["uf"],
                "municipio": row["municipio_nome"],
                "bairro": row["bairro"],
                "cep": row["cep"],
                "endereco": endereco,
                "telefone_1": row["telefone_1"],
                "telefone_2": row["telefone_2"],
                "email": row["email"],
                "cnae_principal": row["cnae_principal"],
                "cnae_descricao": row["cnae_descricao"],
                "porte": row["porte"],
                "porte_descricao": row["porte_descricao"],
                "capital_social": float(row["capital_social"]) if row["capital_social"] is not None else None,
                "situacao": row["situacao"] or "ATIVA",
            }
    except Exception:
        log.exception(f"Lookup local falhou para {digits}; seguindo pra BrasilAPI")

    # 2) Fallback BrasilAPI
    dados, erro_tipo = consultar_cnpj_com_erro(digits)

    if erro_tipo == "NAO_ENCONTRADO":
        raise HTTPException(404, "CNPJ não cadastrado na Receita Federal.")
    if erro_tipo == "FORMATO_INVALIDO":
        raise HTTPException(400, "CNPJ inválido. Verifique os dígitos verificadores.")
    if erro_tipo == "RATE_LIMIT":
        raise HTTPException(429, "Muitas consultas no momento. Aguarde alguns segundos.")
    if erro_tipo == "SERVICO_INDISPONIVEL":
        raise HTTPException(503, "Receita Federal temporariamente indisponível. Tente novamente em alguns minutos.")
    if erro_tipo == "ERRO_REDE" or not dados:
        raise HTTPException(500, "Erro ao consultar Receita Federal.")
    situacao = (dados.get("descricao_situacao_cadastral") or "").upper().strip()
    if situacao != "ATIVA":
        raise HTTPException(422, f"Empresa com situação cadastral '{situacao or 'desconhecida'}'. Apenas empresas ATIVAS podem se cadastrar.")
    return {
        "cnpj": digits,
        "fonte": "brasilapi",
        "razao_social": dados.get("razao_social"),
        "nome_fantasia": dados.get("nome_fantasia"),
        "uf": dados.get("uf"),
        "municipio": dados.get("municipio"),
        "situacao": situacao,
    }

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM    = os.getenv("RESEND_FROM", "WiNS HUB <contato@winshubcomercial.com.br>")
APP_URL        = os.getenv("APP_URL", "https://winshubcomercial.com.br")

def enviar_email(to: str, subject: str, html: str, contexto: str = "email") -> bool:
    if not RESEND_API_KEY:
        log.error("RESEND_API_KEY nao configurada. %s NAO enviado pra %s.", contexto, to)
        return False
    payload = {"from": RESEND_FROM, "to": [to], "subject": subject, "html": html}
    headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post("https://api.resend.com/emails", json=payload, headers=headers)
        if r.status_code >= 400:
            log.error("Resend (%s) respondeu %s: %s", contexto, r.status_code, r.text)
            return False
        log.info("%s enviado pra %s (Resend ID: %s)", contexto, to, r.json().get("id"))
        return True
    except Exception as exc:
        log.exception("Falha ao enviar %s: %s", contexto, exc)
        return False

def _enviar_email_confirmacao(email: str, nome: str, token: str) -> None:
    link = f"{APP_URL}/confirmar/{token}"
    saudacao = f"Ola{(' ' + nome) if nome else ''},"
    html = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0a0e1a;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#e8ecf4;">
  <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#0a0e1a;">
    <tr><td align="center" style="padding:40px 16px;">
      <table cellpadding="0" cellspacing="0" border="0" width="540" style="max-width:540px;background:#0f1525;border:1px solid #1f2942;border-radius:12px;">
        <tr><td style="padding:32px 36px 8px 36px;">
          <span style="font-size:24px;font-weight:800;letter-spacing:1.5px;color:#f5b800;">WiNS HUB</span>
          <span style="font-size:11px;color:#6b7693;text-transform:uppercase;letter-spacing:1.2px;margin-left:8px;">Inteligencia Comercial</span>
        </td></tr>
        <tr><td style="padding:24px 36px 8px 36px;">
          <h2 style="margin:0;font-size:18px;font-weight:600;color:#e8ecf4;">Confirme seu acesso</h2>
        </td></tr>
        <tr><td style="padding:8px 36px 16px 36px;color:#a8b1c9;font-size:14px;line-height:1.6;">
          <p style="margin:0 0 12px 0;">{saudacao}</p>
          <p style="margin:0 0 12px 0;">Recebemos seu cadastro no WiNS HUB. Para ativar sua conta e comecar a usar a plataforma, confirme seu email clicando no botao abaixo:</p>
        </td></tr>
        <tr><td align="center" style="padding:8px 36px 24px 36px;">
          <a href="{link}" style="display:inline-block;background:#f5b800;color:#0a0e1a;text-decoration:none;padding:12px 28px;border-radius:6px;font-weight:700;letter-spacing:0.5px;">Confirmar e-mail</a>
        </td></tr>
        <tr><td style="padding:0 36px 24px 36px;color:#6b7693;font-size:12px;line-height:1.6;">
          <p style="margin:0 0 8px 0;">Ou copie este link no navegador:</p>
          <p style="margin:0 0 16px 0;word-break:break-all;color:#a8b1c9;">{link}</p>
          <p style="margin:0 0 8px 0;">Este link expira em <strong style="color:#a8b1c9;">24 horas</strong>.</p>
          <p style="margin:0;">Se voce nao se cadastrou no WiNS HUB, ignore este email.</p>
        </td></tr>
        <tr><td style="padding:16px 36px 24px 36px;border-top:1px solid #1f2942;color:#6b7693;font-size:11px;text-align:center;letter-spacing:0.3px;">
          WiNS HUB &middot; Inteligencia Comercial<br>
          <a href="https://winshubcomercial.com.br" style="color:#6b7693;text-decoration:none;">winshubcomercial.com.br</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    enviar_email(email, "Confirme seu acesso ao WiNS HUB", html, contexto="Email de confirmacao")


def _enviar_email_newsletter_confirmacao(email: str, nome: Optional[str], token: str) -> None:
    link_confirmar = f"{APP_URL}/api/newsletter/confirmar?token={token}"
    saudacao = f"Ola{(' ' + nome) if nome else ''},"
    html = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0a0e1a;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#e8ecf4;">
  <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#0a0e1a;">
    <tr><td align="center" style="padding:40px 16px;">
      <table cellpadding="0" cellspacing="0" border="0" width="540" style="max-width:540px;background:#0f1525;border:1px solid #1f2942;border-radius:12px;">
        <tr><td style="padding:32px 36px 8px 36px;">
          <span style="font-size:24px;font-weight:800;letter-spacing:1.5px;color:#f5b800;">WiNS HUB</span>
          <span style="font-size:11px;color:#6b7693;text-transform:uppercase;letter-spacing:1.2px;margin-left:8px;">Inteligencia Comercial</span>
        </td></tr>
        <tr><td style="padding:24px 36px 8px 36px;">
          <h2 style="margin:0;font-size:18px;font-weight:600;color:#e8ecf4;">Confirme sua inscricao</h2>
        </td></tr>
        <tr><td style="padding:8px 36px 16px 36px;color:#a8b1c9;font-size:14px;line-height:1.6;">
          <p style="margin:0 0 12px 0;">{saudacao}</p>
          <p style="margin:0 0 12px 0;">Voce solicitou receber a newsletter semanal do WiNS HUB com as principais obras de infraestrutura do Brasil. Para confirmar, clique no botao abaixo:</p>
        </td></tr>
        <tr><td align="center" style="padding:8px 36px 24px 36px;">
          <a href="{link_confirmar}" style="display:inline-block;background:#f5b800;color:#0a0e1a;text-decoration:none;padding:12px 28px;border-radius:6px;font-weight:700;letter-spacing:0.5px;">Confirmar inscricao</a>
        </td></tr>
        <tr><td style="padding:0 36px 24px 36px;color:#6b7693;font-size:12px;line-height:1.6;">
          <p style="margin:0 0 8px 0;">Ou copie este link no navegador:</p>
          <p style="margin:0 0 16px 0;word-break:break-all;color:#a8b1c9;">{link_confirmar}</p>
          <p style="margin:0;">Se voce nao se inscreveu, ignore este email.</p>
        </td></tr>
        <tr><td style="padding:16px 36px 24px 36px;border-top:1px solid #1f2942;color:#6b7693;font-size:11px;text-align:center;letter-spacing:0.3px;">
          WiNS HUB &middot; Inteligencia Comercial<br>
          <a href="https://winshubcomercial.com.br" style="color:#6b7693;text-decoration:none;">winshubcomercial.com.br</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    enviar_email(email, "Confirme sua inscricao na newsletter WiNS HUB", html, contexto="Newsletter confirmacao")


@app.post("/api/newsletter/subscribe")
@limiter.limit("3/minute")
async def newsletter_subscribe(request: Request, req: NewsletterSubscribeReq, bt: BackgroundTasks):
    email = (req.email or "").strip().lower()
    if "@" not in email or "." not in email or len(email) < 5 or len(email) > 200:
        raise HTTPException(400, "Email invalido.")
    nome = (req.nome or "").strip()[:200] or None
    origem = (req.origem or "footer").strip()[:50]
    ip = request.client.host if request.client else None
    token = secrets.token_urlsafe(32)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT confirmado_em, ativo FROM newsletter_subscribers WHERE email=%s",
                (email,),
            )
            existente = cur.fetchone()
            if existente:
                confirmado_em, ativo = existente
                if confirmado_em and ativo:
                    return {"ok": True, "status": "ja_inscrito",
                            "mensagem": "Voce ja esta inscrito. Obrigado!"}
                cur.execute(
                    "UPDATE newsletter_subscribers SET token_confirmacao=%s, ativo=TRUE, "
                    "cancelado_em=NULL, nome=COALESCE(%s, nome), origem=COALESCE(%s, origem), ip=%s "
                    "WHERE email=%s",
                    (token, nome, origem, ip, email),
                )
            else:
                cur.execute(
                    "INSERT INTO newsletter_subscribers "
                    "(email, nome, token_confirmacao, ip, origem) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (email, nome, token, ip, origem),
                )
        conn.commit()
    finally:
        conn.close()
    bt.add_task(_enviar_email_newsletter_confirmacao, email, nome, token)
    return {"ok": True, "status": "pendente",
            "mensagem": "Confira seu email pra confirmar a inscricao."}


@app.get("/api/newsletter/confirmar")
async def newsletter_confirmar(token: str):
    if not token or len(token) < 20:
        raise HTTPException(400, "Token invalido.")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE newsletter_subscribers "
                "SET confirmado_em = COALESCE(confirmado_em, now()), ativo = TRUE, cancelado_em = NULL "
                "WHERE token_confirmacao = %s "
                "RETURNING email, confirmado_em",
                (token,),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Token nao encontrado.")
    return {"ok": True, "email": row[0], "mensagem": "Inscricao confirmada. Obrigado!"}


@app.get("/api/newsletter/cancelar")
async def newsletter_cancelar(token: str):
    if not token or len(token) < 20:
        raise HTTPException(400, "Token invalido.")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE newsletter_subscribers "
                "SET ativo = FALSE, cancelado_em = now() "
                "WHERE token_confirmacao = %s "
                "RETURNING email",
                (token,),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Token nao encontrado.")
    return {"ok": True, "email": row[0], "mensagem": "Inscricao cancelada."}


RANKING_FASES_OK = ('PLANEJAMENTO', 'EM_EXECUCAO', 'LICENCA_INSTALACAO', 'LICENCA_PREVIA')
RANKING_VALOR_MIN = 500_000_000
RANKING_TOP_N = 50

SETOR_LABEL = {
    'PETROLEO_GAS': 'Petróleo & Gás',
    'ENERGIA': 'Energia',
    'LOGISTICO': 'Logístico',
    'INDUSTRIAL': 'Industrial',
    'INFRAESTRUTURA': 'Infraestrutura',
    'PORTUARIO': 'Portuário',
    'MINERACAO': 'Mineração',
    'SANEAMENTO': 'Saneamento',
}
FASE_LABEL = {
    'PLANEJAMENTO': 'Planejamento',
    'EM_EXECUCAO': 'Em execução',
    'LICENCA_INSTALACAO': 'Licença de instalação',
    'LICENCA_PREVIA': 'Licença prévia',
}


def _fmt_brl_curto(v):
    if v is None: return "—"
    try: v = float(v)
    except Exception: return "—"
    if v >= 1e9:  return f"R$ {v/1e9:.2f} bi".replace(".", ",")
    if v >= 1e6:  return f"R$ {v/1e6:.0f} mi".replace(".", ",")
    return f"R$ {v:,.0f}".replace(",", ".")


def _ranking_query(top_n: int = RANKING_TOP_N):
    sql = """
        SELECT DISTINCT ON (LOWER(nome), LOWER(empresa))
               id::text AS id, nome, empresa, setor, uf, fase, valor_estimado
        FROM obras
        WHERE fase = ANY(%s)
          AND valor_estimado IS NOT NULL
          AND valor_estimado >= %s
          AND (visivel IS NULL OR visivel = true)
          AND cnpj_status = 'ok'
          AND fonte_tipo = 'OFICIAL'
          AND empresa IS NOT NULL
          AND nome IS NOT NULL
        ORDER BY LOWER(nome), LOWER(empresa), valor_estimado DESC
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (list(RANKING_FASES_OK), RANKING_VALOR_MIN))
            rows = cur.fetchall()
    finally:
        conn.close()
    rows.sort(key=lambda r: float(r["valor_estimado"] or 0), reverse=True)
    return rows[:top_n]


@app.get("/api/ranking-megaobras")
async def ranking_megaobras_json():
    obras = _ranking_query(RANKING_TOP_N)
    total_valor = sum(float(o["valor_estimado"] or 0) for o in obras)
    por_setor: dict = {}
    por_uf: dict = {}
    for o in obras:
        s = o.get("setor") or "OUTROS"
        u = o.get("uf") or "—"
        por_setor[s] = por_setor.get(s, 0) + 1
        por_uf[u] = por_uf.get(u, 0) + 1
    items = []
    for i, o in enumerate(obras, 1):
        items.append({
            "pos": i,
            "id": o["id"],
            "nome": o["nome"],
            "empresa": o["empresa"],
            "setor": o["setor"],
            "setor_label": SETOR_LABEL.get(o["setor"] or "", o["setor"] or ""),
            "uf": o["uf"],
            "fase": o["fase"],
            "fase_label": FASE_LABEL.get(o["fase"] or "", o["fase"] or ""),
            "valor_estimado": float(o["valor_estimado"]) if o["valor_estimado"] else None,
            "valor_formatado": _fmt_brl_curto(o["valor_estimado"]),
        })
    return {
        "items": items,
        "total_valor": total_valor,
        "total_valor_formatado": _fmt_brl_curto(total_valor),
        "n": len(items),
        "por_setor": [{"setor": k, "label": SETOR_LABEL.get(k, k), "n": v} for k, v in sorted(por_setor.items(), key=lambda x: -x[1])],
        "por_uf": [{"uf": k, "n": v} for k, v in sorted(por_uf.items(), key=lambda x: -x[1])],
        "atualizado_em": datetime.utcnow().isoformat() + "Z",
    }


class RankingPdfReq(BaseModel):
    email: str
    nome: Optional[str] = None


@app.post("/api/ranking-megaobras/pdf-request")
@limiter.limit("3/minute")
async def ranking_pdf_request(request: Request, req: RankingPdfReq, bt: BackgroundTasks):
    email = (req.email or "").strip().lower()
    if "@" not in email or "." not in email or len(email) < 5 or len(email) > 200:
        raise HTTPException(400, "Email invalido.")
    nome = (req.nome or "").strip()[:200] or None
    ip = request.client.host if request.client else None
    token = secrets.token_urlsafe(32)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT token_confirmacao FROM newsletter_subscribers WHERE email=%s", (email,))
            existente = cur.fetchone()
            if existente:
                token = existente[0]
                cur.execute(
                    "UPDATE newsletter_subscribers SET ativo=TRUE, cancelado_em=NULL, "
                    "nome=COALESCE(%s, nome), origem=COALESCE(origem,'ranking_pdf'), ip=%s "
                    "WHERE email=%s",
                    (nome, ip, email),
                )
            else:
                cur.execute(
                    "INSERT INTO newsletter_subscribers "
                    "(email, nome, token_confirmacao, ip, origem) "
                    "VALUES (%s, %s, %s, %s, 'ranking_pdf')",
                    (email, nome, token, ip),
                )
        conn.commit()
    finally:
        conn.close()
    bt.add_task(_enviar_email_newsletter_confirmacao, email, nome, token)
    return {"ok": True, "pdf_url": f"/ranking-megaobras.pdf?token={token}", "email": email}


@app.get("/ranking-megaobras.pdf")
async def ranking_megaobras_pdf(token: str):
    if not token or len(token) < 20:
        raise HTTPException(400, "Token invalido.")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM newsletter_subscribers WHERE token_confirmacao=%s", (token,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Token nao encontrado.")

    obras = _ranking_query(RANKING_TOP_N)
    if not obras:
        raise HTTPException(503, "Sem dados pra gerar ranking no momento.")

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    )
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=18*mm, bottomMargin=16*mm,
        title="WiNS HUB — Maiores Megaobras do Brasil",
    )
    styles = getSampleStyleSheet()
    BRAND = colors.HexColor("#0a0e1a")
    GOLD  = colors.HexColor("#f5b800")
    GRAY1 = colors.HexColor("#6b7693")
    GRAY2 = colors.HexColor("#1f2942")
    LIGHT = colors.HexColor("#f8fafc")
    DARK_TXT = colors.HexColor("#0f1525")

    h_brand = ParagraphStyle('hbrand', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=18, textColor=GOLD, spaceAfter=2, leading=20)
    h_sub = ParagraphStyle('hsub', parent=styles['Normal'],
        fontName='Helvetica', fontSize=9, textColor=GRAY1, spaceAfter=14, leading=11)
    h_title = ParagraphStyle('htitle', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=22, textColor=DARK_TXT, spaceAfter=4, leading=26)
    h_meta = ParagraphStyle('hmeta', parent=styles['Normal'],
        fontName='Helvetica', fontSize=10, textColor=GRAY1, spaceAfter=18, leading=12)
    h_section = ParagraphStyle('hsec', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=12, textColor=DARK_TXT, spaceAfter=8, leading=14)
    h_foot = ParagraphStyle('hfoot', parent=styles['Normal'],
        fontName='Helvetica', fontSize=8, textColor=GRAY1, alignment=TA_CENTER, leading=10)

    story = []
    story.append(Paragraph("WiNS HUB <font size=8 color='#6b7693'>· INTELIGÊNCIA COMERCIAL</font>", h_brand))
    story.append(Paragraph("Maiores Megaobras de Infraestrutura — Brasil", h_title))
    data_str = datetime.utcnow().strftime("%d/%m/%Y")
    total_v = sum(float(o["valor_estimado"] or 0) for o in obras)
    story.append(Paragraph(
        f"Top {len(obras)} obras em planejamento ou execução · "
        f"capex agregado <b>{_fmt_brl_curto(total_v)}</b> · "
        f"atualizado em {data_str}", h_meta))

    story.append(Paragraph("Top 10 — Destaque", h_section))
    top10_data = [["#", "Obra", "Empresa", "Setor", "UF", "Fase", "Valor"]]
    for i, o in enumerate(obras[:10], 1):
        top10_data.append([
            str(i),
            (o["nome"] or "")[:50],
            (o["empresa"] or "")[:32],
            SETOR_LABEL.get(o["setor"] or "", o["setor"] or "—"),
            o["uf"] or "—",
            FASE_LABEL.get(o["fase"] or "", o["fase"] or "—"),
            _fmt_brl_curto(o["valor_estimado"]),
        ])
    t10 = Table(top10_data, colWidths=[8*mm, 56*mm, 36*mm, 22*mm, 10*mm, 22*mm, 22*mm], repeatRows=1)
    t10.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BRAND),
        ('TEXTCOLOR',  (0,0), (-1,0), GOLD),
        ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0,0), (-1,0), 8),
        ('FONTSIZE',   (0,1), (-1,-1), 8),
        ('FONTNAME',   (-1,1), (-1,-1), 'Helvetica-Bold'),
        ('TEXTCOLOR',  (-1,1), (-1,-1), GOLD),
        ('TEXTCOLOR',  (0,1), (-2,-1), DARK_TXT),
        ('ALIGN',      (0,0), (0,-1), 'CENTER'),
        ('ALIGN',      (-1,0), (-1,-1), 'RIGHT'),
        ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
        ('LINEBELOW',  (0,0), (-1,0), 0.5, GOLD),
        ('LINEBELOW',  (0,1), (-1,-1), 0.25, GRAY2),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT]),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(t10)

    if len(obras) > 10:
        story.append(Spacer(1, 14))
        story.append(Paragraph(f"Posições 11–{len(obras)}", h_section))
        rest = [["#", "Obra", "Empresa", "Setor", "UF", "Fase", "Valor"]]
        for i, o in enumerate(obras[10:], 11):
            rest.append([
                str(i),
                (o["nome"] or "")[:50],
                (o["empresa"] or "")[:32],
                SETOR_LABEL.get(o["setor"] or "", o["setor"] or "—"),
                o["uf"] or "—",
                FASE_LABEL.get(o["fase"] or "", o["fase"] or "—"),
                _fmt_brl_curto(o["valor_estimado"]),
            ])
        tr = Table(rest, colWidths=[8*mm, 56*mm, 36*mm, 22*mm, 10*mm, 22*mm, 22*mm], repeatRows=1)
        tr.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), BRAND),
            ('TEXTCOLOR',  (0,0), (-1,0), GOLD),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (-1,0), 7.5),
            ('FONTSIZE',   (0,1), (-1,-1), 7.5),
            ('FONTNAME',   (-1,1), (-1,-1), 'Helvetica-Bold'),
            ('TEXTCOLOR',  (-1,1), (-1,-1), GOLD),
            ('TEXTCOLOR',  (0,1), (-2,-1), DARK_TXT),
            ('ALIGN',      (0,0), (0,-1), 'CENTER'),
            ('ALIGN',      (-1,0), (-1,-1), 'RIGHT'),
            ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
            ('LINEBELOW',  (0,0), (-1,0), 0.5, GOLD),
            ('LINEBELOW',  (0,1), (-1,-1), 0.2, GRAY2),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT]),
            ('LEFTPADDING', (0,0), (-1,-1), 4),
            ('RIGHTPADDING', (0,0), (-1,-1), 4),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ]))
        story.append(tr)

    story.append(Spacer(1, 22))
    story.append(Paragraph(
        "Fonte: ANP, ANEEL, ANTAQ, IBAMA, BNDES e portais oficiais. "
        "Compilado por WiNS HUB · winshubcomercial.com.br · Os valores são os declarados pelas fontes oficiais.",
        h_foot))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()

    fname = f"ranking-megaobras-{datetime.utcnow().strftime('%Y-%m-%d')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/auth/registro")
@limiter.limit("3/minute")
async def registro(request: Request, req: RegReq, bt: BackgroundTasks):
    digits = _cnpj_digits(req.cnpj)
    if len(digits) != 14:
        raise HTTPException(400, "CNPJ deve ter 14 dígitos.")
    # Detectar se CNPJ já estava em fornecedores ANTES do consultar (define source)
    _conn_chk = get_conn()
    try:
        with _conn_chk.cursor() as _cur_chk:
            _cur_chk.execute("SELECT 1 FROM fornecedores WHERE cnpj=%s LIMIT 1", (digits,))
            ja_estava_em_fornecedores = _cur_chk.fetchone() is not None
    finally:
        _conn_chk.close()
    source_signup = 'descoberto_via_receita' if ja_estava_em_fornecedores else 'cadastro_manual'

    dados, erro_tipo = consultar_cnpj_com_erro(digits)
    if erro_tipo == "NAO_ENCONTRADO":
        raise HTTPException(400, "CNPJ não cadastrado na Receita Federal.")
    if erro_tipo == "FORMATO_INVALIDO":
        raise HTTPException(400, "CNPJ inválido. Verifique os dígitos verificadores.")
    if erro_tipo == "RATE_LIMIT":
        raise HTTPException(429, "Muitas consultas no momento. Aguarde alguns segundos.")
    if erro_tipo == "SERVICO_INDISPONIVEL":
        raise HTTPException(503, "Receita Federal temporariamente indisponível. Tente novamente.")
    if erro_tipo == "ERRO_REDE" or not dados:
        raise HTTPException(500, "Erro ao consultar Receita Federal.")
    situacao = (dados.get("descricao_situacao_cadastral") or "").upper().strip()
    if situacao != "ATIVA":
        raise HTTPException(400, f"Empresa com situação cadastral '{situacao or 'desconhecida'}'. Apenas empresas ATIVAS podem se cadastrar.")
    razao_social = dados.get("razao_social") or req.nome_empresa
    token_email = secrets.token_urlsafe(32)
    expiry = datetime.utcnow() + timedelta(hours=24)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM prestadores WHERE email=%s", (req.email,))
            if cur.fetchone():
                raise HTTPException(400, "Email já cadastrado.")
            cur.execute("SELECT nome_empresa FROM prestadores WHERE cnpj=%s", (digits,))
            existente = cur.fetchone()
            if existente:
                raise HTTPException(400, f"Este CNPJ já está cadastrado (empresa: {existente[0]}). Faça login ou recupere a senha.")
            cur.execute(
                "INSERT INTO prestadores (nome_empresa,razao_social,email,senha_hash,telefone,cnpj,segmento,uf,"
                " status,email_token,email_token_expiry,source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pendente',%s,%s,%s) RETURNING id",
                (req.nome_empresa, razao_social, req.email, hash_senha(req.senha),
                 req.telefone, digits, req.segmento, req.uf or dados.get("uf"),
                 token_email, expiry, source_signup)
            )
            r = cur.fetchone()
            cur.execute(
                "INSERT INTO prestador_empresas (prestador_id, cnpj, razao_social, tipo, ativo) "
                "VALUES (%s, %s, %s, 'proprio', true) "
                "ON CONFLICT (prestador_id, cnpj) DO NOTHING",
                (str(r[0]), digits, razao_social)
            )
            # Vincular lead outbound se signup veio via referral PDF
            if req.lead_token:
                try:
                    cur.execute(
                        "UPDATE leads_outbound SET cadastrou_em=NOW(), prestador_id=%s, status='CADASTROU' "
                        "WHERE pdf_token=%s AND prestador_id IS NULL",
                        (str(r[0]), req.lead_token)
                    )
                    log.info("lead_outbound vinculado via lead_token=%s prestador=%s", req.lead_token, str(r[0]))
                except Exception as e:
                    log.warning("Falha ao vincular lead_outbound (token=%s): %s", req.lead_token, e)
        conn.commit()
        bt.add_task(_enviar_email_confirmacao, req.email, req.nome_empresa, token_email)
        return {
            "ok": True,
            "status": "pendente",
            "email": req.email,
            "razao_social": razao_social,
            "mensagem": "Conta criada. Verifique seu e-mail e clique no link de confirmação para ativar."
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally: conn.close()

@app.get("/confirmar/{token}")
def confirmar_email(token: str):
    """Click no link do email — valida token, ativa conta, redireciona pra /login."""
    from fastapi.responses import RedirectResponse
    if not token or len(token) < 20:
        return RedirectResponse(url="/login?msg=token_invalido", status_code=302)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, email, status, email_token_expiry FROM prestadores WHERE email_token=%s",
                (token,)
            )
            row = cur.fetchone()
            if not row:
                return RedirectResponse(url="/login?msg=token_invalido", status_code=302)
            if row["status"] == "ativo":
                return RedirectResponse(url="/login?msg=ja_confirmado", status_code=302)
            expiry = row["email_token_expiry"]
            if expiry and datetime.now(expiry.tzinfo) > expiry:
                return RedirectResponse(url="/login?msg=token_expirado", status_code=302)
            cur.execute(
                "UPDATE prestadores SET status='ativo', email_token_expiry=NULL WHERE id=%s",
                (row["id"],)
            )
        conn.commit()
        return RedirectResponse(url="/login?msg=email_confirmado", status_code=302)
    except Exception as e:
        conn.rollback()
        log.exception("Falha confirmar email: %s", e)
        return RedirectResponse(url="/login?msg=erro_servidor", status_code=302)
    finally:
        conn.close()

@app.post("/api/auth/login")
@limiter.limit("5/minute")
async def login(request: Request, req: LoginReq):
    conn=get_conn()
    ip = request.client.host if request.client else None
    ua = (request.headers.get("user-agent") or "")[:500]
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id,senha_hash,plano,nome_empresa,cnpj,status,COALESCE(is_representante,false) AS is_representante FROM prestadores WHERE email=%s AND ativo=TRUE",(req.email,))
            p=cur.fetchone()
        ok = bool(p and verificar_senha(req.senha, p["senha_hash"]))
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO acessos_log (prestador_id, email, ip, user_agent, sucesso) VALUES (%s,%s,%s,%s,%s)",
                    (str(p["id"]) if (ok and p) else None, req.email, ip, ua, ok)
                )
            conn.commit()
        except Exception as e:
            log.warning(f"acessos_log insert falhou: {e}")
            conn.rollback()
        if not ok: raise HTTPException(401,"Email ou senha incorretos.")
        if p.get("status") and p["status"] != "ativo":
            raise HTTPException(403, "Confirme seu e-mail antes de acessar. Verifique sua caixa de entrada (e a pasta de spam).")
        with conn.cursor() as cur: cur.execute("UPDATE prestadores SET ultimo_acesso=NOW() WHERE id=%s",(str(p["id"]),))
        conn.commit()

        prestador_id = str(p["id"])
        matches_status = "pronto"
        if p.get("cnpj"):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(m.gerado_em) FROM matches_obra_prestador m WHERE m.cnpj = %s",
                    (p["cnpj"],),
                )
                ultima = cur.fetchone()[0]
            if ultima is None or (datetime.now(ultima.tzinfo) - ultima) > timedelta(hours=MATCHES_CACHE_HORAS):
                _disparar_matchmaking_prestador(prestador_id)
                matches_status = "gerando"

        return {"token":criar_token(prestador_id,p["plano"],p.get("is_representante",False),email=req.email,nome=p.get("nome_empresa")),"plano":p["plano"],"nome":p["nome_empresa"],"matches_status":matches_status,"is_admin":bool(req.email=="williamvnvn@gmail.com")}
    finally: conn.close()

@app.get("/api/auth/perfil")
async def perfil(u=Depends(requer_auth)):
    conn=get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id,nome_empresa,email,telefone,cnpj,segmento,uf,plano,COALESCE(creditos_ganhos,0) AS creditos_ganhos,COALESCE(creditos_consumidos,0) AS creditos_consumidos,COALESCE(creditos_ganhos,0)-COALESCE(creditos_consumidos,0) AS creditos_saldo,badge_verificador,acesso_antecipado,contribuicoes_total,COALESCE(is_representante,false) AS is_representante,COALESCE(senha_temporaria,false) AS senha_temporaria,criado_em FROM prestadores WHERE id=%s",(u["sub"],))
        d=cur.fetchone()
    conn.close()
    if not d: raise HTTPException(404,"Não encontrado.")
    return dict(d)


@app.get("/api/perfil/obras-desbloqueadas")
async def perfil_obras_desbloqueadas(u=Depends(requer_auth)):
    """Obras desbloqueadas com decisor + último contato CRM."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (i.obra_id)
                    i.obra_id::text AS id,
                    o.nome, o.empresa, o.uf, o.fase, o.setor,
                    o.valor_estimado, o.cnpj,
                    i.plano_momento, i.valor_cobrado,
                    i.criado_em AS desbloqueado_em,
                    d.nome AS decisor_nome,
                    d.cargo AS decisor_cargo,
                    d.email AS decisor_email,
                    d.linkedin_url AS decisor_linkedin,
                    d.tipo_cargo AS decisor_tipo,
                    cl.id::text AS contato_id,
                    cl.resposta_tipo AS contato_status,
                    cl.confirmado_pelo_decisor,
                    cl.status_obra_reportado,
                    cl.proximo_followup_em,
                    cl.notas,
                    cl.enviado_em AS contato_em
                FROM interacoes i
                JOIN obras o ON o.id = i.obra_id
                LEFT JOIN LATERAL (
                    SELECT nome, cargo, email, linkedin_url, tipo_cargo
                    FROM decisores_obra
                    WHERE obra_id = i.obra_id AND excluido_em IS NULL
                    ORDER BY (email IS NOT NULL AND email <> '') DESC,
                             (linkedin_url IS NOT NULL) DESC
                    LIMIT 1
                ) d ON true
                LEFT JOIN LATERAL (
                    SELECT id, resposta_tipo, confirmado_pelo_decisor,
                           status_obra_reportado, proximo_followup_em,
                           notas, enviado_em
                    FROM contatos_log
                    WHERE obra_id = i.obra_id AND prestador_id = %s
                    ORDER BY enviado_em DESC
                    LIMIT 1
                ) cl ON true
                WHERE i.prestador_id = %s
                  AND i.tipo = 'DESBLOQUEIO'
                ORDER BY i.obra_id, i.criado_em DESC
            """, (u["sub"], u["sub"]))
            rows = cur.fetchall()

        result = []
        for r in rows:
            row = dict(r)
            if row.get("valor_estimado"):
                row["valor_estimado"] = float(row["valor_estimado"])
            result.append(row)

        return {"obras": result, "total": len(result)}
    finally:
        conn.close()

@app.get("/api/perfil/obras-desbloqueadas/export.csv")
async def perfil_obras_desbloqueadas_csv(u=Depends(requer_auth)):
    import csv as _csv
    pid = u["sub"]
    sql = """
        WITH obras_desbloq AS (
            SELECT DISTINCT ON (i.obra_id)
                   i.obra_id, i.criado_em AS desbloqueado_em
            FROM interacoes i
            WHERE i.prestador_id = %s AND i.tipo = 'DESBLOQUEIO'
            ORDER BY i.obra_id, i.criado_em DESC
        )
        SELECT
            o.nome AS obra_nome,
            o.empresa, o.uf, o.fase, o.setor,
            o.valor_estimado, od.desbloqueado_em,
            d.nome  AS decisor_nome,
            d.cargo AS decisor_cargo,
            d.email AS decisor_email,
            d.linkedin_url AS decisor_linkedin
        FROM obras_desbloq od
        JOIN obras o ON o.id = od.obra_id
        LEFT JOIN decisores_obra d
               ON d.obra_id = od.obra_id
              AND d.excluido_em IS NULL
              AND (d.email IS NOT NULL OR d.linkedin_url IS NOT NULL)
        WHERE (o.visivel IS NULL OR o.visivel = true)
        ORDER BY od.desbloqueado_em DESC, o.nome, d.nome
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (pid,))
            rows = cur.fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    buf.write("# Uso interno exclusivo. Redistribuição proibida — Termos de Uso winshubcomercial.com.br\n")
    writer = _csv.writer(buf, quoting=_csv.QUOTE_MINIMAL)
    writer.writerow([
        "obra_nome", "empresa", "uf", "fase", "setor",
        "valor_estimado", "desbloqueado_em",
        "decisor_nome", "decisor_cargo", "decisor_email", "decisor_linkedin",
    ])
    for r in rows:
        (obra_nome, empresa, uf, fase, setor, valor, desbloqueado_em,
         dec_nome, dec_cargo, dec_email, dec_linkedin) = r
        writer.writerow([
            obra_nome or "",
            empresa or "",
            uf or "",
            FASE_LABEL.get(fase or "", fase or ""),
            SETOR_LABEL.get(setor or "", setor or ""),
            f"{float(valor):.2f}" if valor is not None else "",
            desbloqueado_em.strftime("%Y-%m-%d %H:%M:%S") if desbloqueado_em else "",
            dec_nome or "",
            dec_cargo or "",
            dec_email or "",
            dec_linkedin or "",
        ])

    csv_bytes = ("﻿" + buf.getvalue()).encode("utf-8")
    buf.close()
    fname = f"minhas-obras-{datetime.utcnow().strftime('%Y-%m-%d')}.csv"
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/prestador/alertas/preferencias")
async def alertas_get_prefs(u=Depends(requer_auth)):
    """Retorna preferencias + total matches + CNAEs + historico de alertas."""
    from datetime import datetime, timezone, timedelta
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            pid = u["sub"]

            cur.execute("""
                INSERT INTO alertas_preferencias (prestador_id)
                VALUES (%s)
                ON CONFLICT (prestador_id) DO NOTHING
            """, (pid,))
            conn.commit()
            cur.execute("""
                SELECT ativo, frequencia, score_minimo
                FROM alertas_preferencias WHERE prestador_id=%s
            """, (pid,))
            prefs = dict(cur.fetchone() or {"ativo": True, "frequencia": "semanal", "score_minimo": 50})

            cur.execute("""
                SELECT COUNT(DISTINCT mo.obra_id) AS total
                FROM matches_obra_prestador mo
                JOIN prestador_empresas pe ON pe.cnpj = mo.cnpj
                WHERE pe.prestador_id=%s AND mo.score >= %s
            """, (pid, prefs["score_minimo"]))
            total_matches = (cur.fetchone() or {}).get("total", 0)

            cur.execute("""
                SELECT DISTINCT f.cnae_principal, f.divisao_cnae
                FROM prestador_empresas pe
                JOIN fornecedores f ON f.cnpj = pe.cnpj
                WHERE pe.prestador_id=%s
                  AND f.cnae_principal IS NOT NULL
                LIMIT 10
            """, (pid,))
            cnaes = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT canal, obras_count, enviado_em
                FROM alertas_enviados
                WHERE prestador_id=%s
                ORDER BY enviado_em DESC
                LIMIT 5
            """, (pid,))
            historico = [dict(r) for r in cur.fetchall()]

            since = datetime.now(timezone.utc) - timedelta(days=7)
            cur.execute("""
                SELECT DISTINCT ON (o.id)
                    o.id, o.nome, mo.score, o.fase, o.uf, o.setor
                FROM matches_obra_prestador mo
                JOIN prestador_empresas pe ON pe.cnpj = mo.cnpj
                JOIN obras o ON o.id = mo.obra_id
                WHERE pe.prestador_id=%s
                  AND mo.gerado_em > %s
                  AND mo.score >= %s
                  AND o.fase IN ('PLANEJAMENTO','EM_EXECUCAO','LICENCA_INSTALACAO','LICENCA_PREVIA')
                  AND (o.cnpj_status IS NULL OR o.cnpj_status='ok')
                  AND (o.visivel IS NULL OR o.visivel=true)
                ORDER BY o.id, mo.score DESC
                LIMIT 3
            """, (pid, since, prefs["score_minimo"]))
            obras_semana = [dict(r) for r in cur.fetchall()]

        return {
            "preferencias": prefs,
            "total_matches": total_matches,
            "cnaes": cnaes,
            "historico": historico,
            "obras_semana": obras_semana,
        }
    finally:
        conn.close()

@app.put("/api/prestador/alertas/preferencias")
async def alertas_put_prefs(body: dict, u=Depends(requer_auth)):
    """Atualiza preferencias de alerta."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO alertas_preferencias (prestador_id, ativo, frequencia, score_minimo, atualizado_em)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (prestador_id) DO UPDATE SET
                    ativo = EXCLUDED.ativo,
                    frequencia = EXCLUDED.frequencia,
                    score_minimo = EXCLUDED.score_minimo,
                    atualizado_em = now()
            """, (
                u["sub"],
                body.get("ativo", True),
                body.get("frequencia", "semanal"),
                max(50, min(90, int(body.get("score_minimo", 50))))
            ))
            conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ─── Sistema de Contribuição ─────────────────────────────────────────────────

def _calcular_creditos_contribuicao(prestador_id: str, conn) -> dict:
    """Calcula créditos, badge e acesso antecipado baseado em contribuições."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT 
                COUNT(*) AS total,
                COUNT(DISTINCT obra_id) AS obras_distintas,
                COUNT(*) FILTER (WHERE status_obra_reportado IS NOT NULL) AS com_fase,
                COUNT(*) FILTER (
                    WHERE enviado_em > date_trunc('month', now())
                ) AS mes_atual
            FROM contatos_log
            WHERE prestador_id = %s AND confirmado_pelo_decisor = true
        """, (prestador_id,))
        stats = dict(cur.fetchone() or {})

        cur.execute("""
            SELECT COALESCE(creditos_ganhos,0) AS ganhos,
                   COALESCE(creditos_consumidos,0) AS consumidos
            FROM prestadores WHERE id=%s
        """, (prestador_id,))
        wallet = dict(cur.fetchone() or {"ganhos": 0, "consumidos": 0})

    badge = stats.get("total", 0) >= 5
    acesso_antecipado = stats.get("total", 0) >= 10
    saldo_centavos = wallet["ganhos"] - wallet["consumidos"]

    return {
        "creditos_ganhos_total": wallet["ganhos"],
        "creditos_consumidos": wallet["consumidos"],
        "creditos_disponiveis_centavos": saldo_centavos,
        "creditos_disponiveis_reais": saldo_centavos / 100,
        "badge_verificador": badge,
        "acesso_antecipado": acesso_antecipado,
        "contribuicoes_total": stats.get("total", 0),
        "contribuicoes_mes": stats.get("mes_atual", 0),
        "obras_distintas": stats.get("obras_distintas", 0),
    }


def _aplicar_recompensas(prestador_id: str, conn):
    """Atualiza prestadores com badges e créditos calculados."""
    stats = _calcular_creditos_contribuicao(prestador_id, conn)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE prestadores SET
                badge_verificador = %s,
                badge_verificador_desde = CASE
                    WHEN %s AND NOT COALESCE(badge_verificador, false)
                    THEN now() ELSE badge_verificador_desde END,
                acesso_antecipado = %s,
                contribuicoes_total = %s
            WHERE id = %s
        """, (
            stats["badge_verificador"],
            stats["badge_verificador"],
            stats["acesso_antecipado"],
            stats["contribuicoes_total"],
            prestador_id,
        ))
        conn.commit()
    return stats


@app.get("/api/contatos/meus")
async def meus_contatos(u=Depends(requer_auth)):
    """Lista contatos registrados pelo prestador logado."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT cl.id, cl.obra_id, cl.tipo_contato, cl.resposta_tipo,
                       cl.confirmado_pelo_decisor, cl.status_obra_reportado,
                       cl.observacoes, cl.enviado_em,
                       o.nome AS obra_nome, o.empresa AS obra_empresa,
                       o.uf, o.fase, o.setor
                FROM contatos_log cl
                LEFT JOIN obras o ON o.id = cl.obra_id
                WHERE cl.prestador_id = %s
                ORDER BY cl.enviado_em DESC
                LIMIT 50
            """, (u["sub"],))
            items = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE confirmado_pelo_decisor) AS confirmados,
                       COUNT(*) FILTER (WHERE resposta_tipo='fechado') AS fechados
                FROM contatos_log WHERE prestador_id=%s
            """, (u["sub"],))
            stats = dict(cur.fetchone() or {})

        for item in items:
            item["id"] = str(item["id"])
            item["obra_id"] = str(item["obra_id"]) if item["obra_id"] else None

        return {"items": items, "total": stats.get("total", 0),
                "confirmados": stats.get("confirmados", 0),
                "fechados": stats.get("fechados", 0)}
    finally:
        conn.close()


@app.get("/api/contatos/ranking-contribuidores")
async def ranking_contribuidores():
    """Top 10 contribuidores do mês — público."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT p.nome_empresa,
                       p.badge_verificador,
                       p.acesso_antecipado,
                       COUNT(*) AS contribuicoes_mes,
                       COUNT(DISTINCT cl.obra_id) AS obras_distintas
                FROM contatos_log cl
                JOIN prestadores p ON p.id = cl.prestador_id
                WHERE cl.confirmado_pelo_decisor = true
                  AND cl.enviado_em > date_trunc('month', now())
                GROUP BY p.id, p.nome_empresa, p.badge_verificador, p.acesso_antecipado
                ORDER BY contribuicoes_mes DESC
                LIMIT 10
            """)
            rows = [dict(r) for r in cur.fetchall()]
        return {"ranking": rows, "mes": datetime.utcnow().strftime("%Y-%m")}
    finally:
        conn.close()


# ─── CRM + Contribuição Mega ─────────────────────────────────────────────────

def _threshold_ativo() -> int:
    """Threshold adaptativo: 1 confirmação até 50 prestadores ativos, 2+ depois."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM prestadores
                WHERE ultimo_acesso > now() - interval '30 days'
            """)
            ativos = cur.fetchone()[0]
        return 1 if ativos < 50 else 2
    finally:
        conn.close()


async def _notificar_contribuicao_validada(prestador_id: str, obra_nome: str,
                                           creditos_centavos: int, tipo: str):
    """Email + badge in-app quando contribuição é validada."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT email, nome_empresa FROM prestadores WHERE id=%s",
                        (prestador_id,))
            p = cur.fetchone()
    finally:
        conn.close()
    if not p or not p.get("email"):
        return

    valor = f"R${creditos_centavos//100}"
    nome = p.get("nome_empresa") or "Fornecedor"
    html = f"""
    <div style="font-family:Arial;max-width:580px;margin:0 auto;
                background:#0A1628;padding:20px;border-radius:12px;">
      <h1 style="color:#F5A623;font-size:18px;">WiNS Hub</h1>
      <h2 style="color:#fff;font-size:15px;">✅ Contribuição validada!</h2>
      <p style="color:#8DA0B3;font-size:13px;">
        Olá {nome.split()[0]}, sua contribuição sobre
        <strong style="color:#fff;">{obra_nome}</strong>
        foi confirmada por outro fornecedor.
      </p>
      <div style="background:#1A2E42;border-radius:8px;padding:14px;
                  text-align:center;margin:16px 0;">
        <div style="font-size:28px;font-weight:700;color:#27AE60;">+{valor}</div>
        <div style="font-size:12px;color:#8DA0B3;">creditados no seu saldo WiNS</div>
      </div>
      <p style="color:#8DA0B3;font-size:12px;">
        Tipo validado: <strong style="color:#fff;">{tipo}</strong><br>
        Continue contribuindo para acumular mais créditos e desbloquear obras!
      </p>
      <a href="https://winshubcomercial.com.br/perfil"
         style="display:block;text-align:center;background:#F5A623;color:#0D1B2A;
                padding:10px;border-radius:24px;font-weight:700;
                text-decoration:none;font-size:13px;margin-top:16px;">
        Ver meu saldo →
      </a>
      <p style="color:#374151;font-size:10px;text-align:center;margin-top:12px;">
        Créditos válidos exclusivamente na plataforma WiNS Hub.
      </p>
    </div>"""
    enviar_email(p["email"], f"✅ +{valor} creditados — WiNS Hub", html,
                 contexto="Contribuicao validada")


@app.post("/api/contatos/registrar")
async def registrar_contato(body: dict, bt: BackgroundTasks,
                             u=Depends(requer_auth)):
    """Registra contato + créditos adaptativos + notificação."""
    obra_id = (body.get("obra_id") or "").strip()
    if not obra_id:
        raise HTTPException(400, "obra_id obrigatório")

    tipo = body.get("tipo_contato", "ligacao")
    resposta_tipo = body.get("resposta_tipo", "tentativa")
    confirmado = bool(body.get("confirmado_pelo_decisor", False))
    status_obra = (body.get("status_obra_reportado") or "").strip() or None
    observacoes = (body.get("observacoes") or "").strip()[:1000] or None
    notas = (body.get("notas") or "").strip()[:2000] or None
    followup_em = body.get("proximo_followup_em") or None

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, nome, cnpj FROM obras WHERE id=%s", (obra_id,))
            obra = cur.fetchone()
            if not obra:
                raise HTTPException(404, "Obra não encontrada")
            cnpj_destino = (obra.get("cnpj") or "").strip() or "00000000000000"

            cur.execute("""
                INSERT INTO contatos_log
                    (obra_id, prestador_id, tipo_contato, canal, resposta_tipo,
                     confirmado_pelo_decisor, status_obra_reportado,
                     observacoes, notas, proximo_followup_em,
                     enviado_por, enviado_em, cnpj_destino,
                     confirmado_em)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,
                    CASE WHEN %s THEN now() ELSE NULL END)
                RETURNING id
            """, (obra_id, u["sub"], tipo, tipo, resposta_tipo,
                  confirmado, status_obra, observacoes, notas, followup_em,
                  u.get("email", u["sub"]), cnpj_destino,
                  confirmado))
            contato_id = cur.fetchone()["id"]
        conn.commit()

        threshold = _threshold_ativo()
        creditos_ganhos = 0
        badge_verificador = False
        acesso_antecipado = False
        notificacoes = []

        if confirmado:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:

                cur.execute("""
                    SELECT COUNT(DISTINCT prestador_id) AS n
                    FROM contatos_log
                    WHERE obra_id=%s AND confirmado_pelo_decisor=true
                """, (obra_id,))
                n_decisor = (cur.fetchone() or {}).get("n", 0)

                if n_decisor == threshold:
                    cur.execute("""
                        SELECT DISTINCT prestador_id FROM contatos_log
                        WHERE obra_id=%s AND confirmado_pelo_decisor=true
                    """, (obra_id,))
                    confirmadores = [r["prestador_id"] for r in cur.fetchall()]
                    for pid in confirmadores:
                        cur.execute("""
                            UPDATE prestadores
                            SET creditos_ganhos = COALESCE(creditos_ganhos,0) + 1000,
                                creditos_expiram_em = COALESCE(
                                    creditos_expiram_em,
                                    now() + interval '12 months')
                            WHERE id=%s
                        """, (pid,))
                        notificacoes.append((str(pid), obra["nome"], 1000, "decisor confirmado"))
                    if u["sub"] in [str(p) for p in confirmadores]:
                        creditos_ganhos += 1000

                if status_obra:
                    cur.execute("""
                        SELECT COUNT(DISTINCT prestador_id) AS n
                        FROM contatos_log
                        WHERE obra_id=%s
                          AND confirmado_pelo_decisor=true
                          AND status_obra_reportado=%s
                    """, (obra_id, status_obra))
                    n_fase = (cur.fetchone() or {}).get("n", 0)

                    if n_fase == threshold:
                        cur.execute("""
                            UPDATE obras SET
                                fase=%s,
                                observacoes_validacao = COALESCE(
                                    observacoes_validacao||E'\n','') ||
                                    'Auto-atualizado '||%s||'+ contribuidores: '||%s||
                                    ' em '||to_char(now(),'DD/MM/YYYY'),
                                validacao_data=CURRENT_DATE,
                                validacao_metodo='contribuicao_fornecedores'
                            WHERE id=%s
                        """, (status_obra, str(threshold), status_obra, obra_id))

                        cur.execute("""
                            SELECT DISTINCT prestador_id FROM contatos_log
                            WHERE obra_id=%s
                              AND confirmado_pelo_decisor=true
                              AND status_obra_reportado=%s
                        """, (obra_id, status_obra))
                        contribuidores = [r["prestador_id"] for r in cur.fetchall()]
                        for pid in contribuidores:
                            cur.execute("""
                                UPDATE prestadores
                                SET creditos_ganhos = COALESCE(creditos_ganhos,0) + 1000,
                                    creditos_expiram_em = COALESCE(
                                        creditos_expiram_em,
                                        now() + interval '12 months')
                                WHERE id=%s
                            """, (pid,))
                            notificacoes.append((str(pid), obra["nome"], 1000,
                                                 f"fase {status_obra} validada"))
                        if u["sub"] in [str(p) for p in contribuidores]:
                            creditos_ganhos += 1000

            conn.commit()

            recompensas = _aplicar_recompensas(u["sub"], conn)
            badge_verificador = recompensas.get("badge_verificador", False)
            acesso_antecipado = recompensas.get("acesso_antecipado", False)

        for pid, onome, centavos, tipo_val in notificacoes:
            bt.add_task(_notificar_contribuicao_validada, pid, onome,
                        centavos, tipo_val)

        aguardando = confirmado and creditos_ganhos == 0
        if creditos_ganhos > 0:
            valor = f"R${creditos_ganhos//100}"
            if badge_verificador:
                msg = f"Validado! +{valor} em créditos 🏆 Badge Verificador!"
            elif acesso_antecipado:
                msg = f"Validado! +{valor} em créditos ⚡ Acesso antecipado 48h!"
            else:
                msg = f"Validado! +{valor} em créditos acumulados."
        elif aguardando:
            msg = ("Contato registrado! Aguardando confirmação de outro "
                   "fornecedor para validar e liberar créditos.")
        else:
            msg = "Contato registrado!"

        return {
            "ok": True,
            "contato_id": str(contato_id),
            "creditos_ganhos_centavos": creditos_ganhos,
            "creditos_ganhos_reais": creditos_ganhos / 100,
            "badge_verificador": badge_verificador,
            "acesso_antecipado": acesso_antecipado,
            "aguardando_validacao": aguardando,
            "threshold_atual": threshold,
            "mensagem": msg,
        }
    finally:
        conn.close()


@app.patch("/api/contatos/{contato_id}/followup")
async def atualizar_followup(contato_id: str, body: dict,
                              u=Depends(requer_auth)):
    """Atualiza data de follow-up e notas de um contato."""
    followup_em = body.get("proximo_followup_em")
    notas = (body.get("notas") or "").strip()[:2000] or None
    resposta_tipo = body.get("resposta_tipo")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE contatos_log SET
                    proximo_followup_em = %s,
                    notas = COALESCE(%s, notas),
                    resposta_tipo = COALESCE(%s, resposta_tipo),
                    followup_count = followup_count + 1
                WHERE id=%s AND prestador_id=%s
                RETURNING id
            """, (followup_em, notas, resposta_tipo, contato_id, u["sub"]))
            if not cur.fetchone():
                raise HTTPException(404, "Contato não encontrado")
            conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/perfil/pipeline")
async def pipeline_crm(u=Depends(requer_auth)):
    """Pipeline CRM: obras por estágio + valor total em prospecção."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT cl.id, cl.obra_id, cl.resposta_tipo,
                       cl.proximo_followup_em, cl.notas, cl.enviado_em,
                       cl.confirmado_pelo_decisor, cl.followup_count,
                       o.nome AS obra_nome, o.empresa, o.uf, o.fase,
                       o.setor, o.valor_estimado
                FROM contatos_log cl
                JOIN obras o ON o.id = cl.obra_id
                WHERE cl.prestador_id=%s
                  AND cl.resposta_tipo != 'fechado'
                ORDER BY cl.proximo_followup_em ASC NULLS LAST,
                         cl.enviado_em DESC
            """, (u["sub"],))
            contatos = [dict(r) for r in cur.fetchall()]

            seen = {}
            for c in contatos:
                oid = str(c["obra_id"])
                if oid not in seen:
                    seen[oid] = c

            pipeline = list(seen.values())

            total_capex = sum(
                float(c["valor_estimado"] or 0) for c in pipeline
            )
            por_estagio = {}
            for c in pipeline:
                e = c["resposta_tipo"] or "tentativa"
                por_estagio[e] = por_estagio.get(e, 0) + 1

            vencidos = [
                c for c in pipeline
                if c.get("proximo_followup_em") and
                str(c["proximo_followup_em"]) < datetime.utcnow().isoformat()
            ]

        for c in pipeline:
            c["id"] = str(c["id"])
            c["obra_id"] = str(c["obra_id"]) if c["obra_id"] else None

        return {
            "pipeline": pipeline,
            "total_obras": len(pipeline),
            "total_capex_estimado": total_capex,
            "total_capex_formatado": _fmt_brl_curto(total_capex),
            "por_estagio": por_estagio,
            "followups_vencidos": len(vencidos),
        }
    finally:
        conn.close()




@app.get("/api/planos")
async def listar_planos():
    """Lista planos com 4 períodos (mensal/trimestral/semestral/anual)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT plano, periodo, preco_centavos, saldo_centavos, desconto_pct
                FROM planos_pricing WHERE ativo = true
                ORDER BY 
                    CASE plano
                        WHEN 'ESSENCIAL' THEN 1
                        WHEN 'PROFISSIONAL' THEN 2
                        WHEN 'ENTERPRISE' THEN 3
                    END,
                    CASE periodo
                        WHEN 'mensal' THEN 1
                        WHEN 'trimestral' THEN 2
                        WHEN 'semestral' THEN 3
                        WHEN 'anual' THEN 4
                    END
            """)
            rows = cur.fetchall()

        planos = {}
        for r in rows:
            p = r["plano"]
            if p not in planos:
                planos[p] = {"plano": p, "periodos": {}}
            planos[p]["periodos"][r["periodo"]] = {
                "preco_centavos": r["preco_centavos"],
                "preco_reais": r["preco_centavos"] / 100,
                "saldo_centavos": r["saldo_centavos"],
                "saldo_reais": r["saldo_centavos"] / 100,
                "desconto_pct": r["desconto_pct"],
            }

        gratuito = {
            "plano": "GRATUITO",
            "periodos": {
                "mensal": {"preco_centavos": 0, "preco_reais": 0,
                           "saldo_centavos": 0, "saldo_reais": 0, "desconto_pct": 0}
            }
        }

        return {
            "planos": [gratuito] + list(planos.values()),
            "features": {
                "GRATUITO": [
                    "Visualizar obras compatíveis (Ouro, Prata, Pipeline)",
                    "Receber alertas semanais por CNAE",
                    "Newsletter semanal com top 3 obras",
                    "Acesso ao Ranking Megaobras",
                    "Sem acesso aos decisores"
                ],
                "ESSENCIAL": [
                    "Mensalidade vira saldo (R$1 = R$1)",
                    "CRM integrado no perfil",
                    "Pipeline com capex em prospecção",
                    "Alertas em tempo real",
                    "Exportação CSV de decisores",
                    "Saldo parcial + complemento no MP"
                ],
                "PROFISSIONAL": [
                    "Tudo do Essencial",
                    "Saldo 2.3x maior pra desbloqueios",
                    "Score de janela de entrada",
                    "Acesso antecipado 48h em obras novas",
                    "Suporte prioritário"
                ],
                "ENTERPRISE": [
                    "Tudo do Profissional",
                    "Bônus 25% no saldo (R$2500 com R$1997)",
                    "Multi-usuário (até 5 contas)",
                    "API de integração",
                    "Relatórios customizados",
                    "Gerente de conta dedicado"
                ],
            }
        }
    finally:
        conn.close()



# ─── Painel de Vendas ──────────────────────────────────────────────────────

def _requer_representante(u=Depends(requer_auth)):
    """Acesso restrito ao painel de vendas (representantes apenas)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT email, COALESCE(plano,'GRATUITO') AS plano,
                       COALESCE(is_representante,false) AS is_representante,
                       comissao_pct_inicial, comissao_pct_recorrente,
                       comissao_meses_recorrencia, nome_empresa, codigo_convite
                FROM prestadores WHERE id=%s
            """, (u["sub"],))
            row = cur.fetchone()
            enriched = {**u, **(dict(row) if row else {})}
            if not pode_acessar_painel_vendas(enriched):
                raise HTTPException(403, "Acesso restrito a representantes")
            return enriched
    finally:
        conn.close()


@app.get("/api/vendas/kpis")
async def vendas_kpis(rep=Depends(_requer_representante)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status='PDF_GERADO') AS pdfs_gerados,
                    COUNT(*) FILTER (WHERE contato_confirmado_em IS NOT NULL) AS emails_enviados,
                    COUNT(*) FILTER (WHERE pdf_acessado_em IS NOT NULL) AS acessos,
                    COUNT(*) FILTER (WHERE cadastrou_em IS NOT NULL) AS cadastros,
                    COUNT(*) FILTER (WHERE assinou_em IS NOT NULL) AS assinaturas,
                    COALESCE(SUM(valor_pago_centavos) FILTER (WHERE assinou_em IS NOT NULL), 0) AS receita_total_centavos
                FROM leads_outbound WHERE representante_id=%s
            """, (rep["sub"],))
            stats = dict(cur.fetchone() or {})

            cur.execute("""
                SELECT
                    COALESCE(SUM(valor_comissao_centavos), 0) AS total_comissao,
                    COALESCE(SUM(valor_comissao_centavos) FILTER (WHERE pago), 0) AS comissao_paga,
                    COALESCE(SUM(valor_comissao_centavos) FILTER (WHERE NOT pago), 0) AS comissao_pendente
                FROM comissoes WHERE representante_id=%s
            """, (rep["sub"],))
            comissoes = dict(cur.fetchone() or {})

            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE pdf_gerado_em > now() - interval '30 days') AS pdfs_30d,
                    COUNT(*) FILTER (WHERE assinou_em > now() - interval '30 days') AS assinaturas_30d
                FROM leads_outbound WHERE representante_id=%s
            """, (rep["sub"],))
            mes = dict(cur.fetchone() or {})

        return {
            "representante": rep["nome_empresa"],
            "comissao_inicial_pct": rep.get("comissao_pct_inicial", 50),
            "comissao_recorrente_pct": rep.get("comissao_pct_recorrente", 25),
            "codigo": rep.get("codigo_convite", ""),
            "funil": {
                "pdfs_gerados": stats.get("pdfs_gerados", 0),
                "emails_enviados": stats.get("emails_enviados", 0),
                "acessos": stats.get("acessos", 0),
                "cadastros": stats.get("cadastros", 0),
                "assinaturas": stats.get("assinaturas", 0),
            },
            "receita_total_reais": stats.get("receita_total_centavos", 0) / 100,
            "comissao_total_reais": comissoes.get("total_comissao", 0) / 100,
            "comissao_paga_reais": comissoes.get("comissao_paga", 0) / 100,
            "comissao_pendente_reais": comissoes.get("comissao_pendente", 0) / 100,
            "ultimo_mes": mes,
        }
    finally:
        conn.close()


@app.get("/api/vendas/buscar-empresa")
async def buscar_empresa(q: str = "", limite: int = 20,
                          rep=Depends(_requer_representante)):
    """Busca empresas na base RFB (tabela `fornecedores` - ~4M ativas) pra prospectar."""
    q = q.strip()
    if len(q) < 3:
        return {"empresas": []}

    limite = min(max(limite, 1), 50)
    qdigits = "".join(c for c in q if c.isdigit())

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if len(qdigits) >= 8:
                cur.execute("""
                    SELECT cnpj, razao_social, nome_fantasia, cnae_principal,
                           cnae_descricao, uf, municipio_nome, porte
                    FROM fornecedores
                    WHERE cnpj LIKE %s
                      AND situacao_cadastral = '02'
                    ORDER BY razao_social
                    LIMIT %s
                """, (f'{qdigits}%', limite))
            else:
                cur.execute("""
                    SELECT cnpj, razao_social, nome_fantasia, cnae_principal,
                           cnae_descricao, uf, municipio_nome, porte
                    FROM fornecedores
                    WHERE razao_social ILIKE %s
                      AND situacao_cadastral = '02'
                    ORDER BY razao_social
                    LIMIT %s
                """, (f'%{q}%', limite))
            rows = [dict(r) for r in cur.fetchall()]

            cnpjs = [r["cnpj"] for r in rows]
            cadastrados = set()
            if cnpjs:
                cur.execute(
                    "SELECT cnpj FROM prestadores WHERE cnpj = ANY(%s)",
                    (cnpjs,)
                )
                cadastrados = {r["cnpj"] for r in cur.fetchall()}

        for r in rows:
            r["ja_cadastrado"] = r["cnpj"] in cadastrados

        return {"empresas": rows, "total": len(rows)}
    finally:
        conn.close()


@app.post("/api/vendas/gerar-pdf-match")
async def gerar_pdf_match(body: dict, rep=Depends(obter_usuario_completo)):
    if not (pode_ver_conteudo_pago(rep) or eh_admin(rep)):
        raise HTTPException(403, "PDF disponível para clientes Standard/Premium ou representantes")
    import secrets
    cnpj = (body.get("cnpj") or "").replace(".","").replace("-","").replace("/","")
    nome = (body.get("nome") or "").strip()
    cnae = (body.get("cnae") or "").strip()
    uf = (body.get("uf") or "").strip().upper()

    if not nome or not cnpj:
        raise HTTPException(400, "nome e cnpj obrigatórios")

    token = secrets.token_urlsafe(20)

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            setor_map = {
                "4321": "INFRAESTRUTURA","4399": "INFRAESTRUTURA",
                "4313": "INFRAESTRUTURA","4329": "INFRAESTRUTURA",
                "2511": "INDUSTRIAL","2521": "INDUSTRIAL",
                "1011": "FRIGORIFICO","1071": "AGROINDUSTRIAL",
                "1922": "PETROLEO_GAS","0710": "MINERACAO","3511": "ENERGIA",
            }
            setor_alvo = setor_map.get((cnae or "")[:4], "INFRAESTRUTURA")

            cur.execute("""
                SELECT id, nome, empresa, uf, fase, valor_estimado, setor
                FROM obras
                WHERE setor = %s AND fonte_tipo = 'OFICIAL'
                  AND empresa IS NOT NULL AND valor_estimado > 100000000
                ORDER BY valor_estimado DESC LIMIT 5
            """, (setor_alvo,))
            obras = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                INSERT INTO leads_outbound (
                    representante_id, empresa_nome, cnpj, cnae, setor, uf,
                    pdf_token, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PDF_GERADO')
                RETURNING id
            """, (rep["sub"], nome, cnpj, cnae, setor_alvo, uf, token))
            lead_id = cur.fetchone()["id"]
        conn.commit()

        return {
            "lead_id": str(lead_id),
            "pdf_token": token,
            "pdf_url": f"/api/vendas/pdf/{token}",
            "obras_encontradas": len(obras),
            "obras": [
                {
                    "nome": o["nome"][:80],
                    "empresa": o["empresa"][:60] if o["empresa"] else "",
                    "uf": o["uf"], "fase": o["fase"],
                    "valor_reais": float(o["valor_estimado"] or 0),
                }
                for o in obras
            ],
        }
    finally:
        conn.close()


@app.post("/api/vendas/leads/{lead_id}/confirmar-envio")
async def confirmar_envio(lead_id: str, body: dict,
                           rep=Depends(_requer_representante)):
    contato_email = (body.get("contato_email") or "").strip().lower()
    contato_nome = (body.get("contato_nome") or "").strip()
    contato_linkedin = (body.get("contato_linkedin") or "").strip()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE leads_outbound SET
                    contato_email = %s, contato_nome = %s,
                    contato_linkedin = %s, contato_confirmado_em = now(),
                    status = 'EMAIL_ENVIADO'
                WHERE id = %s AND representante_id = %s
                RETURNING id
            """, (contato_email, contato_nome, contato_linkedin,
                  lead_id, rep["sub"]))
            if not cur.fetchone():
                raise HTTPException(404, "Lead não encontrado")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/vendas/leads")
async def listar_leads(rep=Depends(_requer_representante)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text, empresa_nome, cnpj, setor, uf, status,
                       contato_email, contato_nome,
                       pdf_gerado_em, contato_confirmado_em,
                       pdf_acessado_em, cadastrou_em, assinou_em,
                       plano_assinado, valor_pago_centavos
                FROM leads_outbound
                WHERE representante_id = %s
                ORDER BY pdf_gerado_em DESC LIMIT 100
            """, (rep["sub"],))
            leads = [dict(r) for r in cur.fetchall()]
        for l in leads:
            l["valor_pago_reais"] = (l.get("valor_pago_centavos") or 0) / 100
        return {"leads": leads, "total": len(leads)}
    finally:
        conn.close()


@app.get("/api/vendas/pdf/{token}")
async def baixar_pdf_match(token: str):
    from fastapi.responses import StreamingResponse
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import cm

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT lo.*, p.nome_empresa AS rep_nome, lo.representante_id::text AS rep_id,
                       p.codigo_convite AS rep_codigo
                FROM leads_outbound lo
                JOIN prestadores p ON p.id = lo.representante_id
                WHERE lo.pdf_token = %s
            """, (token,))
            lead = cur.fetchone()
            if not lead:
                raise HTTPException(404, "PDF não encontrado")
            lead = dict(lead)

            cur.execute("""
                UPDATE leads_outbound SET
                    pdf_acessado_em = COALESCE(pdf_acessado_em, now()),
                    pdf_acessos = pdf_acessos + 1,
                    status = CASE
                        WHEN status IN ('PDF_GERADO','EMAIL_ENVIADO') THEN 'ACESSOU'
                        ELSE status
                    END
                WHERE pdf_token = %s
            """, (token,))

            cur.execute("""
                SELECT nome, empresa, uf, fase, valor_estimado
                FROM obras
                WHERE setor = %s AND fonte_tipo = 'OFICIAL'
                  AND empresa IS NOT NULL AND valor_estimado > 100000000
                ORDER BY valor_estimado DESC LIMIT 5
            """, (lead["setor"],))
            obras = [dict(r) for r in cur.fetchall()]
        conn.commit()

        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                topMargin=2*cm, bottomMargin=2*cm,
                                leftMargin=2*cm, rightMargin=2*cm)
        styles = getSampleStyleSheet()
        story = []

        title = ParagraphStyle('hero', parent=styles['Heading1'],
                               fontSize=22, textColor=colors.HexColor('#F5A623'),
                               alignment=1, spaceAfter=12)
        body = ParagraphStyle('body', parent=styles['Normal'],
                              fontSize=11, leading=16, alignment=1, spaceAfter=8)
        story.append(Paragraph("WiNS Hub Comercial", title))
        story.append(Paragraph(f"<b>{lead['empresa_nome']}</b>", body))
        story.append(Paragraph(
            f"Identificamos <b>{len(obras)} megaobras</b> que precisam dos serviços que sua empresa oferece.",
            body))

        capex_total = sum(float(o.get("valor_estimado") or 0) for o in obras)
        story.append(Paragraph(
            f"<b>Capex total compatível: R$ {capex_total/1e9:.1f} bi</b>",
            ParagraphStyle('capex', parent=body, fontSize=14,
                          textColor=colors.HexColor('#27AE60'))))
        story.append(Spacer(1, 1*cm))

        cell_style = ParagraphStyle('cell', parent=styles['Normal'], fontSize=9, leading=11, wordWrap='CJK')
        cell_header = ParagraphStyle('cellh', parent=styles['Normal'], fontSize=9, leading=11, wordWrap='CJK', textColor=colors.HexColor('#0D1B2A'), fontName='Helvetica-Bold')

        def _esc(t):
            return (str(t) if t is not None else "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        data = [[
            Paragraph("Obra", cell_header),
            Paragraph("Empresa", cell_header),
            Paragraph("UF", cell_header),
            Paragraph("Fase", cell_header),
            Paragraph("Capex", cell_header),
        ]]
        for o in obras:
            data.append([
                Paragraph(_esc(o["nome"] or "—"), cell_style),
                Paragraph(_esc(o["empresa"] or "—"), cell_style),
                Paragraph(_esc(o["uf"] or "—"), cell_style),
                Paragraph(_esc((o["fase"] or "").replace("_", " ").title() or "—"), cell_style),
                Paragraph(f"R$ {float(o.get('valor_estimado') or 0)/1e6:.0f} mi", cell_style),
            ])

        tbl = Table(data, colWidths=[5.5*cm, 4.5*cm, 1*cm, 2.8*cm, 2.2*cm])
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#F5A623')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#0D1B2A')),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#1E3A52')),
            ('ROWBACKGROUNDS', (0,1), (-1,-1),
             [colors.HexColor('#F8F9FA'), colors.white]),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN', (2,0), (4,-1), 'CENTER'),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            ('RIGHTPADDING', (0,0), (-1,-1), 6),
            ('TOPPADDING', (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 1*cm))

        cta_style = ParagraphStyle('cta', parent=body, fontSize=12,
                                    alignment=1, leading=18,
                                    textColor=colors.HexColor('#0D1B2A'))
        story.append(Paragraph("<b>Com o WiNS Hub você acessa:</b>", cta_style))
        for item in [
            "Contatos diretos dos decisores (nome, cargo, email, LinkedIn)",
            "Atualizações em tempo real do status de cada obra",
            "CRM integrado pra acompanhar suas negociações",
            "Score de timing comercial (janela de entrada)",
        ]:
            story.append(Paragraph(f"&#10003; {item}",
                ParagraphStyle('item', parent=body, fontSize=10,
                              alignment=0, leftIndent=0, spaceAfter=4)))
        story.append(Spacer(1, 0.6*cm))

        story.append(Paragraph(
            f"<b>Cadastre-se grátis e veja todas as {len(obras)} obras compatíveis:</b>",
            ParagraphStyle('cta2', parent=cta_style, fontSize=11,
                          textColor=colors.HexColor('#F5A623'))))
        story.append(Paragraph(
            f'<a href="https://winshubcomercial.com.br/login?ref={lead["rep_codigo"]}&lead={token}">winshubcomercial.com.br/login</a>',
            ParagraphStyle('link', parent=body, fontSize=12,
                          textColor=colors.HexColor('#185FA5'))))
        story.append(Spacer(1, 0.4*cm))
        story.append(Paragraph(
            f'<i>Indicado por {lead["rep_nome"]} - Consultor WiNS Hub</i>',
            ParagraphStyle('rep', parent=body, fontSize=9,
                          textColor=colors.HexColor('#8DA0B3'))))

        doc.build(story)
        buf.seek(0)

        return StreamingResponse(
            buf, media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="match-{lead["empresa_nome"][:30]}.pdf"'
            }
        )
    finally:
        conn.close()



@app.post("/api/auth/trocar-senha")
async def trocar_senha(body: dict, u=Depends(requer_auth)):
    """Permite trocar senha (obrigatório se senha_temporaria=true)."""
    nova = (body.get("nova_senha") or "").strip()
    if len(nova) < 8:
        raise HTTPException(400, "Senha precisa ter ao menos 8 caracteres")

    nova_hash = bcrypt.hashpw(nova.encode(), bcrypt.gensalt()).decode()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE prestadores SET
                    senha_hash = %s,
                    senha_temporaria = false,
                    senha_alterada_em = now()
                WHERE id = %s
            """, (nova_hash, u["sub"]))
            conn.commit()
        return {"ok": True}
    finally:
        conn.close()



# ─── Primeiro Acesso (representantes) ──────────────────────────────────────

@app.post("/api/admin/criar-representante")
async def admin_criar_representante(body: dict, u=Depends(requer_auth)):
    """ADMIN: cria representante + envia email com link de 1º acesso."""
    import secrets as _secrets

    conn0 = get_conn()
    try:
        with conn0.cursor() as cur0:
            cur0.execute("SELECT email FROM prestadores WHERE id=%s", (u["sub"],))
            r0 = cur0.fetchone()
            if not r0 or r0[0] != "williamvnvn@gmail.com":
                raise HTTPException(403, "Apenas admin")
    finally:
        conn0.close()

    email = (body.get("email") or "").strip().lower()
    nome = (body.get("nome") or "").strip()
    comissao_inicial = int(body.get("comissao_pct_inicial", 50))
    comissao_recorrente = int(body.get("comissao_pct_recorrente", 25))

    if not email or not nome:
        raise HTTPException(400, "email e nome obrigatórios")

    senha_placeholder = _secrets.token_urlsafe(32)
    senha_hash = bcrypt.hashpw(senha_placeholder.encode(), bcrypt.gensalt()).decode()

    codigo = nome.split()[0][:4].upper() + _secrets.token_urlsafe(4).upper()[:4]

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO prestadores (
                    email, senha_hash, nome_empresa, plano,
                    is_representante, comissao_pct_inicial, comissao_pct_recorrente,
                    comissao_meses_recorrencia, codigo_convite, senha_temporaria,
                    onboarding_completo, criado_em
                ) VALUES (
                    %s, %s, %s, 'PREMIUM',
                    true, %s, %s,
                    12, %s, true,
                    true, now()
                )
                ON CONFLICT (email) DO UPDATE SET
                    is_representante = true,
                    comissao_pct_inicial = EXCLUDED.comissao_pct_inicial,
                    comissao_pct_recorrente = EXCLUDED.comissao_pct_recorrente,
                    senha_temporaria = true
                RETURNING id, codigo_convite
            """, (email, senha_hash, nome, comissao_inicial, comissao_recorrente, codigo))
            row = cur.fetchone()
            prestador_id = row["id"]
            codigo_final = row["codigo_convite"]

            token_pa = _secrets.token_urlsafe(32)
            cur.execute("""
                INSERT INTO primeiro_acesso_tokens (prestador_id, token, expira_em)
                VALUES (%s, %s, now() + interval '24 hours')
            """, (prestador_id, token_pa))
        conn.commit()

        link = f"https://winshubcomercial.com.br/primeiro-acesso?t={token_pa}"
        email_status = "skipped"
        try:
            api_key = os.getenv("RESEND_API_KEY", "")
            if api_key:
                html = f"""<!DOCTYPE html><html><body style="background:#0D1B2A;color:#fff;font-family:Arial,sans-serif;padding:40px;margin:0;">
<div style="max-width:560px;margin:0 auto;background:#1A2E42;border-radius:16px;padding:40px;border:1px solid #1E3A52;">
  <h1 style="color:#F5A623;font-size:24px;margin:0 0 16px;">Bem-vindo(a) ao WiNS Hub Comercial!</h1>
  <p style="color:#fff;font-size:14px;line-height:1.6;margin:0 0 16px;">Olá <strong>{nome}</strong>,</p>
  <p style="color:#fff;font-size:14px;line-height:1.6;margin:0 0 24px;">Sua conta de representante foi criada. Comissão: <strong style="color:#27AE60;">{comissao_inicial}% no 1º pagamento</strong> e <strong style="color:#27AE60;">{comissao_recorrente}% recorrente por 12 meses</strong>.</p>
  <p style="color:#fff;font-size:14px;line-height:1.6;margin:0 0 24px;">Clique no link abaixo para definir sua senha pessoal:</p>
  <div style="text-align:center;margin:32px 0;">
    <a href="{link}" style="background:linear-gradient(135deg, #F5A623 0%, #E89512 100%);color:#0D1B2A;text-decoration:none;padding:14px 32px;border-radius:24px;font-weight:700;font-size:14px;display:inline-block;">Definir minha senha →</a>
  </div>
  <p style="color:#8DA0B3;font-size:12px;line-height:1.6;margin:24px 0 0;">⏱ Link expira em 24 horas.<br>📧 Código de indicação: <strong style="color:#F5A623;">{codigo_final}</strong></p>
</div></body></html>"""
                with httpx.Client(timeout=15.0) as client:
                    resp = client.post(
                        "https://api.resend.com/emails",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "from": RESEND_FROM,
                            "to": [email],
                            "subject": "Bem-vindo ao WiNS Hub! Defina sua senha",
                            "html": html,
                        }
                    )
                email_status = "enviado" if resp.status_code in (200,201,202) else f"erro {resp.status_code}: {resp.text[:100]}"
            else:
                email_status = "RESEND_API_KEY ausente"
        except Exception as e:
            email_status = f"erro: {str(e)[:100]}"

        return {
            "ok": True,
            "prestador_id": str(prestador_id),
            "codigo_convite": codigo_final,
            "email": email,
            "link_primeiro_acesso": link,
            "email_status": email_status,
        }
    finally:
        conn.close()


@app.post("/api/auth/primeiro-acesso/validar")
async def validar_token_primeiro_acesso(body: dict):
    token_pa = (body.get("token") or "").strip()
    if not token_pa:
        raise HTTPException(400, "Token obrigatório")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT pat.id, pat.expira_em, pat.usado_em,
                       p.id as prestador_id, p.email, p.nome_empresa
                FROM primeiro_acesso_tokens pat
                JOIN prestadores p ON p.id = pat.prestador_id
                WHERE pat.token = %s
            """, (token_pa,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Token inválido")
            if row["usado_em"]:
                raise HTTPException(400, "Token já foi usado")
            from datetime import timezone as _tz
            if row["expira_em"] < datetime.now(_tz.utc):
                raise HTTPException(400, "Token expirado")
            return {"ok": True, "email": row["email"], "nome": row["nome_empresa"]}
    finally:
        conn.close()


@app.post("/api/auth/primeiro-acesso/definir-senha")
async def definir_senha_primeiro_acesso(body: dict):
    from datetime import timezone as _tz
    token_pa = (body.get("token") or "").strip()
    nova_senha = (body.get("nova_senha") or "").strip()
    if len(nova_senha) < 8:
        raise HTTPException(400, "Senha precisa ter ao menos 8 caracteres")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT pat.id, pat.expira_em, pat.usado_em,
                       p.id AS prestador_id, p.email, p.plano,
                       COALESCE(p.is_representante,false) AS is_representante
                FROM primeiro_acesso_tokens pat
                JOIN prestadores p ON p.id = pat.prestador_id
                WHERE pat.token = %s
            """, (token_pa,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Token inválido")
            if row["usado_em"]:
                raise HTTPException(400, "Token já usado")
            if row["expira_em"] < datetime.now(_tz.utc):
                raise HTTPException(400, "Token expirado")

            senha_hash = bcrypt.hashpw(nova_senha.encode(), bcrypt.gensalt()).decode()
            cur.execute("""
                UPDATE prestadores SET
                    senha_hash = %s, senha_temporaria = false, senha_alterada_em = now()
                WHERE id = %s
            """, (senha_hash, row["prestador_id"]))
            cur.execute("UPDATE primeiro_acesso_tokens SET usado_em = now() WHERE id = %s", (row["id"],))
        conn.commit()

        jwt_token = criar_token(str(row["prestador_id"]), row["plano"], row.get("is_representante", False), email=row.get("email"), nome=row.get("nome_empresa"))
        return {"ok": True, "token": jwt_token, "redirect": "/vendas"}
    finally:
        conn.close()



# ─── Painel Admin (Vendas/Reps) ────────────────────────────────────────────

def _requer_admin(u=Depends(requer_auth)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT email FROM prestadores WHERE id=%s", (u["sub"],))
            row = cur.fetchone()
            enriched = {**u, "email": (row["email"] if row else None)}
            if not eh_admin(enriched):
                raise HTTPException(403, "Acesso restrito ao admin")
            return enriched
    finally:
        conn.close()



# ─── Comissões (rep + admin) ───────────────────────────────────────────────

@app.get("/api/vendas/comissoes")
async def vendas_comissoes(
    rep=Depends(_requer_representante),
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    """Lista comissões do representante. Privacidade: oculta valor_base_centavos."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COALESCE(SUM(valor_comissao_centavos) FILTER (WHERE status='disponivel'),0) AS disponivel,
                    COALESCE(SUM(valor_comissao_centavos) FILTER (WHERE status='pendente'),0)   AS pendente,
                    COALESCE(SUM(valor_comissao_centavos) FILTER (WHERE status='paga'),0)        AS pago,
                    COALESCE(SUM(valor_comissao_centavos) FILTER (WHERE status='estornada'),0)   AS estornado,
                    COUNT(*) FILTER (WHERE status='disponivel') AS n_disponivel,
                    COUNT(*) FILTER (WHERE status='pendente') AS n_pendente,
                    COUNT(*) FILTER (WHERE status='paga') AS n_pago,
                    COUNT(*) FILTER (WHERE status='estornada') AS n_estornado
                FROM comissoes WHERE representante_id=%s
            """, (rep["sub"],))
            saldos = dict(cur.fetchone() or {})
            cur.execute("""
                SELECT tipo,
                       COALESCE(SUM(valor_comissao_centavos), 0) AS total,
                       COUNT(*) AS n
                FROM comissoes
                WHERE representante_id=%s
                  AND date_trunc('month', criado_em) = date_trunc('month', NOW())
                  AND status != 'estornada'
                GROUP BY tipo
            """, (rep["sub"],))
            breakdown = {r["tipo"]: {"total_centavos": r["total"], "n": r["n"]} for r in cur.fetchall()}
            params = [rep["sub"]]
            where_extra = ""
            if status in ('disponivel','pendente','paga','estornada'):
                where_extra = " AND c.status=%s"
                params.append(status)
            params.extend([limit, offset])
            cur.execute(f"""
                SELECT c.id::text, c.tipo, c.valor_comissao_centavos, c.pct_aplicado,
                       c.status, c.disponivel_em, c.pago_em, c.pago_via,
                       c.recorrencia_mes, c.criado_em, c.observacoes,
                       lo.empresa_nome
                FROM comissoes c
                LEFT JOIN leads_outbound lo ON lo.id = c.lead_outbound_id
                WHERE c.representante_id=%s
                {where_extra}
                ORDER BY c.criado_em DESC
                LIMIT %s OFFSET %s
            """, params)
            comissoes = []
            for r in cur.fetchall():
                d = dict(r)
                if d.get("disponivel_em"): d["disponivel_em"] = d["disponivel_em"].isoformat()
                if d.get("pago_em"): d["pago_em"] = d["pago_em"].isoformat()
                if d.get("criado_em"): d["criado_em"] = d["criado_em"].isoformat()
                comissoes.append(d)
        return {"saldos_centavos": saldos, "breakdown_mes_corrente": breakdown,
                "comissoes": comissoes, "limit": limit, "offset": offset}
    finally:
        conn.close()


@app.get("/api/admin/comissoes")
async def admin_listar_comissoes(
    u=Depends(_requer_admin),
    status: Optional[str] = "disponivel",
    rep_id: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            params = []
            wheres = []
            if status and status != 'todos':
                wheres.append("c.status=%s")
                params.append(status)
            if rep_id:
                wheres.append("c.representante_id=%s")
                params.append(rep_id)
            where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
            params.extend([limit, offset])
            cur.execute(f"""
                SELECT c.id::text, c.tipo, c.valor_base_centavos,
                       c.pct_aplicado, c.valor_comissao_centavos, c.status,
                       c.disponivel_em, c.pago_em, c.pago_via, c.recorrencia_mes,
                       c.criado_em, c.observacoes,
                       c.representante_id::text AS rep_id, p.email AS rep_email,
                       p.nome_empresa AS rep_nome,
                       lo.empresa_nome AS lead_empresa,
                       pres.email AS prestador_email
                FROM comissoes c
                LEFT JOIN prestadores p ON p.id = c.representante_id
                LEFT JOIN leads_outbound lo ON lo.id = c.lead_outbound_id
                LEFT JOIN prestadores pres ON pres.id = c.prestador_id
                {where_sql}
                ORDER BY c.criado_em DESC
                LIMIT %s OFFSET %s
            """, params)
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                for k in ("disponivel_em","pago_em","criado_em"):
                    if d.get(k): d[k] = d[k].isoformat()
                rows.append(d)
        return {"comissoes": rows, "limit": limit, "offset": offset}
    finally:
        conn.close()


@app.get("/api/admin/comissoes/resumo-mes")
async def admin_resumo_mes(u=Depends(_requer_admin)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT c.representante_id::text AS rep_id, p.email AS rep_email,
                       p.nome_empresa AS rep_nome,
                       COUNT(*) AS comissoes_count,
                       SUM(c.valor_comissao_centavos) AS total_disponivel_centavos
                FROM comissoes c
                JOIN prestadores p ON p.id = c.representante_id
                WHERE c.status='disponivel'
                GROUP BY c.representante_id, p.email, p.nome_empresa
                ORDER BY total_disponivel_centavos DESC
            """)
            por_rep = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT COUNT(*) AS total, SUM(valor_comissao_centavos) AS soma
                FROM comissoes WHERE status='disponivel'
            """)
            tot = dict(cur.fetchone())
        return {"por_rep": por_rep, "total_geral_centavos": tot.get("soma") or 0,
                "total_comissoes": tot.get("total") or 0}
    finally:
        conn.close()


@app.get("/api/admin/comissoes/export.csv")
async def admin_export_csv(u=Depends(_requer_admin)):
    from fastapi.responses import StreamingResponse
    import csv, io
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT p.email AS rep_email, p.nome_empresa AS rep_nome,
                       COUNT(*) AS comissoes_count,
                       SUM(c.valor_comissao_centavos) AS total_disponivel_centavos
                FROM comissoes c
                JOIN prestadores p ON p.id = c.representante_id
                WHERE c.status='disponivel'
                GROUP BY p.email, p.nome_empresa
                ORDER BY total_disponivel_centavos DESC
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rep_email","rep_nome","total_disponivel_centavos","comissoes_count"])
    for r in rows:
        w.writerow([r["rep_email"], r["rep_nome"], r["total_disponivel_centavos"], r["comissoes_count"]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="comissoes_disponiveis.csv"'})


@app.post("/api/admin/comissoes/{cid}/marcar-paga")
async def admin_marcar_paga(cid: str, body: dict = Body(default={}), u=Depends(_requer_admin)):
    pix_id = (body.get("pix_id") or "").strip()
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                UPDATE comissoes
                SET status='paga', pago_em=NOW(), pago_via='PIX_MANUAL',
                    observacoes=COALESCE(observacoes,'') || CASE WHEN %s<>'' THEN E'\n[PIX:'||%s||']' ELSE '' END,
                    pago=true, pago_em=COALESCE(pago_em, NOW())
                WHERE id=%s AND status IN ('disponivel','pendente')
                RETURNING id, status
            """, (pix_id, pix_id, cid))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Comissão não encontrada ou já foi paga/estornada")
        conn.commit()
        return {"ok": True, "id": row["id"], "status": row["status"]}
    finally:
        conn.close()


@app.post("/api/admin/comissoes/{cid}/estornar")
async def admin_estornar(cid: str, body: dict = Body(default={}), u=Depends(_requer_admin)):
    motivo = (body.get("motivo") or "").strip()
    if not motivo:
        raise HTTPException(400, "Motivo do estorno é obrigatório")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                UPDATE comissoes
                SET status='estornada',
                    observacoes=COALESCE(observacoes,'') || E'\n[ESTORNADA: '||%s||']'
                WHERE id=%s AND status IN ('pendente','disponivel')
                RETURNING id, status
            """, (motivo, cid))
            row = cur.fetchone()
            if not row:
                raise HTTPException(400, "Comissão não pode ser estornada (status: paga/já estornada)")
        conn.commit()
        return {"ok": True, "id": row["id"], "status": row["status"]}
    finally:
        conn.close()


@app.post("/api/admin/comissoes/criar-manual")
async def admin_criar_manual(body: dict = Body(...), u=Depends(_requer_admin)):
    rep_id = body.get("rep_id")
    prestador_id = body.get("prestador_id")
    valor_centavos = int(body.get("valor_centavos") or 0)
    tipo = (body.get("tipo") or "").upper()
    motivo = (body.get("motivo") or "").strip()
    if not rep_id or not prestador_id or valor_centavos <= 0 or tipo not in ('INICIAL','RECORRENTE','AVULSO') or not motivo:
        raise HTTPException(400, "rep_id, prestador_id, valor_centavos>0, tipo e motivo obrigatórios")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COALESCE(comissao_pct_inicial,50) AS pct FROM prestadores WHERE id=%s", (rep_id,))
            r = cur.fetchone()
            if not r: raise HTTPException(404, "Rep não encontrado")
            pct = r["pct"]
            valor_comissao = (valor_centavos * pct) // 100
            cur.execute("""
                INSERT INTO comissoes (
                    representante_id, prestador_id, tipo, valor_base_centavos,
                    pct_aplicado, valor_comissao_centavos, status, disponivel_em,
                    referente_a, observacoes, criado_em
                ) VALUES (%s, %s, %s, %s, %s, %s, 'disponivel', NOW(), CURRENT_DATE,
                          'MANUAL: ' || %s, NOW())
                RETURNING id::text
            """, (rep_id, prestador_id, tipo, valor_centavos, pct, valor_comissao, motivo))
            row = cur.fetchone()
        conn.commit()
        return {"ok": True, "id": row["id"], "valor_comissao_centavos": valor_comissao}
    finally:
        conn.close()


@app.get("/api/admin/buscar-prestador")
async def admin_buscar_prestador(q: str = "", u=Depends(_requer_admin)):
    if len(q) < 2:
        return {"resultados": []}
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text, email, nome_empresa, cnpj, plano
                FROM prestadores
                WHERE (email ILIKE %s OR nome_empresa ILIKE %s OR cnpj ILIKE %s)
                  AND COALESCE(ativo, true) = true
                ORDER BY criado_em DESC
                LIMIT 10
            """, (f"%{q}%", f"%{q}%", f"%{q}%"))
            return {"resultados": [dict(r) for r in cur.fetchall()]}
    finally:
        conn.close()


# ─── Admin: Gerar Matches V2 sob demanda (obra ou fornecedor) ──────────────
# Reusa o mesmo SQL do matchmaker_worker (pre-rank 200 + LATERAL engine v2 +
# ON CONFLICT). Idempotente: DELETE matches da entidade + INSERT. Síncrono
# (~900ms por obra) — não dispara worker pesado. Filtros defensivos do
# worker (razao_social NOT NULL, porte != MICRO) já estão na sub-query.

_MATCH_V2_INSERT_OBRA_SQL = """
INSERT INTO matches_v2 (obra_id, cnpj, score, score_breakdown, gerado_em)
SELECT %(obra_id)s::uuid, c.cnpj, m.score, m.breakdown, NOW()
FROM (
  SELECT f.cnpj, MAX(scc.peso) * MAX(up.peso) AS pre_score
  FROM fornecedores f
  JOIN setor_cnae_compatibility scc
    ON scc.setor_obra = %(setor)s
   AND (f.cnae_principal = scc.cnae_codigo OR scc.cnae_codigo = ANY(f.cnae_secundarios))
  JOIN uf_proximidade up ON up.uf_obra = %(uf)s AND up.uf_fornec = f.uf
  WHERE f.porte_inferido != 'MICRO'
    AND f.razao_social IS NOT NULL AND TRIM(f.razao_social) != ''
  GROUP BY f.cnpj
  ORDER BY pre_score DESC
  LIMIT 200
) c
CROSS JOIN LATERAL calcular_score_match_v2(%(obra_id)s::uuid, c.cnpj) m
WHERE m.score >= 50
ON CONFLICT (obra_id, cnpj) DO UPDATE
  SET score = EXCLUDED.score,
      score_breakdown = EXCLUDED.score_breakdown,
      gerado_em = NOW()
"""

_MATCH_V2_INSERT_FORNEC_SQL = """
INSERT INTO matches_v2 (obra_id, cnpj, score, score_breakdown, gerado_em)
SELECT sub.id, %(cnpj)s, m.score, m.breakdown, NOW()
FROM (
  SELECT o.id, MAX(scc.peso) * MAX(up.peso) AS pre_score
  FROM obras o
  JOIN setor_cnae_compatibility scc
    ON scc.setor_obra = o.setor
   AND (%(cnae_principal)s = scc.cnae_codigo OR scc.cnae_codigo = ANY(%(cnae_secundarios)s::text[]))
  JOIN uf_proximidade up ON up.uf_obra = o.uf AND up.uf_fornec = %(uf_fornec)s
  WHERE o.visivel = true
    AND o.classificacao_computed IN ('OURO','PRATA','BRONZE','PIPELINE')
    AND COALESCE(o.fonte_tipo,'OFICIAL') != 'NOTICIA'
    AND o.setor IS NOT NULL AND o.uf IS NOT NULL
  GROUP BY o.id
  ORDER BY pre_score DESC
  LIMIT 200
) sub
CROSS JOIN LATERAL calcular_score_match_v2(sub.id, %(cnpj)s) m
WHERE m.score >= 50
ORDER BY m.score DESC
LIMIT 10
ON CONFLICT (obra_id, cnpj) DO UPDATE
  SET score = EXCLUDED.score,
      score_breakdown = EXCLUDED.score_breakdown,
      gerado_em = NOW()
"""


@app.post("/api/admin/obras/{obra_id}/gerar-matches")
async def admin_gerar_matches_obra(obra_id: str, u=Depends(_requer_admin)):
    """Dispara engine v2 para 1 obra (idempotente: DELETE + INSERT). ~900ms."""
    t0 = time.time()
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text AS id, setor, uf, classificacao_computed, visivel,
                       COALESCE(fonte_tipo,'OFICIAL') AS fonte_tipo
                FROM obras WHERE id = %s::uuid
            """, (obra_id,))
            obra = cur.fetchone()
            if not obra:
                raise HTTPException(404, "Obra não encontrada")
            if not obra["visivel"]:
                raise HTTPException(400, "Obra invisível — não elegível para matchmaking")
            if obra["classificacao_computed"] == "REJEITADO":
                raise HTTPException(400, "Obra REJEITADA — não elegível para matchmaking")
            # NOTICIA visível é permitido (Cohort B: ~89 obras deliberadas com decisor OURO/PRATA).
            # classificacao NULL/outros também aceito — engine inline não filtra como o worker batch.
            if not obra["setor"] or not obra["uf"]:
                raise HTTPException(400, "Obra sem setor ou UF — precondições do engine v2")

            cur.execute("DELETE FROM matches_v2 WHERE obra_id = %s::uuid", (obra_id,))
            deleted = cur.rowcount
            cur.execute(_MATCH_V2_INSERT_OBRA_SQL, {
                "obra_id": obra_id, "setor": obra["setor"], "uf": obra["uf"],
            })
            inserted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {
        "matches_criados": inserted,
        "matches_deletados": deleted,
        "obra_id": obra_id,
        "duracao_ms": int((time.time() - t0) * 1000),
    }


@app.post("/api/admin/fornecedores/{cnpj}/gerar-matches")
async def admin_gerar_matches_fornecedor(cnpj: str, u=Depends(_requer_admin)):
    """Dispara engine v2 inverso (1 fornecedor → top 10 obras compatíveis).
    Idempotente: DELETE matches do CNPJ + INSERT top 10. Skip wallet (admin)."""
    t0 = time.time()
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT cnpj, cnae_principal, COALESCE(cnae_secundarios, '{}'::text[]) AS cnae_secundarios,
                       uf, porte_inferido, razao_social
                FROM fornecedores WHERE cnpj = %s
            """, (cnpj,))
            forn = cur.fetchone()
            if not forn:
                raise HTTPException(404, "Fornecedor não encontrado")
            if not forn["razao_social"] or not forn["razao_social"].strip():
                raise HTTPException(400, "Fornecedor sem razao_social — precondição defensiva")
            if forn["porte_inferido"] == "MICRO":
                raise HTTPException(400, "Fornecedor MICRO — exclui do matchmaking v2")
            if not forn["cnae_principal"] or not forn["uf"]:
                raise HTTPException(400, "Fornecedor sem cnae_principal ou UF")

            cur.execute("DELETE FROM matches_v2 WHERE cnpj = %s", (cnpj,))
            deleted = cur.rowcount
            cur.execute(_MATCH_V2_INSERT_FORNEC_SQL, {
                "cnpj": cnpj,
                "cnae_principal": forn["cnae_principal"],
                "cnae_secundarios": list(forn["cnae_secundarios"] or []),
                "uf_fornec": forn["uf"],
            })
            inserted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {
        "matches_criados": inserted,
        "matches_deletados": deleted,
        "cnpj": cnpj,
        "duracao_ms": int((time.time() - t0) * 1000),
    }


# ─── Admin: PDF Match v2 (obra + fornecedor) ───────────────────────────────
# Branding WiNS Hub (preto/dourado, DejaVuSans UTF-8, A4). Módulo isolado em
# services/pdf_match.py — não depende do PDF v0.7 legado (/api/vendas/gerar-pdf-match).

def _slugify_for_filename(s: str, maxlen: int = 60) -> str:
    if not s:
        return "obra"
    out = []
    for c in s.lower():
        if c.isalnum():
            out.append(c)
        elif c in " -_":
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return (slug or "obra")[:maxlen]


@app.get("/api/admin/obras/{obra_id}/match-pdf")
async def admin_obra_match_pdf(obra_id: str, u=Depends(_requer_admin)):
    """PDF profissional do match de 1 obra. Branding WiNS Hub."""
    from services.pdf_match import build_pdf_obra
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text AS id, nome, empresa, uf, municipio, setor, fase,
                       valor_estimado, valor_formatado, lead_score, classificacao_computed,
                       fonte, url_fonte, validacao_data
                FROM obras WHERE id = %s::uuid
            """, (obra_id,))
            obra = cur.fetchone()
            if not obra:
                raise HTTPException(404, "Obra não encontrada")

            cur.execute("""
                SELECT nome, cargo, email, linkedin_url, telefone, tipo_cargo
                FROM decisores_obra
                WHERE obra_id = %s::uuid AND excluido_em IS NULL
                ORDER BY
                    CASE tipo_cargo
                        WHEN 'C-LEVEL' THEN 1
                        WHEN 'DIRETOR' THEN 2
                        WHEN 'GERENTE' THEN 3
                        ELSE 4 END,
                    nome
                LIMIT 10
            """, (obra_id,))
            decisores = [dict(r) for r in cur.fetchall()]

            # Dedup por raiz CNPJ (primeiros 8 digitos = mesmo grupo empresarial):
            # se 3 filiais da mesma matriz tem matches, mostra so a de maior score.
            cur.execute("""
                WITH ranked AS (
                    SELECT m.cnpj, m.score::int AS score, m.score_breakdown,
                           f.razao_social, f.uf, f.porte_inferido,
                           f.capital_social, f.cnae_principal,
                           ROW_NUMBER() OVER (
                             PARTITION BY LEFT(m.cnpj, 8)
                             ORDER BY m.score DESC, m.cnpj
                           ) AS rn_grp
                    FROM matches_v2 m
                    JOIN fornecedores f ON f.cnpj = m.cnpj
                    WHERE m.obra_id = %s::uuid
                      AND f.razao_social IS NOT NULL AND TRIM(f.razao_social) != ''
                )
                SELECT cnpj, score, score_breakdown, razao_social, uf,
                       porte_inferido, capital_social, cnae_principal
                FROM ranked
                WHERE rn_grp = 1
                ORDER BY score DESC, cnpj
                LIMIT 20
            """, (obra_id,))
            matches = [dict(r) for r in cur.fetchall()]

            # Time ideal: reusa SQL do /api/obras/{oid}/time-ideal
            time_ideal_data = None
            if obra.get("setor"):
                cur.execute("""
                    WITH categorias_setor AS (
                        SELECT DISTINCT cs.id, cs.nome, cs.ordem
                        FROM categorias_servico cs
                        WHERE EXISTS (
                            SELECT 1 FROM setor_cnae_compatibility scc
                            WHERE scc.setor_obra = %s
                              AND scc.cnae_codigo = ANY(cs.cnaes)
                              AND scc.peso >= 0.5
                        )
                    ),
                    match_cat AS (
                        SELECT m.cnpj, m.score::int AS score,
                               (SELECT cs.id FROM categorias_servico cs
                                WHERE (m.score_breakdown->>'cnae_codigo') = ANY(cs.cnaes)
                                ORDER BY cs.ordem ASC, cs.id ASC LIMIT 1) AS categoria_id
                        FROM matches_v2 m
                        WHERE m.obra_id = %s::uuid AND m.score >= 50
                    ),
                    ranked AS (
                        SELECT mc.*,
                               ROW_NUMBER() OVER (PARTITION BY categoria_id ORDER BY score DESC, cnpj) AS rnk
                        FROM match_cat mc WHERE categoria_id IS NOT NULL
                    )
                    SELECT cs.nome AS categoria, r.cnpj, r.score,
                           f.razao_social,
                           CASE WHEN r.cnpj IS NULL THEN true ELSE false END AS gap
                    FROM categorias_setor cs
                    LEFT JOIN ranked r ON r.categoria_id = cs.id AND r.rnk = 1
                    LEFT JOIN fornecedores f ON f.cnpj = r.cnpj AND f.razao_social IS NOT NULL
                    ORDER BY cs.ordem ASC, cs.id ASC
                """, (obra["setor"], obra_id))
                rows = cur.fetchall()
                categorias = []
                for r in rows:
                    cat = {"nome": r["categoria"], "gap": r["gap"]}
                    if not r["gap"] and r["cnpj"]:
                        cat["fornecedor"] = {
                            "razao_social": r["razao_social"],
                            "cnpj": r["cnpj"],
                            "score": r["score"] or 0,
                        }
                    categorias.append(cat)
                cobertas = sum(1 for c in categorias if not c["gap"])
                gaps_count = sum(1 for c in categorias if c["gap"])
                scores = [c["fornecedor"]["score"] for c in categorias if not c["gap"]]
                time_ideal_data = {
                    "categorias": categorias,
                    "score_medio": round(sum(scores) / len(scores), 1) if scores else 0,
                    "cobertura_pct": round(cobertas / len(categorias) * 100) if categorias else 0,
                    "gaps_count": gaps_count,
                }
    finally:
        conn.close()

    pdf_bytes = build_pdf_obra(dict(obra), decisores, matches, time_ideal_data)
    fname = f"match-obra-{_slugify_for_filename(obra.get('nome'))}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{fname}"'},
    )


@app.get("/api/admin/fornecedores/{cnpj}/match-pdf")
async def admin_fornecedor_match_pdf(cnpj: str, u=Depends(_requer_admin)):
    """PDF profissional do match inverso (1 fornecedor → top 10 obras). Branding WiNS Hub."""
    from services.pdf_match import build_pdf_fornecedor
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT cnpj, razao_social, nome_fantasia, situacao, capital_social,
                       porte_inferido, uf,
                       COALESCE(municipio_nome, municipio_rfb) AS municipio,
                       data_abertura AS data_fundacao,
                       cnae_principal, cnae_descricao AS cnae_principal_descricao,
                       cnae_secundarios
                FROM fornecedores WHERE cnpj = %s
            """, (cnpj,))
            forn = cur.fetchone()
            if not forn:
                raise HTTPException(404, "Fornecedor não encontrado")

            cur.execute("""
                SELECT m.obra_id::text AS obra_id, m.score::int AS score, m.score_breakdown,
                       o.nome, o.empresa, o.valor_estimado, o.valor_formatado,
                       o.fase, o.uf, o.setor, o.classificacao_computed,
                       (SELECT d.nome FROM decisores_obra d
                        WHERE d.obra_id = m.obra_id AND d.excluido_em IS NULL
                        ORDER BY CASE d.tipo_cargo WHEN 'C-LEVEL' THEN 1
                                  WHEN 'DIRETOR' THEN 2 WHEN 'GERENTE' THEN 3
                                  ELSE 4 END LIMIT 1) AS decisor_nome,
                       (SELECT d.cargo FROM decisores_obra d
                        WHERE d.obra_id = m.obra_id AND d.excluido_em IS NULL
                        ORDER BY CASE d.tipo_cargo WHEN 'C-LEVEL' THEN 1
                                  WHEN 'DIRETOR' THEN 2 WHEN 'GERENTE' THEN 3
                                  ELSE 4 END LIMIT 1) AS decisor_cargo,
                       (SELECT d.email FROM decisores_obra d
                        WHERE d.obra_id = m.obra_id AND d.excluido_em IS NULL
                          AND d.email IS NOT NULL
                        ORDER BY CASE d.tipo_cargo WHEN 'C-LEVEL' THEN 1
                                  WHEN 'DIRETOR' THEN 2 WHEN 'GERENTE' THEN 3
                                  ELSE 4 END LIMIT 1) AS decisor_email
                FROM matches_v2 m
                JOIN obras o ON o.id = m.obra_id
                WHERE m.cnpj = %s
                  AND (o.visivel IS NULL OR o.visivel = true)
                  AND o.classificacao_computed != 'REJEITADO'
                ORDER BY m.score DESC
                LIMIT 10
            """, (cnpj,))
            obras = [dict(r) for r in cur.fetchall()]
            # decisor_email_verificado: campo derivado (não temos email_status em decisores_obra hoje)
            for o in obras:
                o["decisor_email_verificado"] = bool(o.get("decisor_email"))
    finally:
        conn.close()

    pdf_bytes = build_pdf_fornecedor(dict(forn), obras)
    fname = f"match-fornec-{_slugify_for_filename(forn.get('razao_social'))}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{fname}"'},
    )


@app.get("/api/admin/metricas-gerais")
async def admin_metricas_gerais(u=Depends(_requer_admin)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(DISTINCT id) FILTER (WHERE is_representante = true) AS reps_total,
                    COUNT(DISTINCT id) FILTER (WHERE is_representante = true AND COALESCE(ativo, true) = true) AS reps_ativos
                FROM prestadores
            """)
            reps = dict(cur.fetchone() or {})

            cur.execute("""
                SELECT
                    COUNT(*) AS pdfs_total,
                    COUNT(*) FILTER (WHERE status = 'EMAIL_ENVIADO') AS emails_enviados,
                    COUNT(*) FILTER (WHERE pdf_acessado_em IS NOT NULL) AS acessos,
                    COUNT(*) FILTER (WHERE cadastrou_em IS NOT NULL) AS cadastros,
                    COUNT(*) FILTER (WHERE assinou_em IS NOT NULL) AS assinaturas,
                    COALESCE(SUM(valor_pago_centavos) FILTER (WHERE assinou_em IS NOT NULL), 0) AS receita_centavos
                FROM leads_outbound
            """)
            leads = dict(cur.fetchone() or {})

            cur.execute("""
                SELECT
                    COALESCE(SUM(valor_comissao_centavos), 0) AS total,
                    COALESCE(SUM(valor_comissao_centavos) FILTER (WHERE pago), 0) AS pagas,
                    COALESCE(SUM(valor_comissao_centavos) FILTER (WHERE NOT pago), 0) AS pendentes
                FROM comissoes
            """)
            comissoes = dict(cur.fetchone() or {})

        return {
            "representantes": {"total": reps.get("reps_total", 0), "ativos": reps.get("reps_ativos", 0)},
            "funil": {
                "pdfs": leads.get("pdfs_total", 0),
                "emails_enviados": leads.get("emails_enviados", 0),
                "acessos": leads.get("acessos", 0),
                "cadastros": leads.get("cadastros", 0),
                "assinaturas": leads.get("assinaturas", 0),
            },
            "receita_total_reais": (leads.get("receita_centavos", 0) or 0) / 100,
            "comissoes": {
                "total_reais": (comissoes.get("total", 0) or 0) / 100,
                "pagas_reais": (comissoes.get("pagas", 0) or 0) / 100,
                "pendentes_reais": (comissoes.get("pendentes", 0) or 0) / 100,
            },
        }
    finally:
        conn.close()


@app.get("/api/admin/representantes")
async def admin_listar_representantes(u=Depends(_requer_admin)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    p.id::text, p.email, p.nome_empresa,
                    p.comissao_pct_inicial, p.comissao_pct_recorrente,
                    p.codigo_convite, p.criado_em,
                    COALESCE(p.ativo, true) AS ativo,
                    p.senha_alterada_em,
                    (SELECT COUNT(*) FROM leads_outbound WHERE representante_id = p.id) AS pdfs_gerados,
                    (SELECT COUNT(*) FROM leads_outbound WHERE representante_id = p.id AND assinou_em IS NOT NULL) AS assinaturas,
                    (SELECT COALESCE(SUM(valor_comissao_centavos), 0) FROM comissoes WHERE representante_id = p.id) AS comissao_centavos,
                    (SELECT MAX(pdf_gerado_em) FROM leads_outbound WHERE representante_id = p.id) AS ultima_atividade
                FROM prestadores p
                WHERE p.is_representante = true
                ORDER BY p.criado_em DESC
            """)
            reps = []
            for r in cur.fetchall():
                d = dict(r)
                d["comissao_reais"] = (d.get("comissao_centavos", 0) or 0) / 100
                reps.append(d)
        return {"representantes": reps, "total": len(reps)}
    finally:
        conn.close()


@app.get("/api/admin/representantes/{rep_id}/leads")
async def admin_leads_representante(rep_id: str, u=Depends(_requer_admin)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text, empresa_nome, cnpj, setor, uf, status,
                       contato_email, contato_nome, contato_linkedin,
                       pdf_gerado_em, contato_confirmado_em,
                       pdf_acessado_em, pdf_acessos,
                       cadastrou_em, assinou_em,
                       plano_assinado, valor_pago_centavos
                FROM leads_outbound
                WHERE representante_id = %s
                ORDER BY pdf_gerado_em DESC
            """, (rep_id,))
            leads = []
            for r in cur.fetchall():
                d = dict(r)
                d["valor_pago_reais"] = (d.get("valor_pago_centavos") or 0) / 100
                leads.append(d)
        return {"leads": leads, "total": len(leads)}
    finally:
        conn.close()


@app.get("/api/admin/atividade-recente")
async def admin_atividade_recente(limite: int = 30, tipo: str = None, rep_id: str = None, u=Depends(_requer_admin)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            extra_where = ""
            params = []
            if tipo:
                extra_where += " AND tipo = %s"
                params.append(tipo)
            if rep_id:
                extra_where += " AND rep_id = %s"
                params.append(rep_id)
            params.append(limite)
            cur.execute("""
                WITH eventos AS (
                    SELECT lo.id::text AS lead_id, lo.empresa_nome, lo.pdf_gerado_em AS quando, 'PDF_GERADO' AS tipo, p.nome_empresa AS rep_nome, lo.representante_id::text AS rep_id
                    FROM leads_outbound lo JOIN prestadores p ON p.id = lo.representante_id
                    UNION ALL
                    SELECT lo.id::text, lo.empresa_nome, lo.contato_confirmado_em, 'EMAIL_ENVIADO', p.nome_empresa, lo.representante_id::text
                    FROM leads_outbound lo JOIN prestadores p ON p.id = lo.representante_id
                    WHERE lo.contato_confirmado_em IS NOT NULL
                    UNION ALL
                    SELECT lo.id::text, lo.empresa_nome, lo.pdf_acessado_em, 'ACESSOU_LINK', p.nome_empresa, lo.representante_id::text
                    FROM leads_outbound lo JOIN prestadores p ON p.id = lo.representante_id
                    WHERE lo.pdf_acessado_em IS NOT NULL
                    UNION ALL
                    SELECT lo.id::text, lo.empresa_nome, lo.cadastrou_em, 'CADASTROU', p.nome_empresa, lo.representante_id::text
                    FROM leads_outbound lo JOIN prestadores p ON p.id = lo.representante_id
                    WHERE lo.cadastrou_em IS NOT NULL
                    UNION ALL
                    SELECT lo.id::text, lo.empresa_nome, lo.assinou_em, 'ASSINOU', p.nome_empresa, lo.representante_id::text
                    FROM leads_outbound lo JOIN prestadores p ON p.id = lo.representante_id
                    WHERE lo.assinou_em IS NOT NULL
                )
                SELECT * FROM eventos WHERE quando IS NOT NULL""" + extra_where + """
                ORDER BY quando DESC LIMIT %s
            """, tuple(params))
            eventos = [dict(r) for r in cur.fetchall()]
        return {"eventos": eventos, "total": len(eventos)}
    finally:
        conn.close()


@app.post("/api/admin/representantes/{rep_id}/resetar-senha")
async def admin_resetar_senha(rep_id: str, u=Depends(_requer_admin)):
    import secrets as _secrets
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT email, nome_empresa FROM prestadores
                WHERE id = %s AND is_representante = true
            """, (rep_id,))
            rep = cur.fetchone()
            if not rep:
                raise HTTPException(404, "Representante não encontrado")
            rep = dict(rep)

            cur.execute("UPDATE prestadores SET senha_temporaria = true WHERE id = %s", (rep_id,))
            cur.execute("""
                UPDATE primeiro_acesso_tokens SET usado_em = COALESCE(usado_em, now())
                WHERE prestador_id = %s AND usado_em IS NULL
            """, (rep_id,))

            token_pa = _secrets.token_urlsafe(32)
            cur.execute("""
                INSERT INTO primeiro_acesso_tokens (prestador_id, token, expira_em)
                VALUES (%s, %s, now() + interval '24 hours')
            """, (rep_id, token_pa))
        conn.commit()

        link = f"https://winshubcomercial.com.br/primeiro-acesso?t={token_pa}"
        email_status = "skipped"
        try:
            api_key = os.getenv("RESEND_API_KEY", "")
            if api_key:
                html = f"""<!DOCTYPE html><html><body style="background:#0D1B2A;color:#fff;font-family:Arial,sans-serif;padding:40px;margin:0;">
<div style="max-width:560px;margin:0 auto;background:#1A2E42;border-radius:16px;padding:40px;">
  <h1 style="color:#F5A623;font-size:22px;margin:0 0 16px;">Reset de senha · WiNS Hub</h1>
  <p style="color:#fff;font-size:14px;line-height:1.6;">Olá <strong>{rep['nome_empresa']}</strong>, sua senha foi resetada pelo admin.</p>
  <p style="color:#fff;font-size:14px;line-height:1.6;margin:0 0 24px;">Clique no link abaixo para definir uma nova senha:</p>
  <div style="text-align:center;margin:32px 0;">
    <a href="{link}" style="background:linear-gradient(135deg, #F5A623 0%, #E89512 100%);color:#0D1B2A;text-decoration:none;padding:14px 32px;border-radius:24px;font-weight:700;">Definir nova senha →</a>
  </div>
  <p style="color:#8DA0B3;font-size:12px;">Link expira em 24 horas.</p>
</div></body></html>"""
                with httpx.Client(timeout=15.0) as client:
                    resp = client.post(
                        "https://api.resend.com/emails",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "from": RESEND_FROM,
                            "to": [rep["email"]],
                            "subject": "Reset de senha · WiNS Hub Comercial",
                            "html": html,
                        }
                    )
                email_status = "enviado" if resp.status_code in (200,201,202) else f"erro {resp.status_code}"
        except Exception as e:
            email_status = f"erro: {str(e)[:100]}"

        return {"ok": True, "link": link, "email_status": email_status}
    finally:
        conn.close()


@app.post("/api/admin/representantes/{rep_id}/desativar")
async def admin_desativar(rep_id: str, u=Depends(_requer_admin)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE prestadores SET ativo = false WHERE id = %s AND is_representante = true RETURNING id", (rep_id,))
            if not cur.fetchone():
                raise HTTPException(404, "Representante não encontrado")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/admin/representantes/{rep_id}/reativar")
async def admin_reativar(rep_id: str, u=Depends(_requer_admin)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE prestadores SET ativo = true WHERE id = %s AND is_representante = true RETURNING id", (rep_id,))
            if not cur.fetchone():
                raise HTTPException(404, "Representante não encontrado")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ─── Fila de Prospecção (V0.1.5) ─────────────────────────────────────

@app.post("/api/admin/fila-prospeccao/gerar-lote")
async def fila_gerar_lote(payload: dict, u=Depends(_requer_admin)):
    """Dispara enriquecimento em background. ~3s/lead (Brasil API + HEAD + Serper)."""
    rep_email = (payload or {}).get('rep_email')
    qtd = int((payload or {}).get('qtd', 100))
    if not rep_email:
        raise HTTPException(400, 'rep_email obrigatório')
    if qtd < 1 or qtd > 500:
        raise HTTPException(400, 'qtd entre 1 e 500')
    import subprocess
    proc = subprocess.Popen(
        ['python', '/app/scripts/enriquecer_fila.py', '--rep', rep_email, '--qtd', str(qtd)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return {'status': 'iniciado', 'pid': proc.pid, 'estimativa_min': round(qtd * 0.05, 1)}


@app.get("/api/admin/fila-prospeccao/stats")
async def fila_stats(u=Depends(_requer_admin)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT rep_atribuido,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status='PENDENTE') AS pendentes,
                       COUNT(*) FILTER (WHERE status_digital='ATIVO') AS ativos,
                       COUNT(*) FILTER (WHERE status='EM_CONTATO') AS em_contato,
                       COUNT(*) FILTER (WHERE status='CONVERTIDO') AS convertidos,
                       MAX(lote) AS ultimo_lote,
                       MAX(atribuido_em) AS ultimo_lote_em
                FROM fila_prospeccao
                WHERE rep_atribuido IS NOT NULL
                GROUP BY rep_atribuido
                ORDER BY total DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {'reps': rows}


@app.get("/api/admin/fila-prospeccao/export")
async def fila_export(rep_email: Optional[str] = None, u=Depends(_requer_admin)):
    """Export CSV (BOM UTF-8 + ;) da fila — Excel-friendly. Filtro opcional por rep."""
    import csv, io
    from fastapi.responses import StreamingResponse

    where = ""
    params: tuple = ()
    if rep_email:
        where = "WHERE rep_atribuido=%s"
        params = (rep_email,)

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                SELECT fornecedor_cnpj, razao_social, setor, score_match,
                       status_digital, site_url, linkedin_url, email_generico,
                       status, lote, atribuido_em, rep_atribuido
                FROM fila_prospeccao
                {where}
                ORDER BY lote DESC NULLS LAST, status_digital, score_match DESC NULLS LAST
            """, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    buf.write('﻿')  # BOM Excel UTF-8
    w = csv.writer(buf, delimiter=';')
    w.writerow([
        'CNPJ','Razao Social','Setor','Score','Status Digital',
        'Site','LinkedIn','Email','Status','Lote','Rep','Atribuido em'
    ])
    for r in rows:
        w.writerow([
            r['fornecedor_cnpj'] or '',
            r['razao_social'] or '',
            r['setor'] or '',
            r['score_match'] if r['score_match'] is not None else '',
            r['status_digital'] or '',
            r['site_url'] or '',
            r['linkedin_url'] or '',
            r['email_generico'] or '',
            r['status'] or '',
            f"#{r['lote']}" if r['lote'] is not None else '',
            r['rep_atribuido'] or '',
            r['atribuido_em'].strftime('%Y-%m-%d %H:%M') if r.get('atribuido_em') else '',
        ])

    fname = f"fila_{rep_email or 'todos'}_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )


# ─── Matchmaker on-demand (V0.1.6) ───────────────────────────────

@app.post("/api/admin/matchmaker/start")
async def matchmaker_start(modo: str = 'incremental', u=Depends(_requer_admin)):
    """Inicia worker em background. 409 se já tem job rodando.
    modo: 'incremental' (default, obras sem entry em matches_v2)
          'full' (todas OURO/PRATA/BRONZE/PIPELINE visiveis + OFICIAL ~4.6k)"""
    if modo not in ('full', 'incremental'):
        raise HTTPException(400, "modo deve ser 'full' ou 'incremental'")
    email = u.get('email')
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM matchmaker_jobs WHERE status='RODANDO' LIMIT 1")
            if cur.fetchone():
                raise HTTPException(409, 'Já tem job rodando')
            cur.execute("""
                INSERT INTO matchmaker_jobs (iniciado_por, status, modo)
                VALUES (%s, 'RODANDO', %s) RETURNING id
            """, (email, modo))
            job_id = cur.fetchone()['id']
        conn.commit()
    finally:
        conn.close()

    import subprocess
    proc = subprocess.Popen(
        ['python', '/app/scripts/matchmaker_worker.py', str(job_id), modo],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE matchmaker_jobs SET pid=%s WHERE id=%s", (proc.pid, str(job_id)))
        conn.commit()
    finally:
        conn.close()
    return {'job_id': str(job_id), 'pid': proc.pid}


@app.post("/api/admin/matchmaker/stop")
async def matchmaker_stop(u=Depends(_requer_admin)):
    """Sinaliza PAUSADO. Worker detecta no próximo checkpoint (~10 obras) e sai."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, pid FROM matchmaker_jobs WHERE status='RODANDO' ORDER BY iniciado_em DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, 'Sem job rodando')
            cur.execute("UPDATE matchmaker_jobs SET status='PAUSADO' WHERE id=%s", (row['id'],))
        conn.commit()
        if row.get('pid'):
            try:
                import os as _os, signal as _signal
                _os.kill(int(row['pid']), _signal.SIGTERM)
            except (ProcessLookupError, PermissionError, ValueError):
                pass
    finally:
        conn.close()
    return {'ok': True}


@app.get("/api/admin/matchmaker/status")
async def matchmaker_status(u=Depends(_requer_admin)):
    """Último job + count obras-alvo (sem match em matches_v2).
    Marca jobs RODANDO sem heartbeat > 60s como ZOMBIE (FIX D)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Zombie cleanup: jobs RODANDO sem heartbeat > 60s
            cur.execute("""
                UPDATE matchmaker_jobs
                SET status='ZOMBIE', finalizado_em=NOW(),
                    erro=COALESCE(erro,'') || ' [zombie: heartbeat stale]'
                WHERE status='RODANDO'
                  AND (heartbeat IS NULL AND iniciado_em < NOW() - INTERVAL '60 seconds'
                       OR heartbeat < NOW() - INTERVAL '60 seconds')
            """)
            cur.execute("""
                SELECT id::text, status, obras_alvo, obras_processadas, matches_criados,
                       iniciado_em, finalizado_em, iniciado_por, erro, modo, heartbeat
                FROM matchmaker_jobs
                ORDER BY iniciado_em DESC LIMIT 1
            """)
            job = cur.fetchone()
            cur.execute("""
                SELECT COUNT(*) AS n FROM obras o
                WHERE o.visivel=true
                  AND o.classificacao_computed IN ('OURO','PRATA','BRONZE','PIPELINE')
                  AND COALESCE(o.fonte_tipo,'OFICIAL') != 'NOTICIA'
                  AND o.setor IS NOT NULL AND o.uf IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM matches_v2 m WHERE m.obra_id=o.id)
            """)
            sem_match_incremental = cur.fetchone()['n']
            cur.execute("""
                SELECT COUNT(*) AS n FROM obras o
                WHERE o.visivel=true
                  AND o.classificacao_computed IN ('OURO','PRATA','BRONZE','PIPELINE')
                  AND COALESCE(o.fonte_tipo,'OFICIAL') != 'NOTICIA'
                  AND o.setor IS NOT NULL AND o.uf IS NOT NULL
            """)
            total_full = cur.fetchone()['n']
        conn.commit()
    finally:
        conn.close()
    return {
        'ultimo_job': dict(job) if job else None,
        'obras_sem_match': sem_match_incremental,
        'obras_full': total_full,
    }


@app.get("/api/admin/matchmaker/historico")
async def matchmaker_historico(limit: int = 5, u=Depends(_requer_admin)):
    """Últimos N jobs do matchmaker (default 5) pra tabela colapsável do painel admin."""
    lim = max(1, min(int(limit or 5), 50))
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text, iniciado_por, iniciado_em, finalizado_em, status,
                       obras_alvo, obras_processadas, matches_criados, erro,
                       EXTRACT(EPOCH FROM (COALESCE(finalizado_em, NOW()) - iniciado_em))::int AS duracao_seg
                FROM matchmaker_jobs
                ORDER BY iniciado_em DESC
                LIMIT %s
            """, (lim,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"jobs": rows, "total": len(rows)}


@app.get("/api/admin/matches-global")
async def admin_matches_global(score_min: int = 50, limit: int = 200, cnpj: str = None, u=Depends(_requer_admin)):
    """Admin: top matches global (matches_v2) sem filtro prestador_empresas.
    Opcional ?cnpj=XXX pra filtrar por fornecedor especifico."""
    lim = max(1, min(int(limit or 200), 1000))
    s_min = max(0, min(int(score_min or 50), 100))
    params = [s_min]
    where_cnpj = ""
    if cnpj:
        import re as _re_cnpj
        cnpj_norm = _re_cnpj.sub(r"[./-]", "", cnpj.strip())
        if cnpj_norm.isdigit() and len(cnpj_norm) == 14:
            where_cnpj = "AND mv.cnpj = %s"
            params.append(cnpj_norm)
    params.append(lim)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                SELECT mv.obra_id::text, mv.cnpj, mv.score, mv.score_breakdown, mv.gerado_em,
                       o.nome AS obra_nome, o.empresa, o.uf, o.setor, o.fase,
                       o.classificacao_computed, o.valor_formatado,
                       f.razao_social AS fornec_razao, f.nome_fantasia AS fornec_fantasia,
                       f.porte_inferido, f.uf AS fornec_uf
                FROM matches_v2 mv
                JOIN obras o ON o.id = mv.obra_id
                LEFT JOIN fornecedores f ON f.cnpj = mv.cnpj
                WHERE mv.score >= %s
                  AND (o.visivel IS NULL OR o.visivel=true)
                  {where_cnpj}
                ORDER BY mv.score DESC, mv.gerado_em DESC
                LIMIT %s
            """, params)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"matches": rows, "total": len(rows), "score_min": s_min, "cnpj_filtro": cnpj}


@app.get("/api/admin/prestador/{email}/perfil")
async def admin_prestador_perfil(email: str, u=Depends(_requer_admin)):
    """Admin: perfil completo de qualquer prestador (CNPJs vinculados, saldo, desbloqueios)."""
    email_norm = (email or "").strip().lower()
    if "@" not in email_norm:
        raise HTTPException(400, "Email invalido.")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text, email, nome_empresa, plano, is_representante,
                       criado_em, ultimo_acesso, ativo, status,
                       creditos_ganhos, creditos_consumidos,
                       (creditos_ganhos - creditos_consumidos) AS saldo_centavos,
                       onboarding_completo, badge_verificador
                FROM prestadores WHERE lower(email) = %s
            """, (email_norm,))
            prest = cur.fetchone()
            if not prest:
                raise HTTPException(404, "Prestador nao encontrado.")
            pid = prest["id"]
            cur.execute("""
                SELECT cnpj, razao_social, tipo, ativo
                FROM prestador_empresas WHERE prestador_id = %s
                ORDER BY ativo DESC, cnpj
            """, (pid,))
            empresas = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT obra_id::text, cnpj_empresa, faixa_valor, valor_cobrado, criado_em
                FROM desbloqueios WHERE prestador_id = %s
                ORDER BY criado_em DESC LIMIT 50
            """, (pid,))
            desbloqueios = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {
        "prestador": dict(prest),
        "empresas_vinculadas": empresas,
        "desbloqueios_recentes": desbloqueios,
        "total_empresas": len(empresas),
        "total_desbloqueios": len(desbloqueios),
    }


@app.get("/api/obras/{oid}/top-matches")
async def obra_top_matches(oid: str, u=Depends(get_user)):
    """Top 3 fornecedores por categoria de servico pra obra.
    Window function ROW_NUMBER() PARTITION BY categoria.
    Categoria primaria do match: pega 1a categoria (ordem ASC) cujo
    cnaes contem o cnae_codigo do breakdown.

    Fallback: se NENHUM cnae_codigo dos matches estiver mapeado em
    categorias_servico, retorna pseudo-categoria 'Outros Fornecedores
    Compativeis' com top 10 por score — evita seção vazia na UI mesmo
    quando o vocabulario de CNAEs cresce mais rapido que a curadoria de
    categorias_servico.

    Sempre retorna total_matches (count global de matches_v2 pra obra)
    pra que o frontend possa decidir habilitar botão PDF independente da
    cobertura de categorias.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) FROM matches_v2 WHERE obra_id = %s::uuid", (oid,))
            total_matches = int(cur.fetchone()["count"])

            # Dedup por raiz CNPJ + por categoria (mesmo grupo empresarial pode
            # ter varias filiais com matches no mesmo CNAE — mostra so a top).
            cur.execute("""
                WITH match_cat AS (
                    SELECT
                        m.cnpj, m.score::int AS score,
                        m.score_breakdown->>'cnae_codigo' AS cnae,
                        LEFT(m.cnpj, 8) AS raiz,
                        (SELECT cs.nome FROM categorias_servico cs
                         WHERE (m.score_breakdown->>'cnae_codigo') = ANY(cs.cnaes)
                         ORDER BY cs.ordem ASC, cs.id ASC LIMIT 1) AS categoria
                    FROM matches_v2 m
                    WHERE m.obra_id = %s::uuid
                ),
                deduped AS (
                    -- 1 cnpj por raiz dentro de cada categoria (top score)
                    SELECT mc.*,
                           ROW_NUMBER() OVER (
                             PARTITION BY categoria, raiz
                             ORDER BY score DESC, cnpj
                           ) AS rn_grp
                    FROM match_cat mc
                    WHERE categoria IS NOT NULL
                ),
                ranked AS (
                    SELECT d.*,
                           ROW_NUMBER() OVER (PARTITION BY categoria ORDER BY score DESC, cnpj) AS rnk,
                           COUNT(*) OVER (PARTITION BY categoria) AS cat_total
                    FROM deduped d
                    WHERE rn_grp = 1
                )
                SELECT r.cnpj, f.razao_social, f.nome_fantasia,
                       f.uf, f.porte_inferido, r.score, r.categoria, r.cnae, r.cat_total
                FROM ranked r
                JOIN fornecedores f ON f.cnpj = r.cnpj AND f.razao_social IS NOT NULL AND TRIM(f.razao_social) != ''
                WHERE r.rnk <= 3
                ORDER BY r.categoria, r.score DESC, r.cnpj
            """, (oid,))
            rows = [dict(r) for r in cur.fetchall()]

            # Fallback: 0 categorias mapeadas mas matches existem (CNAE sem categoria_servico).
            # Mesmo dedup por raiz aqui.
            fallback_rows: list = []
            if not rows and total_matches > 0:
                cur.execute("""
                    WITH ranked AS (
                        SELECT m.cnpj, m.score::int AS score,
                               f.razao_social, f.nome_fantasia,
                               f.uf, f.porte_inferido,
                               m.score_breakdown->>'cnae_codigo' AS cnae,
                               ROW_NUMBER() OVER (
                                 PARTITION BY LEFT(m.cnpj, 8)
                                 ORDER BY m.score DESC, m.cnpj
                               ) AS rn_grp
                        FROM matches_v2 m
                        JOIN fornecedores f ON f.cnpj = m.cnpj
                          AND f.razao_social IS NOT NULL AND TRIM(f.razao_social) != ''
                        WHERE m.obra_id = %s::uuid
                    )
                    SELECT cnpj, score, razao_social, nome_fantasia, uf, porte_inferido, cnae
                    FROM ranked
                    WHERE rn_grp = 1
                    ORDER BY score DESC, cnpj
                    LIMIT 10
                """, (oid,))
                fallback_rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # Agrupar por categoria
    from collections import OrderedDict
    grouped: "OrderedDict[str, dict]" = OrderedDict()
    for r in rows:
        cat = r["categoria"]
        if cat not in grouped:
            grouped[cat] = {"nome": cat, "total": r["cat_total"], "fornecedores": []}
        grouped[cat]["fornecedores"].append({
            "cnpj": r["cnpj"],
            "razao_social": r["razao_social"],
            "nome_fantasia": r["nome_fantasia"],
            "uf": r["uf"],
            "porte": r["porte_inferido"],
            "score": r["score"],
        })

    if not grouped and fallback_rows:
        grouped["Outros Fornecedores Compatíveis"] = {
            "nome": "Outros Fornecedores Compatíveis",
            "total": total_matches,
            "fallback": True,
            "fornecedores": [{
                "cnpj": r["cnpj"],
                "razao_social": r["razao_social"],
                "nome_fantasia": r["nome_fantasia"],
                "uf": r["uf"],
                "porte": r["porte_inferido"],
                "score": r["score"],
            } for r in fallback_rows],
        }

    return {"categorias": list(grouped.values()), "total_matches": total_matches}


@app.get("/api/obras/{oid}/time-ideal")
async def obra_time_ideal(oid: str, score_min: int = 50, peso_min: float = 0.5, u=Depends(get_user)):
    """Time ideal: 1 fornecedor por categoria de servico esperada pelo setor da obra.

    Atribuição multi-categoria (v1.0.0): cada match aparece em TODAS as
    categorias compatíveis (CNAE do match em cs.cnaes E scc.peso >= peso_min).
    Mesmo fornecedor pode ser top em N categorias se seu CNAE for relevante
    pra elas — reflete capacidade multi-skill honestamente.

    Antes (LIMIT 1 ordem ASC): fornecedor ia pra UMA categoria só (a de menor
    ordem), provocando "GAP" falso em categorias específicas (Caldeiraria,
    Tubulação) quando o CNAE era commodity (3311200, 7112000, 2599399) em
    múltiplas. Schema scc(setor, cnae) não suporta peso por (setor, cnae,
    categoria) pra desempate genuíno — abordagem multi-categoria contorna isso.

    Universo: categorias_servico com >=1 CNAE em scc(setor, peso>=peso_min).
    GAP = categoria do universo sem nenhum fornecedor scored (score >= score_min).
    """
    s_min = max(0, min(int(score_min or 50), 100))
    p_min = max(0.0, min(float(peso_min), 1.0))
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT setor FROM obras WHERE id = %s::uuid", (oid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Obra nao encontrada.")
            setor_obra = row["setor"]
            if not setor_obra:
                return {"obra_id": oid, "setor": None, "time": [],
                        "score_medio": 0, "cobertura_pct": 0, "gaps": 0,
                        "total_categorias": 0, "cobertas": 0,
                        "erro": "Obra sem setor definido"}

            cur.execute("""
                WITH match_cats AS (
                    -- Multi-categoria: 1 row por (match, categoria) onde cnae
                    -- pertence a cs.cnaes E scc(setor, cnae) tem peso suficiente.
                    -- Mesmo cnpj/match pode gerar N rows (uma por categoria que
                    -- aceita seu CNAE).
                    SELECT m.cnpj, m.score::int AS score,
                           cs.id AS categoria_id
                    FROM matches_v2 m
                    JOIN categorias_servico cs
                      ON (m.score_breakdown->>'cnae_codigo') = ANY(cs.cnaes)
                     AND cs.ativo = true
                    JOIN setor_cnae_compatibility scc
                      ON scc.setor_obra = %s
                     AND scc.cnae_codigo = (m.score_breakdown->>'cnae_codigo')
                     AND scc.peso >= %s
                    WHERE m.obra_id = %s::uuid AND m.score >= %s
                ),
                top_por_cat AS (
                    -- Top 1 fornec por categoria + alternativas (count distinct
                    -- nao precisa pq cada cnpj gera no maximo 1 row por categoria).
                    SELECT mc.*,
                           ROW_NUMBER() OVER (
                             PARTITION BY categoria_id
                             ORDER BY score DESC, cnpj
                           ) AS rnk_cat,
                           COUNT(*) OVER (PARTITION BY categoria_id) AS alternativas_cat
                    FROM match_cats mc
                ),
                cats_universo AS (
                    -- Universo: categorias com ao menos 1 cnae em scc(setor, peso>=p)
                    SELECT DISTINCT cs.id, cs.nome, cs.ordem
                    FROM categorias_servico cs
                    WHERE cs.ativo = true
                      AND EXISTS (
                          SELECT 1 FROM setor_cnae_compatibility scc
                          WHERE scc.setor_obra = %s
                            AND scc.cnae_codigo = ANY(cs.cnaes)
                            AND scc.peso >= %s
                      )
                )
                SELECT cs.id AS categoria_id, cs.nome AS categoria,
                       tpc.cnpj, tpc.score,
                       COALESCE(tpc.alternativas_cat, 0) AS alternativas_cat,
                       f.razao_social, f.nome_fantasia, f.uf,
                       f.porte_inferido AS porte,
                       CASE WHEN tpc.cnpj IS NULL THEN 'GAP' ELSE 'COBERTO' END AS status
                FROM cats_universo cs
                LEFT JOIN top_por_cat tpc ON tpc.categoria_id = cs.id AND tpc.rnk_cat = 1
                LEFT JOIN fornecedores f ON f.cnpj = tpc.cnpj
                  AND f.razao_social IS NOT NULL AND TRIM(f.razao_social) != ''
                ORDER BY cs.ordem ASC, cs.id ASC
            """, (setor_obra, p_min, oid, s_min, setor_obra, p_min))
            time_rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    cobertas = sum(1 for r in time_rows if r["status"] == "COBERTO")
    gaps = sum(1 for r in time_rows if r["status"] == "GAP")
    total = len(time_rows)
    scores = [r["score"] for r in time_rows if r["score"] is not None]
    score_medio = round(sum(scores) / len(scores), 1) if scores else 0
    cobertura_pct = round((cobertas / total) * 100) if total > 0 else 0

    return {
        "obra_id": oid,
        "setor": setor_obra,
        "time": time_rows,
        "score_medio": score_medio,
        "cobertura_pct": cobertura_pct,
        "gaps": gaps,
        "cobertas": cobertas,
        "total_categorias": total,
        "score_min": s_min,
    }


@app.get("/api/obras/{oid}/time-ideal/alternativas")
async def obra_time_alternativas(oid: str, categoria: str, limit: int = 5,
                                 exclude_cnpj: str = None, u=Depends(get_user)):
    """Top N alternativos pra trocar o fornecedor de uma categoria."""
    lim = max(1, min(int(limit or 5), 20))
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH match_cat AS (
                    SELECT m.cnpj, m.score::int AS score,
                           (SELECT cs.nome FROM categorias_servico cs
                            WHERE (m.score_breakdown->>'cnae_codigo') = ANY(cs.cnaes)
                            ORDER BY cs.ordem ASC, cs.id ASC LIMIT 1) AS categoria_nome
                    FROM matches_v2 m
                    WHERE m.obra_id = %s::uuid
                )
                SELECT mc.cnpj, mc.score, f.razao_social, f.nome_fantasia,
                       f.uf, f.porte_inferido AS porte
                FROM match_cat mc
                JOIN fornecedores f ON f.cnpj = mc.cnpj
                WHERE mc.categoria_nome = %s
                  AND f.razao_social IS NOT NULL AND TRIM(f.razao_social) != ''
                  AND (%s::text IS NULL OR mc.cnpj != %s)
                ORDER BY mc.score DESC, mc.cnpj
                LIMIT %s
            """, (oid, categoria, exclude_cnpj, exclude_cnpj, lim))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"categoria": categoria, "alternativas": rows, "total": len(rows)}


@app.get("/api/admin/me-token")
async def admin_me_token(u=Depends(_requer_admin)):
    """Retorna ADMIN_TOKEN pra frontend admin popular localStorage automaticamente.
    Evita o window.prompt() em v2-admin.js _token() apos login bem-sucedido.
    So user com JWT admin (email==ADMIN_EMAIL) pode chamar."""
    tok = os.environ.get('ADMIN_TOKEN', '')
    if not tok:
        raise HTTPException(503, 'ADMIN_TOKEN env nao configurado')
    return {'admin_token': tok}


@app.get("/api/representante/minha-fila")
async def minha_fila(u=Depends(obter_usuario_completo)):
    """Rep vê só sua própria fila PENDENTE, ordenada por status digital + score."""
    email = u.get('email')
    if not email:
        raise HTTPException(401, 'auth required')
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text, fornecedor_cnpj, razao_social, setor, score_match,
                       site_url, site_ativo, linkedin_url, email_generico, status_digital,
                       status, observacoes, lote
                FROM fila_prospeccao
                WHERE rep_atribuido = %s AND status='PENDENTE'
                ORDER BY
                  CASE status_digital
                    WHEN 'ATIVO' THEN 1
                    WHEN 'SEM_LINKEDIN' THEN 2
                    WHEN 'SEM_SITE' THEN 3
                    WHEN 'INVALIDO' THEN
                      CASE WHEN email_generico IS NOT NULL AND email_generico <> '' THEN 4 ELSE 5 END
                    ELSE 6
                  END,
                  score_match DESC NULLS LAST,
                  fornecedor_cnpj
            """, (email,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {'fila': rows}


@app.patch("/api/representante/fila/{lead_id}")
async def fila_atualizar_lead(lead_id: str, payload: dict, u=Depends(obter_usuario_completo)):
    email = u.get('email')
    if not email:
        raise HTTPException(401, 'auth required')
    novo_status = (payload or {}).get('status')
    if novo_status not in ('PENDENTE','EM_CONTATO','RESPONDEU','NAO_ATENDE','INVALIDO','CONVERTIDO','PULADO'):
        raise HTTPException(400, 'status inválido')
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE fila_prospeccao
                SET status=%s,
                    resultado=COALESCE(%s, resultado),
                    observacoes=COALESCE(%s, observacoes),
                    contatado_em=CASE WHEN %s <> 'PENDENTE' THEN NOW() ELSE contatado_em END
                WHERE id=%s AND rep_atribuido=%s
                RETURNING id
            """, (
                novo_status,
                (payload or {}).get('resultado'),
                (payload or {}).get('observacoes'),
                novo_status,
                lead_id, email,
            ))
            if not cur.fetchone():
                raise HTTPException(404, 'lead não encontrado')
        conn.commit()
    finally:
        conn.close()
    return {'ok': True}


@app.get("/api/vendas/pdf-fornecedor/{cnpj}")
async def pdf_fornecedor_descontinuado(cnpj: str):
    """DESCONTINUADO. Use POST /api/vendas/gerar-pdf-match."""
    raise HTTPException(410, "Endpoint descontinuado, use /api/vendas/gerar-pdf-match")


# ─── Perfil completo do fornecedor (P1.1) ────────────────────────────
PERFIL_OPCOES_FAIXAS_FATURAMENTO = ["<R$5Mi", "R$5-50Mi", "R$50-300Mi", ">R$300Mi"]
PERFIL_OPCOES_PORTE = ["ME", "EPP", "Médio", "Grande"]
PERFIL_OPCOES_UFS = [
    "AC","AL","AP","AM","BA","CE","DF","ES","GO","MA","MT","MS","MG","PA",
    "PB","PR","PE","PI","RJ","RN","RS","RO","RR","SC","SP","SE","TO"
]
PERFIL_OPCOES_CERTIFICACOES = [
    {"chave": "ISO_9001", "label": "ISO 9001 (Qualidade)"},
    {"chave": "ISO_14001", "label": "ISO 14001 (Ambiental)"},
    {"chave": "ISO_45001", "label": "ISO 45001 (Saúde & Segurança)"},
    {"chave": "ISO_27001", "label": "ISO 27001 (Segurança da Informação)"},
    {"chave": "NR_10", "label": "NR-10 (Elétrica)"},
    {"chave": "NR_12", "label": "NR-12 (Máquinas)"},
    {"chave": "NR_13", "label": "NR-13 (Caldeiras/Vasos)"},
    {"chave": "NR_33", "label": "NR-33 (Confinado)"},
    {"chave": "NR_35", "label": "NR-35 (Altura)"},
    {"chave": "SASSMAQ", "label": "SASSMAQ (Petroquímico)"},
    {"chave": "PBQP_H", "label": "PBQP-H (Habitação)"},
    {"chave": "CRC", "label": "CRC (Cadastro Receita/Construtoras)"},
]
PERFIL_OPCOES_ESPECIALIDADES = [
    "Construção Civil", "Estrutura Metálica", "Elétrica Industrial",
    "Mecânica/Tubulação", "Hidráulica", "Automação", "Instrumentação",
    "Caldeiraria", "Soldagem", "Pintura Industrial", "Isolamento Térmico",
    "Andaimes", "Movimentação de Carga", "Terraplenagem", "Sondagem/Geotecnia",
    "Topografia", "Saneamento", "HVAC/Climatização", "Pavimentação",
    "Geração Solar/Eólica", "Transmissão Energia", "Telecomunicações",
    "TI/Datacenter", "Segurança Patrimonial", "Consultoria Engenharia"
]
PERFIL_CAMPOS_PERMITIDOS = {
    "cnaes_primario", "cnaes_secundarios", "ufs_atuacao", "especialidades_tags",
    "tamanho_porte", "capex_min_milhoes", "capex_max_milhoes",
    "faixa_faturamento", "viaja_nacional", "viaja_internacional",
    "certificacoes", "telefone_comercial", "site_institucional",
    "linkedin_empresa", "referencias_obras", "cases_breve",
}


@app.get("/api/perfil/opcoes")
async def perfil_opcoes():
    """Retorna listas estáticas usadas pelo wizard de perfil."""
    return {
        "ufs": PERFIL_OPCOES_UFS,
        "porte": PERFIL_OPCOES_PORTE,
        "faixa_faturamento": PERFIL_OPCOES_FAIXAS_FATURAMENTO,
        "certificacoes": PERFIL_OPCOES_CERTIFICACOES,
        "especialidades": PERFIL_OPCOES_ESPECIALIDADES,
    }


@app.get("/api/me/perfil/sugestao-receita")
async def perfil_sugestao_receita(u=Depends(requer_auth)):
    """Pre-fill com dados da Receita Federal a partir do CNPJ próprio do prestador."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT f.cnpj, f.cnae_principal, f.cnae_secundarios,
                       f.porte, f.uf, f.municipio_nome,
                       f.telefone_1, f.email, f.capital_social
                FROM prestador_empresas pe
                JOIN fornecedores f ON f.cnpj = pe.cnpj
                WHERE pe.prestador_id = %s AND pe.ativo
                ORDER BY (pe.tipo = 'proprio') DESC, pe.criado_em
                LIMIT 1
            """, (u["sub"],))
            row = cur.fetchone()
        if not row:
            return {"encontrado": False}
        porte_map = {"01": "ME", "03": "EPP", "05": "Médio", "MICRO EMPRESA": "ME",
                     "EMPRESA DE PEQUENO PORTE": "EPP", "DEMAIS": "Grande"}
        porte_padronizado = porte_map.get((row.get("porte") or "").strip().upper(), row.get("porte"))
        return {
            "encontrado": True,
            "cnpj": row["cnpj"],
            "cnae_principal": row.get("cnae_principal"),
            "cnae_secundarios": row.get("cnae_secundarios") or [],
            "porte_sugerido": porte_padronizado,
            "uf_sugerida": row.get("uf"),
            "municipio": row.get("municipio_nome"),
            "telefone_comercial_sugerido": row.get("telefone_1"),
            "capital_social": float(row["capital_social"]) if row.get("capital_social") else None,
        }
    finally:
        conn.close()


def _perfil_completude_calc(row):
    """Calcula % preenchimento + lista de faltantes."""
    obrigatorios = ["cnaes_primario", "ufs_atuacao", "capex_max_milhoes", "telefone_comercial"]
    recomendados = ["especialidades_tags", "certificacoes", "cases_breve", "referencias_obras",
                    "faixa_faturamento", "tamanho_porte", "site_institucional"]

    def preenchido(v):
        if v is None:
            return False
        if isinstance(v, str):
            return v.strip() != ""
        if isinstance(v, (list, dict)):
            return len(v) > 0
        return True

    obr_p = sum(1 for f in obrigatorios if preenchido(row.get(f)))
    rec_p = sum(1 for f in recomendados if preenchido(row.get(f)))
    obr_total = len(obrigatorios)
    rec_total = len(recomendados)
    total_p = obr_p + rec_p
    total_t = obr_total + rec_total
    pct = round(100 * total_p / total_t) if total_t else 0
    falta = [f for f in obrigatorios + recomendados if not preenchido(row.get(f))]
    perfil_completo = obr_p == obr_total and pct >= 80
    return {
        "completude_percent": pct,
        "obrigatorios_preenchidos": obr_p,
        "obrigatorios_total": obr_total,
        "recomendados_preenchidos": rec_p,
        "recomendados_total": rec_total,
        "falta": falta,
        "perfil_completo": perfil_completo,
    }


@app.get("/api/me/perfil/completude")
async def perfil_completude(u=Depends(requer_auth)):
    """% de preenchimento do perfil + campos faltantes + wizard simplificado se signup via Receita."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT cnaes_primario, cnaes_secundarios, ufs_atuacao,
                       especialidades_tags, tamanho_porte,
                       capex_min_milhoes, capex_max_milhoes,
                       faixa_faturamento, viaja_nacional, viaja_internacional,
                       certificacoes, telefone_comercial, site_institucional,
                       linkedin_empresa, referencias_obras, cases_breve,
                       COALESCE(source, 'cadastro_manual') AS source
                FROM prestadores WHERE id=%s
            """, (u["sub"],))
            row = dict(cur.fetchone() or {})
    finally:
        conn.close()
    res = _perfil_completude_calc(row)
    res["source"] = row.get("source")
    res["wizard_simplificado"] = row.get("source") == "descoberto_via_receita"
    return res


@app.patch("/api/me/perfil")
async def perfil_patch(req: dict = Body(...), u=Depends(requer_auth)):
    """Update parcial do perfil do prestador. Aceita só campos da whitelist."""
    if not isinstance(req, dict):
        raise HTTPException(400, "Body deve ser objeto JSON.")
    updates = {k: v for k, v in req.items() if k in PERFIL_CAMPOS_PERMITIDOS}
    if not updates:
        raise HTTPException(400, "Nenhum campo permitido recebido.")
    # Construir SET dinamicamente
    cols = list(updates.keys())
    placeholders = ", ".join(f"{c} = %s" for c in cols)
    params = []
    for c in cols:
        v = updates[c]
        if c == "certificacoes" and isinstance(v, dict):
            params.append(json.dumps(v))
        elif c in ("cnaes_secundarios", "ufs_atuacao", "especialidades_tags", "referencias_obras"):
            params.append(v if isinstance(v, list) else [])
        else:
            params.append(v)
    params.append(u["sub"])
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"UPDATE prestadores SET {placeholders} WHERE id=%s "
                        f"RETURNING cnaes_primario, ufs_atuacao, tamanho_porte, "
                        f"capex_max_milhoes, faixa_faturamento, telefone_comercial",
                        params)
            row = dict(cur.fetchone() or {})
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "atualizado": cols, "perfil": row}


@app.get("/api/perfil/onboarding")
async def onboarding_status(u=Depends(requer_auth)):
    """Retorna se o prestador já completou o onboarding."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT COALESCE(onboarding_completo, false) AS completo,
                       onboarding_completo_em,
                       (SELECT COUNT(*) FROM interacoes 
                        WHERE prestador_id=%s AND tipo='DESBLOQUEIO') AS desbloqueios,
                       (SELECT COUNT(*) FROM contatos_log
                        WHERE prestador_id=%s) AS contatos
                FROM prestadores WHERE id=%s
            """, (u["sub"], u["sub"], u["sub"]))
            row = dict(cur.fetchone() or {})
        return {
            "completo": row.get("completo", False),
            "desbloqueios": row.get("desbloqueios", 0),
            "contatos": row.get("contatos", 0),
        }
    finally:
        conn.close()


@app.post("/api/perfil/onboarding/completar")
async def onboarding_completar(u=Depends(requer_auth)):
    """Marca o onboarding como completo (não aparece mais)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE prestadores SET
                    onboarding_completo = true,
                    onboarding_completo_em = now()
                WHERE id=%s
            """, (u["sub"],))
            conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/perfil/convite")
async def meu_convite(u=Depends(requer_auth)):
    """Retorna link de convite único do prestador."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT codigo_convite,
                    (SELECT COUNT(*) FROM prestadores
                     WHERE convidado_por=%s) AS convidados,
                    (SELECT COUNT(*) FROM prestadores p2
                     JOIN contatos_log cl ON cl.prestador_id=p2.id
                     WHERE p2.convidado_por=%s
                       AND cl.confirmado_pelo_decisor=true) AS convidados_ativos
                FROM prestadores WHERE id=%s
            """, (u["sub"], u["sub"], u["sub"]))
            row = dict(cur.fetchone() or {})
    finally:
        conn.close()

    codigo = row.get("codigo_convite", "")
    return {
        "codigo": codigo,
        "link": f"https://winshubcomercial.com.br/login?convite={codigo}",
        "convidados": row.get("convidados", 0),
        "convidados_ativos": row.get("convidados_ativos", 0),
        "bonus_por_convidado_ativo": "R$20,00",
    }

@app.get("/api/prestador/alertas/badge")
async def alertas_badge(u=Depends(requer_auth)):
    """GET read-only: quantas obras novas desde ultimo /marcar-visto. Nao altera estado."""
    from datetime import datetime, timezone, timedelta
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            prestador_id = u["sub"]
            cur.execute(
                "SELECT last_inapp_alert_at FROM prestadores WHERE id=%s",
                (prestador_id,)
            )
            row = cur.fetchone()
            since = (row["last_inapp_alert_at"] if row and row["last_inapp_alert_at"]
                     else datetime.now(timezone.utc) - timedelta(days=7))

            cur.execute("""
                SELECT DISTINCT ON (o.id)
                    o.id AS obra_id, o.nome, mo.score, o.fase, o.uf,
                    o.valor_estimado, o.setor
                FROM matches_obra_prestador mo
                JOIN prestador_empresas pe ON pe.cnpj = mo.cnpj
                JOIN obras o ON o.id = mo.obra_id
                WHERE pe.prestador_id = %s
                  AND pe.ativo = true
                  AND mo.gerado_em > %s
                  AND mo.score >= 50
                  AND o.fase IN (
                      'PLANEJAMENTO','EM_EXECUCAO',
                      'LICENCA_INSTALACAO','LICENCA_PREVIA',
                      'PROJETO','LICITACAO_ABERTA'
                  )
                  AND (o.cnpj_status IS NULL OR o.cnpj_status = 'ok')
                  AND (o.visivel IS NULL OR o.visivel = true)
                ORDER BY o.id, mo.score DESC
                LIMIT 5
            """, (prestador_id, since))
            obras = cur.fetchall()

        return {
            "obras_novas": len(obras),
            "desde": since.isoformat() if hasattr(since, 'isoformat') else str(since),
            "top_obras": [dict(o) for o in obras]
        }
    finally:
        conn.close()

@app.post("/api/prestador/alertas/marcar-visto")
async def alertas_marcar_visto(u=Depends(requer_auth)):
    """POST: marca obras como vistas (atualiza last_inapp_alert_at). Chamar so quando user clica no sino."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE prestadores SET last_inapp_alert_at=now() WHERE id=%s",
                (u["sub"],)
            )
            conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/obras")
async def listar_obras(
    uf: str = None, setor: str = None, fase: str = None, busca: str = None,
    ufs: str = None, setores: str = None, fases: str = None,
    tiers: str = None, capex: str = None, ordem: str = None,
    score_min: int = 0, proximidade: str = None,
    limit: int = 50, offset: int = 0, tier: str = None, apenas_ouro: int = 0, apenas_prata: int = 0, apenas_bronze: int = 0, apenas_meus_matches: int = 0, u=Depends(get_user)
):
    """Aceita 'uf' (single, legado) ou 'ufs' (csv, novo modelo facetado)."""
    plano = u["plano"] if u else "GRATUITO"
    # Obras são públicas — cap único em 100. Decisor é o que diferencia plano (mascarado via filtrar_obra).
    lim = min(limit, 100)
    conn = get_conn()
    cond = ["1=1"]
    DECISOR_EXISTS_SQL = "EXISTS (SELECT 1 FROM decisores_obra d WHERE d.obra_id = obras.id AND d.excluido_em IS NULL)"
    # ═══════════════════════════════════════════════════════════════
    # REGRA IMUTÁVEL — NÃO ALTERAR SEM APROVAÇÃO EXPLÍCITA DO WILLIAM
    # OURO  = classificacao_computed = 'OURO'  (capex >= R$ 500 milhões)
    # PRATA = classificacao_computed = 'PRATA' (capex >= R$ 50 milhões e < R$ 500 milhões)
    # Filtros apenas_ouro/apenas_prata usam classificacao_computed — igual aos hero counters.
    # ═══════════════════════════════════════════════════════════════
    if tier and tier.upper() in ('OURO','PRATA','BRONZE','PIPELINE'):
        cond.append(f"classificacao_computed = '{tier.upper()}'")
    # tiers (csv multi) — sidebar filtro nova
    _tiers_list = []
    if tiers:
        _tiers_list = [t.upper() for t in tiers.split(",") if t.upper() in ('OURO','PRATA','BRONZE','PIPELINE')]
        if _tiers_list:
            cond.append("classificacao_computed = ANY(%s)")
    if apenas_ouro:
        cond.append("classificacao_computed = 'OURO'")
    if apenas_prata:
        cond.append("classificacao_computed = 'PRATA'")
    if apenas_bronze:
        cond.append("classificacao_computed = 'BRONZE'")
    params = []
    # ── Filtros adicionais quando apenas_meus_matches=1 ──
    # score_min default 50 (baseline anti-FP); proximidade csv com whitelist.
    _VALID_PROX = {'municipio', 'vizinha', 'uf', 'distante'}
    _score_threshold = max(int(score_min), 0) if score_min else 50
    _prox_list = [p.strip() for p in (proximidade or "").split(",") if p.strip()]
    _prox_clean = [p for p in _prox_list if p in _VALID_PROX]
    _has_nacional = 'nacional' in _prox_list
    if _prox_clean and _has_nacional:
        _prox_sql = "AND (mo.nivel_proximidade = ANY(ARRAY[%s]) OR mo.escopo = 'nacional')" %                     ",".join(f"'{p}'" for p in _prox_clean)
    elif _prox_clean:
        _prox_sql = "AND mo.nivel_proximidade = ANY(ARRAY[%s])" %                     ",".join(f"'{p}'" for p in _prox_clean)
    elif _has_nacional:
        _prox_sql = "AND mo.escopo = 'nacional'"
    else:
        _prox_sql = ""

    if apenas_meus_matches and u:
        cond.append("fase IN ('PLANEJAMENTO','EM_EXECUCAO','LICENCA_INSTALACAO','LICENCA_PREVIA','PROJETO','LICITACAO_ABERTA')")
        # Feature flag v1 vs v2 (rollback safe)
        # ADMIN BYPASS: ve todos matches sem filtrar por prestador_empresas
        _is_admin = bool(u and u.get('is_admin'))
        if MATCHMAKER_VERSION == 'v2':
            if _is_admin:
                cond.append(f"""EXISTS (
                    SELECT 1 FROM matches_v2 mv
                    WHERE mv.obra_id = obras.id AND mv.score >= {_score_threshold}
                )""")
            else:
                cond.append(f"""EXISTS (
                    SELECT 1 FROM matches_v2 mv
                    JOIN prestador_empresas pe ON pe.cnpj = mv.cnpj
                    WHERE mv.obra_id = obras.id
                      AND pe.prestador_id = %s
                      AND pe.ativo = true
                      AND mv.score >= {_score_threshold}
                )""")
                params.append(u["sub"])
        else:
            if _is_admin:
                cond.append(f"""EXISTS (
                    SELECT 1 FROM matches_obra_prestador mo
                    WHERE mo.obra_id = obras.id AND mo.score >= {_score_threshold}
                      {_prox_sql}
                )""")
            else:
                cond.append(f"""EXISTS (
                    SELECT 1 FROM matches_obra_prestador mo
                    JOIN prestador_empresas pe ON pe.cnpj = mo.cnpj
                    WHERE mo.obra_id = obras.id
                      AND pe.prestador_id = %s
                      AND pe.ativo = true
                      AND mo.score >= {_score_threshold}
                      {_prox_sql}
                )""")
                params.append(u["sub"])

    # empurra params na ORDEM dos cond.append acima
    if _tiers_list:
        params.append(_tiers_list)
    # capex buckets (csv): lt50, b50_500, b500_1000, gt1000
    if capex:
        capex_conds = []
        for b in capex.split(","):
            b = b.strip()
            if b == "lt50":
                capex_conds.append("(valor_estimado IS NOT NULL AND valor_estimado < 50000000)")
            elif b == "b50_500":
                capex_conds.append("(valor_estimado >= 50000000 AND valor_estimado < 500000000)")
            elif b == "b500_1000":
                capex_conds.append("(valor_estimado >= 500000000 AND valor_estimado < 1000000000)")
            elif b == "gt1000":
                capex_conds.append("(valor_estimado >= 1000000000)")
        if capex_conds:
            cond.append("(" + " OR ".join(capex_conds) + ")")

    # Aceita single (legado) ou lista (novo)
    ufs_list = [x for x in (ufs or "").split(",") if x] or ([uf.upper()] if uf else [])
    setores_list = [x for x in (setores or "").split(",") if x] or ([setor] if setor else [])
    fases_list = [x for x in (fases or "").split(",") if x] or ([fase] if fase else [])

    if ufs_list:
        cond.append("uf = ANY(%s)")
        params.append([u.upper() for u in ufs_list])
    if setores_list:
        cond.append("setor = ANY(%s)")
        params.append(setores_list)
    if fases_list:
        cond.append("fase = ANY(%s)")
        params.append(fases_list)
    if busca:
        cond.append("(unaccent(lower(nome)) ILIKE unaccent(lower(%s)) OR unaccent(lower(empresa)) ILIKE unaccent(lower(%s)))")
        params.extend([f"%{busca}%", f"%{busca}%"])
    # Obras são públicas em todas as fases. Decisor é o pago (mascarado via filtrar_obra).

    # score_match + score_breakdown (subqueries que só rodam quando apenas_meus_matches)
    if apenas_meus_matches and u:
        _is_admin_score = bool(u.get('is_admin'))
        if MATCHMAKER_VERSION == 'v2':
            if _is_admin_score:
                _match_select_cols = f"""(
                    SELECT MAX(mv.score)::int FROM matches_v2 mv
                      WHERE mv.obra_id = obras.id AND mv.score >= {_score_threshold}
                ) AS score_match,
                (SELECT mv.score_breakdown FROM matches_v2 mv
                   WHERE mv.obra_id = obras.id AND mv.score >= {_score_threshold}
                   ORDER BY mv.score DESC LIMIT 1) AS score_breakdown,
                (SELECT mv.score_breakdown->>'cnae_codigo' FROM matches_v2 mv
                   WHERE mv.obra_id = obras.id AND mv.score >= {_score_threshold}
                   ORDER BY mv.score DESC LIMIT 1) AS categoria_match,"""
                _match_params = []
            else:
                _match_select_cols = f"""(
                    SELECT MAX(mv.score)::int FROM matches_v2 mv
                      JOIN prestador_empresas pe ON pe.cnpj = mv.cnpj
                      WHERE mv.obra_id = obras.id AND pe.prestador_id = %s AND pe.ativo = true
                        AND mv.score >= {_score_threshold}
                ) AS score_match,
                (SELECT mv.score_breakdown FROM matches_v2 mv
                   JOIN prestador_empresas pe ON pe.cnpj = mv.cnpj
                   WHERE mv.obra_id = obras.id AND pe.prestador_id = %s AND pe.ativo = true
                     AND mv.score >= {_score_threshold}
                   ORDER BY mv.score DESC LIMIT 1) AS score_breakdown,
                (SELECT mv.score_breakdown->>'cnae_codigo' FROM matches_v2 mv
                   JOIN prestador_empresas pe ON pe.cnpj = mv.cnpj
                   WHERE mv.obra_id = obras.id AND pe.prestador_id = %s AND pe.ativo = true
                     AND mv.score >= {_score_threshold}
                   ORDER BY mv.score DESC LIMIT 1) AS categoria_match,"""
                _match_params = [u["sub"], u["sub"], u["sub"]]
        else:
            if _is_admin_score:
                _match_select_cols = f"""(
                    SELECT MAX(m.score)::int FROM matches_obra_prestador m
                      WHERE m.obra_id = obras.id AND m.score >= {_score_threshold}
                        {_prox_sql.replace("mo.", "m.")}
                ) AS score_match,
                (SELECT c.nome FROM matches_obra_prestador m
                   JOIN categorias_servico c ON c.id = m.categoria_id
                   WHERE m.obra_id = obras.id AND m.score >= {_score_threshold}
                     {_prox_sql.replace("mo.", "m.")}
                   ORDER BY m.score DESC LIMIT 1) AS categoria_match,"""
                _match_params = []
            else:
                _match_select_cols = f"""(
                    SELECT MAX(m.score)::int FROM matches_obra_prestador m
                      JOIN prestador_empresas pe ON pe.cnpj = m.cnpj
                      WHERE m.obra_id = obras.id AND pe.prestador_id = %s AND pe.ativo = true
                        AND m.score >= {_score_threshold}
                        {_prox_sql.replace("mo.", "m.")}
                ) AS score_match,
                (SELECT c.nome FROM matches_obra_prestador m
                   JOIN prestador_empresas pe ON pe.cnpj = m.cnpj
                   JOIN categorias_servico c ON c.id = m.categoria_id
                   WHERE m.obra_id = obras.id AND pe.prestador_id = %s AND pe.ativo = true
                     AND m.score >= {_score_threshold}
                     {_prox_sql.replace("mo.", "m.")}
                   ORDER BY m.score DESC LIMIT 1) AS categoria_match,"""
                _match_params = [u["sub"], u["sub"]]
    else:
        _match_select_cols = ""
        _match_params = []

    _orderby_map = {
        "recente":     "criado_em DESC NULLS LAST",
        "capex_desc":  "valor_estimado DESC NULLS LAST",
        "capex_asc":   "valor_estimado ASC NULLS LAST",
        "nome_asc":    "nome ASC",
        "nome_desc":   "nome DESC",
    }
    _default_orderby = (
        "score_match DESC NULLS LAST, rank_in_empresa ASC"
        if apenas_meus_matches and u
        else "rank_in_empresa ASC, urgencia ASC, lead_score DESC NULLS LAST"
    )
    _orderby_sql = _orderby_map.get((ordem or "").strip().lower(), _default_orderby)

    w = " AND ".join(cond)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Diversificação por empresa (ISSUE-002 — pitch fix):
        # ROW_NUMBER particionado por empresa rankeia obras DENTRO da mesma
        # empresa por urgencia/lead_score; ORDER BY rank_in_empresa primeiro
        # faz o top-N intercalar empresas (rank=1 de cada empresa antes do
        # rank=2 de qualquer uma). Resolve concentração de Petrobras no top.
        cur.execute(f"""
            SELECT * FROM (
                SELECT obras.*,
                    {DECISOR_EXISTS_SQL} AS tem_decisor_externo,
                    {OURO_DECISOR_SQL} AS is_ouro_sql,
                    {PRATA_MATCH_SQL} AS is_prata_match_sql,
                    {PIPELINE_SQL} AS is_pipeline_sql,
                    {SCORE_PROSPECCAO_SQL} AS score_prospeccao,
                    -- Sprint 1 Auditoria Dedup: flag de decisor replicado FP
                    -- (1233 rows em decisores_obra com hipotese_replicacao=
                    -- 'REPLICADO_PROVAVEL_FALSO_POSITIVO'). filtrar_obra zera
                    -- nivel1_* quando true. Ex.: Francisco Antonio Rueda.
                    EXISTS (
                        SELECT 1 FROM decisores_obra dob
                        WHERE dob.obra_id = obras.id
                          AND dob.nome = obras.nivel1_nome
                          AND dob.hipotese_replicacao = 'REPLICADO_PROVAVEL_FALSO_POSITIVO'
                          AND dob.excluido_em IS NULL
                    ) AS decisor_replicado_fp,
    obra_janela_score(
        obras.fase,
        obras.data_publicacao,
        obras.status_licenca,
        obras.valor_estimado
    ) AS janela_score,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(NULLIF(empresa, ''), cnpj, id::text)
                        ORDER BY urgencia ASC, lead_score DESC NULLS LAST
                    ) AS rank_in_empresa,
                    {_match_select_cols}
                    EXTRACT(DAY FROM NOW() - obras.validacao_obra_at)::INTEGER AS dias_desde_validacao,
                    COALESCE(obras.validacao_manual_status, ufv.existencia_status) AS url_validacao_status,
                    obras.obra_listada_na_fonte AS obra_listada_na_fonte,
                    obras.obra_dados_mudaram_at AS obra_dados_mudaram_at
                FROM obras
                LEFT JOIN urls_fonte_validacao ufv ON ufv.url_fonte = obras.url_fonte
                WHERE {w} AND (visivel IS NULL OR visivel = true) AND empresa IS NOT NULL AND empresa <> ''
            ) ranked
            ORDER BY {_orderby_sql}
            LIMIT %s OFFSET %s
        """, _match_params + params + [lim, offset])
        obras = cur.fetchall()
        cur.execute(f"SELECT COUNT(*) FROM obras WHERE {w} AND (visivel IS NULL OR visivel = true) AND empresa IS NOT NULL AND empresa <> ''", params)
        total = cur.fetchone()["count"]
    ids_desbl = []
    if u:
        with conn.cursor() as cur:
            cur.execute("SELECT obra_id::text FROM interacoes WHERE prestador_id=%s AND tipo='DESBLOQUEIO'", (u["sub"],))
            ids_desbl = [r[0] for r in cur.fetchall()]
    conn.close()
    return {"dados": [filtrar_obra(dict(o), plano, str(o["id"]) in ids_desbl) for o in obras], "total": total, "plano": plano}


@app.get("/api/obras/{oid}")
async def detalhe_obra(oid:str,u=Depends(get_user)):
    _validar_uuid(oid)
    plano=u["plano"] if u else "GRATUITO"
    conn=get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT obras.*, EXISTS (SELECT 1 FROM decisores_obra d WHERE d.obra_id = obras.id AND d.excluido_em IS NULL) AS tem_decisor_externo, EXTRACT(DAY FROM NOW() - obras.validacao_obra_at)::INTEGER AS dias_desde_validacao, COALESCE(obras.validacao_manual_status, ufv.existencia_status) AS url_validacao_status, obras.obra_listada_na_fonte AS obra_listada_na_fonte, obras.obra_dados_mudaram_at AS obra_dados_mudaram_at FROM obras LEFT JOIN urls_fonte_validacao ufv ON ufv.url_fonte = obras.url_fonte WHERE obras.id=%s",(oid,)); obra=cur.fetchone()
    if not obra: conn.close(); raise HTTPException(404,"Não encontrada.")
    desbl=False
    if u:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM interacoes WHERE prestador_id=%s AND obra_id=%s AND tipo='DESBLOQUEIO'",(u["sub"],oid))
            desbl=cur.fetchone() is not None
        try:
            with conn.cursor() as cur: cur.execute("INSERT INTO interacoes (obra_id,prestador_id,tipo,plano_momento) VALUES (%s,%s,'VISUALIZACAO',%s) ON CONFLICT DO NOTHING",(oid,u["sub"],plano))
            conn.commit()
        except: conn.rollback()
    conn.close()
    return filtrar_obra(dict(obra),plano,desbl)


@app.get("/api/obras/{oid}/canais-cadastro")
async def get_canais_cadastro_obra(oid: str):
    """Retorna canal(is) de cadastro vinculado(s) à obra. Match em cascata: CNPJ → nome exato → fuzzy → URL."""
    _validar_uuid(oid)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, nome, cnpj, empresa, canal_cadastro_url,
                       familias_fornecimento, fornecedor_principal
                FROM obras WHERE id=%s
            """, (oid,))
            obra = cur.fetchone()
        if not obra:
            raise HTTPException(404, "Obra não encontrada")

        canal = None
        match_strategy = None
        match_score = None
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if obra["cnpj"]:
                cur.execute("""
                    SELECT * FROM canais_cadastro_empresa
                    WHERE empresa_cnpj=%s AND ativo=TRUE LIMIT 1
                """, (obra["cnpj"],))
                canal = cur.fetchone()
                if canal:
                    match_strategy = "cnpj"
                    match_score = 1.0
            if not canal and obra["empresa"]:
                cur.execute("""
                    SELECT * FROM canais_cadastro_empresa
                    WHERE LOWER(empresa_nome)=LOWER(%s) AND ativo=TRUE LIMIT 1
                """, (obra["empresa"],))
                canal = cur.fetchone()
                if canal:
                    match_strategy = "nome_exato"
                    match_score = 1.0
            if not canal and obra["empresa"]:
                cur.execute("""
                    SELECT * FROM canais_cadastro_empresa
                    WHERE ativo=TRUE
                      AND LENGTH(empresa_nome) >= 4
                      AND (
                          unaccent(lower(%s)) LIKE '%%' || unaccent(lower(empresa_nome)) || '%%'
                          OR unaccent(lower(empresa_nome)) LIKE '%%' || unaccent(lower(%s)) || '%%'
                      )
                    ORDER BY LENGTH(empresa_nome) DESC
                    LIMIT 1
                """, (obra["empresa"], obra["empresa"]))
                canal = cur.fetchone()
                if canal:
                    match_strategy = "nome_ilike"
                    match_score = 0.8
            if not canal and obra["empresa"]:
                cur.execute("""
                    SELECT *, similarity(empresa_nome, %s) AS sim
                    FROM canais_cadastro_empresa
                    WHERE ativo=TRUE AND similarity(empresa_nome, %s) >= 0.35
                    ORDER BY sim DESC LIMIT 1
                """, (obra["empresa"], obra["empresa"]))
                canal = cur.fetchone()
                if canal:
                    match_strategy = "nome_fuzzy"
                    match_score = float(canal["sim"])
            if not canal and obra["canal_cadastro_url"]:
                cur.execute("""
                    SELECT * FROM canais_cadastro_empresa
                    WHERE canal_url=%s AND ativo=TRUE LIMIT 1
                """, (obra["canal_cadastro_url"],))
                canal = cur.fetchone()
                if canal:
                    match_strategy = "url_obra"
                    match_score = 1.0
    finally:
        conn.close()

    def classificar_tipologia(c):
        if not c:
            return None
        url = (c.get("canal_url") or "").lower()
        if "potassiodobrasil" in url:
            return {"emoji": "🟢", "label": "Auto-aberto e gratuito", "fricao": "baixa"}
        if "fornecedor.arcelormittal" in url or "canalfornecedor.petrobras" in url:
            return {"emoji": "🟢", "label": "Auto-aberto", "fricao": "baixa"}
        if "portaldofornecedor.suzano" in url or "mrn.com.br" in url:
            return {"emoji": "🟢", "label": "Auto-aberto + homologação", "fricao": "média"}
        if "gsrp.midas" in url:
            return {"emoji": "🟡", "label": "Pré-cadastro + convite", "fricao": "média"}
        if any(x in url for x in ["yarabrasil", "mosaicco", "virtual360"]):
            return {"emoji": "🟡", "label": "Por convite após identificação", "fricao": "alta"}
        if "csn.com.br" in url:
            return {"emoji": "🔴", "label": "Via SAP Ariba", "fricao": "alta"}
        if "cedromineracao" in url:
            return {"emoji": "🟠", "label": "Sem portal público (canal comercial)", "fricao": "alta"}
        return {"emoji": "⚪", "label": "Não classificado", "fricao": "desconhecida"}

    return {
        "obra_id": str(obra["id"]),
        "obra_nome": obra["nome"],
        "encontrado": canal is not None,
        "match_strategy": match_strategy,
        "match_score": match_score,
        "canal": {
            "empresa_nome": canal["empresa_nome"],
            "nome_canal": canal["nome_canal"],
            "canal_url": canal["canal_url"],
            "familias_disponiveis": canal["familias_disponiveis"],
            "requer_pre_qualificacao": canal["requer_pre_qualificacao"],
            "requer_crc": canal["requer_crc"],
            "aceita_estrangeiros": canal["aceita_estrangeiros"],
            "notas": canal["notas"],
            "tipologia": classificar_tipologia(canal),
        } if canal else None,
        "fornecedor_principal_obra": obra["fornecedor_principal"],
        "familias_fornecimento_obra": obra["familias_fornecimento"],
    }


def _normalizar_cnpj(cnpj: Optional[str]) -> str:
    return re.sub(r'\D', '', cnpj or '')


def _socios_de_payload(payload: dict) -> list:
    """Extrai sócios (qsa) do payload BrasilAPI em formato simplificado."""
    qsa = payload.get("qsa") or []
    out = []
    for s in qsa:
        out.append({
            "nome": s.get("nome_socio") or s.get("nome"),
            "qualificacao": s.get("qualificacao_socio") or s.get("qualificacao"),
            "cargo": s.get("qualificacao_socio") or s.get("qualificacao"),
            "data_entrada": s.get("data_entrada_sociedade") or s.get("data_entrada"),
        })
    return out


@app.get("/api/obras/{oid}/decisores-auto")
async def get_decisores_auto_obra(oid: str):
    """Retorna decisores via BrasilAPI (sócios) com cache 12 meses em decisores_cache.

    Endpoint NOVO complementar a /api/obras/{oid}/decisores (esse último retorna
    decisores curados manualmente em decisores_obra). Hunter (emails) só é buscado
    pelo endpoint /enriquecer (admin). Aqui retornamos o que estiver no cache.
    """
    _validar_uuid(oid)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, nome, cnpj, empresa FROM obras WHERE id=%s", (oid,))
            obra = cur.fetchone()
        if not obra:
            raise HTTPException(404, "Obra não encontrada")

        cnpj_raw = obra.get("cnpj") or ""
        cnpj_clean = _normalizar_cnpj(cnpj_raw)
        if len(cnpj_clean) != 14:
            return {
                "obra_id": str(obra["id"]),
                "obra_nome": obra["nome"],
                "cnpj": None,
                "encontrado": False,
                "fonte": "vazio",
                "motivo": "Obra sem CNPJ válido cadastrado.",
            }

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT cnpj, empresa_nome, razao_social, nome_fantasia, status_cnpj,
                       socios, cnpj_atualizado_em, cnpj_erro,
                       dominio, emails, hunter_atualizado_em,
                       expira_em, forcado_refresh
                FROM decisores_cache WHERE cnpj=%s
            """, (cnpj_clean,))
            cache = cur.fetchone()

        cache_valido = (
            cache is not None
            and cache["expira_em"] is not None
            and cache["expira_em"] > datetime.utcnow()
            and not cache["forcado_refresh"]
            and cache["cnpj_atualizado_em"] is not None
        )

        fonte = "cache"
        if not cache_valido:
            fonte = "brasilapi"
            payload = consultar_cnpj(cnpj_clean)
            if payload:
                socios = _socios_de_payload(payload)
                # Popular decisores_cache.dominio a partir de empresa_dominios (mestre)
                # antes do UPSERT — assim Hunter pode rodar sem precisar inferir depois.
                dominio_mestre = None
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "SELECT dominio, holding_dominio FROM empresa_dominios WHERE cnpj=%s",
                        (cnpj_clean,),
                    )
                    ed = cur.fetchone()
                if ed:
                    dominio_mestre = ed.get("dominio") or ed.get("holding_dominio")
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO decisores_cache (
                            cnpj, empresa_nome, razao_social, nome_fantasia,
                            status_cnpj, socios, dominio,
                            cnpj_atualizado_em, cnpj_erro,
                            expira_em, forcado_refresh
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NULL,
                                  NOW() + INTERVAL '12 months', FALSE)
                        ON CONFLICT (cnpj) DO UPDATE SET
                            empresa_nome = EXCLUDED.empresa_nome,
                            razao_social = EXCLUDED.razao_social,
                            nome_fantasia = EXCLUDED.nome_fantasia,
                            status_cnpj = EXCLUDED.status_cnpj,
                            socios = EXCLUDED.socios,
                            dominio = COALESCE(EXCLUDED.dominio, decisores_cache.dominio),
                            cnpj_atualizado_em = NOW(),
                            cnpj_erro = NULL,
                            expira_em = NOW() + INTERVAL '12 months',
                            forcado_refresh = FALSE
                    """, (
                        cnpj_clean,
                        obra.get("empresa") or payload.get("razao_social"),
                        payload.get("razao_social"),
                        payload.get("nome_fantasia"),
                        payload.get("descricao_situacao_cadastral"),
                        psycopg2.extras.Json(socios),
                        dominio_mestre,
                    ))
                    conn.commit()
            else:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO decisores_cache (
                            cnpj, empresa_nome, cnpj_atualizado_em, cnpj_erro,
                            expira_em
                        ) VALUES (%s, %s, NOW(),
                                  'BrasilAPI retornou vazio ou erro',
                                  NOW() + INTERVAL '7 days')
                        ON CONFLICT (cnpj) DO UPDATE SET
                            cnpj_atualizado_em = NOW(),
                            cnpj_erro = 'BrasilAPI retornou vazio ou erro',
                            expira_em = NOW() + INTERVAL '7 days'
                    """, (cnpj_clean, obra.get("empresa")))
                    conn.commit()

            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT cnpj, empresa_nome, razao_social, nome_fantasia, status_cnpj,
                           socios, cnpj_atualizado_em, cnpj_erro,
                           dominio, emails, hunter_atualizado_em,
                           expira_em, forcado_refresh
                    FROM decisores_cache WHERE cnpj=%s
                """, (cnpj_clean,))
                cache = cur.fetchone()
    finally:
        conn.close()

    if cache is None or cache.get("cnpj_erro"):
        return {
            "obra_id": str(obra["id"]),
            "obra_nome": obra["nome"],
            "cnpj": cnpj_clean,
            "encontrado": False,
            "fonte": fonte,
            "motivo": (cache or {}).get("cnpj_erro") or "Sem dados disponíveis.",
        }

    return {
        "obra_id": str(obra["id"]),
        "obra_nome": obra["nome"],
        "cnpj": cache["cnpj"],
        "encontrado": True,
        "fonte": fonte,
        "razao_social": cache["razao_social"],
        "nome_fantasia": cache["nome_fantasia"],
        "status_cnpj": cache["status_cnpj"],
        "socios": cache["socios"] or [],
        "tem_dados_hunter": cache["hunter_atualizado_em"] is not None,
        "emails_hunter": cache["emails"] or [],
        "dominio_hunter": cache["dominio"],
        "cache_expira_em": cache["expira_em"].isoformat() if cache["expira_em"] else None,
        "ultima_atualizacao": cache["cnpj_atualizado_em"].isoformat() if cache["cnpj_atualizado_em"] else None,
    }


def _inferir_dominio_de_obra(obra: dict, cache_row: Optional[dict]) -> Optional[str]:
    """Tenta inferir domínio em ordem:
       1. empresa_dominios — fonte autoritativa (curada manualmente); só status validados.
       2. cache existente (decisores_cache.dominio) — Hunter já rodou.
       3. URL da obra (url_fonte / canal_cadastro_url) — fallback fraco; ignora gov.br.
    Retorna None se não conseguir inferir com confiança.
    """
    _STATUS_VALIDOS = ("ok", "validado", "validado_holding", "validado_rebrand_em_curso")

    cnpj_clean = _normalizar_cnpj(obra.get("cnpj"))
    if len(cnpj_clean) == 14:
        conn_ed = get_conn()
        try:
            with conn_ed.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT dominio, holding_dominio, dominio_status "
                    "FROM empresa_dominios WHERE cnpj=%s",
                    (cnpj_clean,),
                )
                ed = cur.fetchone()
        finally:
            conn_ed.close()
        if ed and ed.get("dominio_status") in _STATUS_VALIDOS:
            if ed.get("dominio"):
                return ed["dominio"]
            if ed.get("holding_dominio"):
                return ed["holding_dominio"]

    if cache_row and cache_row.get("dominio"):
        return cache_row["dominio"]

    url = (obra.get("url_fonte") or obra.get("canal_cadastro_url") or "").strip()
    if url:
        m = re.search(r"https?://(?:www\.)?([^/\s]+)", url, flags=re.IGNORECASE)
        if m:
            host = m.group(1).lower().strip("/")
            if host and "." in host and "gov.br" not in host and "dados.gov" not in host:
                return host
    return None


@app.post("/api/obras/{oid}/decisores-auto/enriquecer")
async def enriquecer_decisores_auto(
    oid: str,
    modo: str = Query("A", regex="^[AC]$"),
    body: Optional[dict] = Body(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Enriquece decisores_cache via Hunter.io. Requer X-Admin-Token.

    Query param `?modo=A` (padrão): Hunter padrão (todos cargos) + filtro client-side por cargos do Anderson.
    Query param `?modo=C`: BrasilAPI sócios filtrados + Hunter ?department=management filtrado client-side.

    Body opcional: {"dominio": "exemplo.com.br"}.
    """
    _check_admin_token(x_admin_token)
    _validar_uuid(oid)

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, nome, cnpj, empresa, url_fonte, canal_cadastro_url, cnpj_status
                FROM obras WHERE id=%s
            """, (oid,))
            obra = cur.fetchone()
        if not obra:
            raise HTTPException(404, "Obra não encontrada")

        if obra.get("cnpj_status") == "hallucinated":
            return {
                "status": "skipped",
                "obra_id": str(obra["id"]),
                "motivo": "CNPJ inválido (hallucinated) — aguardando correção via captador proper",
            }

        cnpj_clean = _normalizar_cnpj(obra.get("cnpj"))
        if len(cnpj_clean) != 14:
            raise HTTPException(400, "Obra sem CNPJ válido cadastrado.")

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM decisores_cache WHERE cnpj=%s", (cnpj_clean,))
            cache_row = cur.fetchone()

        dominio = None
        if body and isinstance(body, dict) and body.get("dominio"):
            dominio = str(body["dominio"]).strip()
        else:
            dominio = _inferir_dominio_de_obra(dict(obra), dict(cache_row) if cache_row else None)

        if not dominio:
            raise HTTPException(400, "Domínio não pôde ser inferido. Envie body {\"dominio\": \"...\"}.")

        if modo == "A":
            resultado = buscar_emails_dominio(dominio)
        else:
            resultado = buscar_emails_management(dominio)

        if "erro" in resultado:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO decisores_cache (cnpj, empresa_nome, dominio, hunter_atualizado_em, hunter_erro)
                    VALUES (%s, %s, %s, NOW(), %s)
                    ON CONFLICT (cnpj) DO UPDATE SET
                        dominio = EXCLUDED.dominio,
                        hunter_atualizado_em = NOW(),
                        hunter_erro = EXCLUDED.hunter_erro
                """, (cnpj_clean, obra.get("empresa"), dominio, resultado["erro"]))
                conn.commit()
            raise HTTPException(502, f"Hunter.io ({modo}): {resultado['erro']}")

        emails_brutos = resultado.get("emails") or []
        emails_match, emails_descartados, palavras_emails = filtrar_por_cargo_decisor(
            emails_brutos, campo_cargo="position"
        )
        creditos = int(resultado.get("creditos_usados") or 1)

        socios_match: list = []
        socios_descartados_count = 0
        socios_total = 0
        if modo == "C":
            socios_raw = (cache_row or {}).get("socios") or []
            socios_total = len(socios_raw)
            s_match, s_descartados, _ = filtrar_por_cargo_decisor(
                socios_raw, campo_cargo="qualificacao"
            )
            socios_match = s_match
            socios_descartados_count = len(s_descartados)

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO decisores_cache (
                    cnpj, empresa_nome, dominio, emails,
                    hunter_atualizado_em, hunter_creditos_usados, hunter_erro
                ) VALUES (%s, %s, %s, %s, NOW(), %s, NULL)
                ON CONFLICT (cnpj) DO UPDATE SET
                    dominio = EXCLUDED.dominio,
                    emails = EXCLUDED.emails,
                    hunter_atualizado_em = NOW(),
                    hunter_creditos_usados = COALESCE(decisores_cache.hunter_creditos_usados, 0) + EXCLUDED.hunter_creditos_usados,
                    hunter_erro = NULL
            """, (
                cnpj_clean,
                obra.get("empresa"),
                resultado.get("dominio_pesquisado") or dominio,
                psycopg2.extras.Json(emails_brutos),
                creditos,
            ))
            conn.commit()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT hunter_atualizado_em, hunter_creditos_usados, dominio
                FROM decisores_cache WHERE cnpj=%s
            """, (cnpj_clean,))
            atual = cur.fetchone()
    finally:
        conn.close()

    if modo == "A":
        return {
            "modo": "A",
            "tecnica": "Hunter padrão + filtro client-side",
            "obra_id": str(obra["id"]),
            "cnpj": cnpj_clean,
            "dominio": atual["dominio"],
            "emails_brutos_count": len(emails_brutos),
            "emails_match_count": len(emails_match),
            "emails_match": emails_match,
            "emails_descartados": [d.get("position") for d in emails_descartados],
            "palavras_match": palavras_emails,
            "total_emails_no_dominio": resultado.get("total_emails_no_dominio"),
            "creditos_usados_total": atual["hunter_creditos_usados"],
            "atualizado_em": atual["hunter_atualizado_em"].isoformat() if atual["hunter_atualizado_em"] else None,
        }
    return {
        "modo": "C",
        "tecnica": "BrasilAPI sócios + Hunter management + filtro client-side",
        "obra_id": str(obra["id"]),
        "cnpj": cnpj_clean,
        "dominio": atual["dominio"],
        "socios_count": socios_total,
        "socios_match_count": len(socios_match),
        "socios_descartados_count": socios_descartados_count,
        "socios_match": socios_match,
        "emails_brutos_count": len(emails_brutos),
        "emails_match_count": len(emails_match),
        "emails_match": emails_match,
        "emails_descartados": [d.get("position") for d in emails_descartados],
        "palavras_match": palavras_emails,
        "total_decisores": len(socios_match) + len(emails_match),
        "total_emails_no_dominio": resultado.get("total_emails_no_dominio"),
        "creditos_usados_total": atual["hunter_creditos_usados"],
        "atualizado_em": atual["hunter_atualizado_em"].isoformat() if atual["hunter_atualizado_em"] else None,
    }


@app.post("/api/admin/decisores-auto/refresh")
async def refresh_decisores_auto(
    body: dict = Body(...),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Invalida cache de um CNPJ específico em decisores_cache. Requer X-Admin-Token.

    Body: {"cnpj": "33000167000101"} (com ou sem formatação).
    Próxima leitura via GET /decisores-auto vai re-buscar BrasilAPI.
    """
    _check_admin_token(x_admin_token)

    if not isinstance(body, dict) or not body.get("cnpj"):
        raise HTTPException(400, "Body inválido. Esperado {\"cnpj\": \"...\"}.")

    cnpj_clean = _normalizar_cnpj(body.get("cnpj"))
    if len(cnpj_clean) != 14:
        raise HTTPException(400, "CNPJ inválido (precisa ter 14 dígitos).")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE decisores_cache
                SET expira_em = NOW(),
                    forcado_refresh = TRUE
                WHERE cnpj = %s
            """, (cnpj_clean,))
            linhas = cur.rowcount
            conn.commit()
    finally:
        conn.close()

    if linhas == 0:
        raise HTTPException(404, f"CNPJ {cnpj_clean} não está em decisores_cache.")

    return {"invalidado": True, "cnpj": cnpj_clean, "linhas_afetadas": linhas}


# NOTE: /api/obras/{oid}/desbloquear v2 está definido na seção "Sistema de desbloqueio"
# (no fim do arquivo, antes do catch-all). Substitui o fluxo antigo de créditos.

@app.post("/api/admin/obras")
async def criar_obra(req: ObraReq, u=Depends(requer_auth)):
    conn=get_conn(); vf=fmt_valor(req.valor_estimado) if req.valor_estimado else None
    with conn.cursor() as cur:
        cur.execute("INSERT INTO obras (nome,empresa,setor,municipio,uf,valor_estimado,valor_formatado,fase,descricao,nivel1_nome,nivel1_cargo,nivel1_email,nivel2_nome,nivel2_cargo,nivel2_email,urgencia,lead_score,necessidades,fonte) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'manual') RETURNING id",
            (req.nome,req.empresa,req.setor,req.municipio,req.uf,req.valor_estimado,vf,req.fase,req.descricao,req.nivel1_nome,req.nivel1_cargo,req.nivel1_email,req.nivel2_nome,req.nivel2_cargo,req.nivel2_email,calc_urgencia(req.fase),calc_score(req.fase,req.valor_estimado),detectar_nec(req.setor,req.descricao or "")))
        nid=cur.fetchone()[0]
    conn.commit(); conn.close()
    return {"id":str(nid),"mensagem":"Obra criada."}

@app.post("/api/cron/captar")
async def captar(request: Request, bt: BackgroundTasks):
    if request.headers.get("Authorization")!=f"Bearer {CRON_SECRET}": raise HTTPException(401,"Não autorizado.")
    async def run():
        for fonte,url,rid in [("IBAMA","https://dados.gov.br/api/3/action/datastore_search","ede897ad-0e7c-4096-a97e-d5c50b37ed4c"),("BNDES","https://dados.gov.br/api/3/action/datastore_search","7f6c4cd1-1bdc-4900-a9e1-d3dcf1afe86a")]:
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    r=await c.get(url,params={"resource_id":rid,"limit":50,"sort":"_id desc"})
                recs=r.json().get("result",{}).get("records",[]) if r.status_code==200 else []
                novos=0; conn=get_conn()
                for rec in recs:
                    nome=str(rec.get("NomeEmpreendimento") or rec.get("Empreendimento") or "").strip()
                    if not nome: continue
                    id_ext=f"{fonte}-{re.sub(chr(92)+'D','',str(rec.get('NumProcesso') or rec.get('Numero_Operacao') or nome))[:20]}"
                    try:
                        with conn.cursor() as cur:
                            cur.execute("SELECT 1 FROM obras WHERE id_externo=%s",(id_ext,))
                            if cur.fetchone(): continue
                            setor=inferir_setor(nome,""); fase="LICENCA_PREVIA" if fonte=="IBAMA" else "FINANCIAMENTO_BNDES"
                            val_s=str(rec.get("Valor_Aprovado") or "0").replace(",",".")
                            try: val=float(re.sub(r"[^\d.]",val_s,""))
                            except: val=None
                            cur.execute("INSERT INTO obras (id_externo,nome,setor,fase,fonte,urgencia,lead_score,necessidades) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                (id_ext,nome,setor,fase,fonte,calc_urgencia(fase),calc_score(fase,val),detectar_nec(setor,"")))
                        conn.commit(); novos+=1
                    except: conn.rollback()
                conn.close(); log.info("Captação %s: %d novos",fonte,novos)
            except Exception as e: log.error("Erro captação %s: %s",fonte,e)
    bt.add_task(run)
    return {"mensagem":"Captação iniciada."}

@app.get("/api/stats")
async def stats():
    conn=get_conn()
    with conn.cursor() as cur:
        # anp_pte: agregados macro ANP-PTE sem decisor/CNPJ por linha, distorcem totais (~R$60T)
        cur.execute("SELECT COUNT(*) FROM obras WHERE (visivel IS NULL OR visivel = true) AND COALESCE(fonte,'') != 'anp_pte'"); obras=cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM prestadores"); prest=cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM interacoes WHERE tipo='DESBLOQUEIO'"); desbl=cur.fetchone()[0]
        cur.execute("SELECT fase,COUNT(*) FROM obras WHERE (visivel IS NULL OR visivel = true) AND COALESCE(fonte,'') != 'anp_pte' GROUP BY fase ORDER BY COUNT(*) DESC"); por_fase=cur.fetchall()
        cur.execute("SELECT uf,COUNT(*) FROM obras WHERE (visivel IS NULL OR visivel = true) AND uf IS NOT NULL AND COALESCE(fonte,'') != 'anp_pte' GROUP BY uf ORDER BY COUNT(*) DESC LIMIT 10"); por_uf=cur.fetchall()
    conn.close()
    return {"obras":obras,"prestadores":prest,"desbloqueios":desbl,"por_fase":dict(por_fase),"top_ufs":dict(por_uf)}

@app.get("/login")
async def login_page():
    with open("/app/frontend/login.html") as f: html = f.read()
    return HTMLResponse(html)

@app.get("/esqueci")
async def esqueci_page():
    with open("/app/frontend/esqueci.html") as f: html = f.read()
    return HTMLResponse(html)

@app.get("/reset")
async def reset_page():
    with open("/app/frontend/reset.html") as f: html = f.read()
    return HTMLResponse(html)

@app.get("/score")
async def score_page():
    with open("/app/frontend/score.html") as f: html = f.read()
    return HTMLResponse(html)

@app.get("/api/dashboard/ouro_count")
async def ouro_count():
    """Contagem OURO no site = classificacao_computed='OURO' (capex >= R$500mi).

    Independente de decisor (regra canônica). Não confundir com OURO_DECISOR_SQL,
    que filtra adicionalmente por decisor validado (uso interno em outros endpoints).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # ═══════════════════════════════════════════════════════════════
            # REGRA IMUTÁVEL — NÃO ALTERAR SEM APROVAÇÃO EXPLÍCITA DO WILLIAM
            # OURO  = classificacao_computed = 'OURO'  (capex >= R$ 500 milhões)
            # PRATA = classificacao_computed = 'PRATA' (capex >= R$ 50 milhões e < R$ 500 milhões)
            # Contagem usa COUNT(*) direto — SEM filtro de email, decisor ou qualquer outro campo.
            # Os filtros de prospecção (OURO_DECISOR_SQL, PRATA_MATCH_SQL) existem SEPARADOS
            # e só são usados nos endpoints de matches/times — NUNCA nos contadores do hero/dashboard.
            # Alterações aqui quebram os números públicos do site. Discutir antes de mexer.
            # ═══════════════════════════════════════════════════════════════
            cur.execute("""
                SELECT COUNT(*) FROM obras
                WHERE classificacao_computed = 'OURO'
            """)
            count = cur.fetchone()[0]
    finally:
        conn.close()
    return {"count": count}

_matches_ouro_cache: dict = {}
_MATCHES_OURO_TTL = 600  # 10min — uniformizado com stats-public (sprint perf 21/05)

# ═══════════════════════════════════════════════════════════════
# LEGACY INTENCIONAL — workflow Mari/enriquecimento (audit 21/05)
# Filtro semântico: "obra com decisor preenchido + contato verificado"
# (nivel1_nome + nivel1_email/linkedin + cargo_decisor_keyword).
# NÃO migrar para classificacao_computed='OURO': o TIER canônico filtra
# por CAPEX (≥R$500mi), não por dados de decisor. Endpoints têm consumidores
# de workflow Mari/agente remoto que dependem desse universo específico.
# ═══════════════════════════════════════════════════════════════
@app.get("/api/dashboard/matches_ouro")
async def dashboard_matches_ouro(setor: Optional[str] = None, uf: Optional[str] = None):
    """KPIs de matchmaking por obra-ouro pra alimentar cards da aba Inteligência de Match.
    Filtros opcionais: setor, uf. Sem auth (cobertura agregada não revela contatos)."""
    cache_key = (setor or "", uf or "")
    now_cache = time.time()
    cached = _matches_ouro_cache.get(cache_key)
    if cached and (now_cache - cached["ts"]) < _MATCHES_OURO_TTL:
        return cached["data"]

    cond_extra = ""
    params = []
    if setor:
        cond_extra += " AND o.setor = %s"
        params.append(setor)
    if uf:
        cond_extra += " AND o.uf = %s"
        params.append(uf)

    sql = f"""
        WITH ouro AS (
            SELECT id, nome, empresa, uf, setor, valor_estimado, valor_formatado, lead_score
            FROM obras o
            WHERE o.nivel1_nome IS NOT NULL AND o.nivel1_nome != ''
              AND (COALESCE(o.nivel1_email,'') != '' OR COALESCE(o.nivel1_linkedin,'') != '')
              AND cargo_decisor_keyword(o.nivel1_cargo)
              AND COALESCE(o.fonte_tipo,'OFICIAL') != 'NOTICIA'
              AND (o.visivel IS NULL OR o.visivel = TRUE)
              {cond_extra}
        ),
        agregado AS (
            SELECT m.obra_id,
                   COUNT(*)                            AS total_matches,
                   COUNT(DISTINCT m.cnpj)              AS prestadores_unicos,
                   ROUND(AVG(m.score)::numeric, 0)::int AS score_medio,
                   MAX(m.score)::int                   AS score_top
            FROM matches_obra_prestador m
            WHERE m.obra_id IN (SELECT id FROM ouro)
            GROUP BY m.obra_id
        ),
        top_cat AS (
            SELECT DISTINCT ON (obra_id) obra_id, cat_nome
            FROM (
                SELECT m.obra_id, c.nome AS cat_nome, c.ordem AS cat_ordem,
                       COUNT(*) AS qtd
                FROM matches_obra_prestador m
                JOIN categorias_servico c ON c.id = m.categoria_id
                WHERE m.obra_id IN (SELECT id FROM ouro)
                GROUP BY m.obra_id, c.nome, c.ordem
            ) sub
            ORDER BY obra_id, qtd DESC, cat_ordem
        ),
        top_forn AS (
            SELECT DISTINCT ON (obra_id) obra_id, cnpj, nome_fornecedor
            FROM (
                SELECT m.obra_id, m.cnpj, MAX(m.score) AS score,
                       COALESCE(NULLIF(e.nome_fantasia,''), e.razao_social) AS nome_fornecedor
                FROM matches_obra_prestador m
                LEFT JOIN fornecedores e ON e.cnpj = m.cnpj
                WHERE m.obra_id IN (SELECT id FROM ouro)
                GROUP BY m.obra_id, m.cnpj, e.nome_fantasia, e.razao_social
            ) sub
            ORDER BY obra_id, score DESC, cnpj
        )
        SELECT o.id::text AS obra_id, o.nome, o.empresa, o.uf, o.setor,
               o.valor_estimado, o.valor_formatado, o.lead_score,
               COALESCE(a.total_matches, 0)      AS total_matches,
               COALESCE(a.prestadores_unicos, 0) AS prestadores_unicos,
               COALESCE(a.score_medio, 0)        AS score_medio,
               COALESCE(a.score_top, 0)          AS score_top,
               tc.cat_nome                       AS top_categoria,
               tf.nome_fornecedor                AS top_fornecedor_nome,
               tf.cnpj                           AS top_fornecedor_cnpj
        FROM ouro o
        LEFT JOIN agregado a  ON a.obra_id = o.id
        LEFT JOIN top_cat  tc ON tc.obra_id = o.id
        LEFT JOIN top_forn tf ON tf.obra_id = o.id
        ORDER BY a.total_matches DESC NULLS LAST, o.lead_score DESC NULLS LAST
    """

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    for r in rows:
        if r.get("valor_estimado") is not None:
            r["valor_estimado"] = float(r["valor_estimado"])

    setores = sorted({r["setor"] for r in rows if r.get("setor")})
    ufs = sorted({r["uf"] for r in rows if r.get("uf")})
    result = {"total": len(rows), "obras": rows, "setores": setores, "ufs": ufs}
    _matches_ouro_cache[cache_key] = {"data": result, "ts": now_cache}
    return result


_times_ouro_cache: dict = {}
_TIMES_OURO_TTL = 600  # 10min — uniformizado (sprint perf 21/05)

# ═══════════════════════════════════════════════════════════════
# LEGACY INTENCIONAL — workflow Mari/enriquecimento (audit 21/05)
# Filtro semântico: "obra com decisor preenchido + contato verificado"
# (nivel1_nome + nivel1_email/linkedin + cargo_decisor_keyword).
# NÃO migrar para classificacao_computed='OURO': o TIER canônico filtra
# por CAPEX (≥R$500mi), não por dados de decisor. Endpoints têm consumidores
# de workflow Mari/agente remoto que dependem desse universo específico.
# ═══════════════════════════════════════════════════════════════
@app.get("/api/dashboard/times_ouro")
async def dashboard_times_ouro(setor: Optional[str] = None, uf: Optional[str] = None):
    """Times sugeridos por obra-ouro: top 5 fornecedores distintos com especialidade.

    Mesmo universo de obras-ouro que /matches_ouro. Para cada obra, deduplica
    fornecedores por CNPJ (pegando a categoria de melhor score) e retorna os 5
    melhores como mini-cards. Fee min/max derivado de valor_estimado (0,5%–1%).
    """
    cache_key = (setor or "", uf or "")
    now_cache = time.time()
    cached = _times_ouro_cache.get(cache_key)
    if cached and (now_cache - cached["ts"]) < _TIMES_OURO_TTL:
        return cached["data"]

    cond_extra = ""
    params = []
    if setor:
        cond_extra += " AND o.setor = %s"
        params.append(setor)
    if uf:
        cond_extra += " AND o.uf = %s"
        params.append(uf)

    sql = f"""
        WITH ouro AS (
            SELECT id, nome, empresa, uf, setor, fase,
                   valor_estimado, valor_formatado, lead_score
            FROM obras o
            WHERE o.nivel1_nome IS NOT NULL AND o.nivel1_nome != ''
              AND (COALESCE(o.nivel1_email,'') != '' OR COALESCE(o.nivel1_linkedin,'') != '')
              AND cargo_decisor_keyword(o.nivel1_cargo)
              AND COALESCE(o.fonte_tipo,'OFICIAL') != 'NOTICIA'
              AND (o.visivel IS NULL OR o.visivel = TRUE)
              {cond_extra}
        ),
        melhor_por_par AS (
            -- Para cada (obra, cnpj), pega categoria de maior score
            SELECT DISTINCT ON (m.obra_id, m.cnpj)
                   m.obra_id, m.cnpj,
                   COALESCE(NULLIF(e.nome_fantasia,''), e.razao_social) AS nome_fornecedor,
                   c.nome AS especialidade,
                   m.score
            FROM matches_obra_prestador m
            LEFT JOIN fornecedores e ON e.cnpj = m.cnpj
            LEFT JOIN categorias_servico c ON c.id = m.categoria_id
            WHERE m.obra_id IN (SELECT id FROM ouro)
            ORDER BY m.obra_id, m.cnpj, m.score DESC NULLS LAST, m.ranking
        ),
        ranked AS (
            SELECT obra_id, cnpj, nome_fornecedor, especialidade, score,
                   ROW_NUMBER() OVER (
                       PARTITION BY obra_id
                       ORDER BY score DESC NULLS LAST, cnpj
                   ) AS rn
            FROM melhor_por_par
        ),
        top5 AS (
            SELECT obra_id,
                   json_agg(
                       json_build_object(
                           'cnpj', cnpj,
                           'nome', COALESCE(nome_fornecedor, '—'),
                           'score', score::int,
                           'especialidade', COALESCE(especialidade, '—')
                       )
                       ORDER BY rn
                   ) FILTER (WHERE rn <= 5) AS top_fornecedores
            FROM ranked
            GROUP BY obra_id
        ),
        agregado AS (
            SELECT obra_id, COUNT(*) AS total_membros
            FROM melhor_por_par
            GROUP BY obra_id
        )
        SELECT o.id::text AS obra_id,
               o.nome, o.empresa, o.uf, o.setor, o.fase,
               o.valor_estimado, o.valor_formatado,
               o.lead_score AS score_time,
               COALESCE(t.top_fornecedores, '[]'::json) AS top_fornecedores,
               COALESCE(a.total_membros, 0) AS total_membros
        FROM ouro o
        LEFT JOIN top5     t ON t.obra_id = o.id
        LEFT JOIN agregado a ON a.obra_id = o.id
        WHERE COALESCE(a.total_membros, 0) > 0
        ORDER BY o.lead_score DESC NULLS LAST, a.total_membros DESC NULLS LAST
    """

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    for r in rows:
        ve = r.get("valor_estimado")
        if ve is not None:
            v = float(ve)
            r["valor_estimado"] = v
            r["fee_min"] = int(v * 0.005)
            r["fee_max"] = int(v * 0.010)
        else:
            r["fee_min"] = None
            r["fee_max"] = None

    setores = sorted({r["setor"] for r in rows if r.get("setor")})
    ufs = sorted({r["uf"] for r in rows if r.get("uf")})
    result = {"total": len(rows), "obras": rows, "setores": setores, "ufs": ufs}
    _times_ouro_cache[cache_key] = {"data": result, "ts": now_cache}
    return result


@app.get("/api/dashboard/prata_count")
async def prata_count():
    """Contagem PRATA no site = classificacao_computed='PRATA' (R$50mi <= capex < R$500mi).

    Independente de decisor (regra canônica).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # ═══════════════════════════════════════════════════════════════
            # REGRA IMUTÁVEL — NÃO ALTERAR SEM APROVAÇÃO EXPLÍCITA DO WILLIAM
            # OURO  = classificacao_computed = 'OURO'  (capex >= R$ 500 milhões)
            # PRATA = classificacao_computed = 'PRATA' (capex >= R$ 50 milhões e < R$ 500 milhões)
            # Contagem usa COUNT(*) direto — SEM filtro de email, decisor ou qualquer outro campo.
            # Os filtros de prospecção (OURO_DECISOR_SQL, PRATA_MATCH_SQL) existem SEPARADOS
            # e só são usados nos endpoints de matches/times — NUNCA nos contadores do hero/dashboard.
            # Alterações aqui quebram os números públicos do site. Discutir antes de mexer.
            # ═══════════════════════════════════════════════════════════════
            cur.execute("""
                SELECT COUNT(*) FROM obras
                WHERE classificacao_computed = 'PRATA'
            """)
            count = cur.fetchone()[0]
    finally:
        conn.close()
    return {"count": count}

@app.get("/api/dashboard/bronze_count")
async def bronze_count():
    """Contagem BRONZE no site = classificacao_computed='BRONZE'.

    Independente de decisor (regra canônica). Após cleanup 19/05 ~4.4k obras.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # ═══════════════════════════════════════════════════════════════
            # REGRA IMUTÁVEL — NÃO ALTERAR SEM APROVAÇÃO EXPLÍCITA DO WILLIAM
            # BRONZE = classificacao_computed = 'BRONZE'
            # Contagem usa COUNT(*) direto — SEM filtro de email, decisor ou qualquer outro campo.
            # ═══════════════════════════════════════════════════════════════
            cur.execute("""
                SELECT COUNT(*) FROM obras
                WHERE classificacao_computed = 'BRONZE'
            """)
            count = cur.fetchone()[0]
    finally:
        conn.close()
    return {"count": count}


@app.get("/api/dashboard/pipeline_count")
async def pipeline_count():
    """Obras-pipeline: EM_EXECUCAO + valor_estimado >= R$100M + sem decisor classificado em decisores_obra E sem nivel1_nome."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*) FROM obras
                WHERE (visivel IS NULL OR visivel = true)
                  AND COALESCE(fonte,'') != 'anp_pte'
                  AND {PIPELINE_SQL}
            """)
            count = cur.fetchone()[0]
    finally:
        conn.close()
    return {"count": count}


_stats_public_cache = {"data": None, "ts": 0.0}
_STATS_PUBLIC_TTL = 600  # 10 min — usado pelo hero da home

@app.get("/api/dashboard/stats-public")
async def stats_public():
    """Stats agregadas pra hero da home (sem auth). Cache 10min."""
    import time as _time
    now = _time.time()
    if _stats_public_cache["data"] is not None and (now - _stats_public_cache["ts"]) < _STATS_PUBLIC_TTL:
        return _stats_public_cache["data"]
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # ═══════════════════════════════════════════════════════════════
            # REGRA IMUTÁVEL — NÃO ALTERAR SEM APROVAÇÃO EXPLÍCITA DO WILLIAM
            # OURO  = classificacao_computed = 'OURO'  (capex >= R$ 500 milhões)
            # PRATA = classificacao_computed = 'PRATA' (capex >= R$ 50 milhões e < R$ 500 milhões)
            # Contagem usa COUNT(*) direto — SEM filtro de email, decisor ou qualquer outro campo.
            # Os filtros de prospecção (OURO_DECISOR_SQL, PRATA_MATCH_SQL) existem SEPARADOS
            # e só são usados nos endpoints de matches/times — NUNCA nos contadores do hero/dashboard.
            # Alterações aqui quebram os números públicos do site. Discutir antes de mexer.
            # ═══════════════════════════════════════════════════════════════
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE classificacao_computed='OURO') AS ouro,
                  COUNT(*) FILTER (WHERE classificacao_computed='PRATA') AS prata,
                  COUNT(*) FILTER (WHERE classificacao_computed='BRONZE') AS bronze,
                  COUNT(*) FILTER (WHERE classificacao_computed='PIPELINE') AS pipeline,
                  COALESCE(ROUND(SUM(valor_estimado) FILTER (
                    WHERE (visivel IS NULL OR visivel=true)
                      AND COALESCE(fonte,'') != 'anp_pte'
                      AND classificacao_computed IN ('OURO','PRATA','BRONZE','PIPELINE')
                  ) / 1e9)::int, 0) AS capex_total_bi
                FROM obras
            """)
            agg = dict(cur.fetchone())
            cur.execute("SELECT COUNT(*) AS total FROM fornecedores")
            forn = cur.fetchone()
            agg["fornecedores"] = forn["total"] if forn else 0
    finally:
        conn.close()
    _stats_public_cache["data"] = agg
    _stats_public_cache["ts"] = now
    return agg


_setores_public_cache = {"data": None, "ts": 0.0}
_SETORES_PUBLIC_TTL = 600  # 10 min — usado pelo gráfico de setores no hero

@app.get("/api/dashboard/setores-public")
async def setores_public():
    """Distribuição de obras por setor pra gráfico da landing (sem auth). Cache 10min.

    Critério: obras com classificacao_computed IN (OURO/PRATA/BRONZE/PIPELINE) e
    fonte_tipo != NOTICIA. Retorna TODOS os setores (sem top-N), ordenados desc.
    """
    import time as _time
    now = _time.time()
    if _setores_public_cache["data"] is not None and (now - _setores_public_cache["ts"]) < _SETORES_PUBLIC_TTL:
        return _setores_public_cache["data"]
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT COALESCE(NULLIF(setor, ''), 'OUTRO') AS setor, COUNT(*) AS total
                FROM obras
                WHERE classificacao_computed IN ('OURO','PRATA','BRONZE','PIPELINE')
                  AND COALESCE(fonte_tipo,'OFICIAL') != 'NOTICIA'
                GROUP BY 1
                ORDER BY total DESC
            """)
            rows = cur.fetchall()
            total_geral = sum(r["total"] for r in rows) or 1
            setores = [
                {
                    "setor": r["setor"],
                    "total": int(r["total"]),
                    "pct": round(100.0 * r["total"] / total_geral, 1),
                }
                for r in rows
            ]
    finally:
        conn.close()
    payload = {"setores": setores, "total": total_geral}
    _setores_public_cache["data"] = payload
    _setores_public_cache["ts"] = now
    return payload


@app.get("/api/grupos/{cnpj}")
async def grupos_por_cnpj(cnpj: str):
    """
    Retorna grupos empresariais e consórcios onde o CNPJ aparece, junto com
    irmãos em cada grupo, contagem como cliente (executor de obras) e como
    fornecedor (presença em matches). Sem auth.
    """
    # Normaliza CNPJ (aceita com pontuação ou limpo)
    import re as _re
    cnpj_norm = _re.sub(r"[./-]", "", (cnpj or "").strip())
    if len(cnpj_norm) != 14 or not cnpj_norm.isdigit():
        raise HTTPException(400, "CNPJ inválido (esperado 14 dígitos).")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT e.cnpj, e.razao_social, e.nome_fantasia, e.uf, e.municipio_nome,
                       e.porte, e.cnae_principal, e.situacao, e.capital_social,
                       fm.papel_wins_hub
                FROM fornecedores e
                LEFT JOIN fornecedor_meta fm ON fm.cnpj = e.cnpj
                WHERE e.cnpj = %s
            """, (cnpj_norm,))
            empresa = cur.fetchone()

            cur.execute("""
                SELECT g.id::text AS id, g.nome, g.tipo, cg.papel
                FROM cnpj_grupo cg
                JOIN grupo g ON g.id = cg.grupo_id
                WHERE cg.cnpj = %s
                ORDER BY g.tipo, g.nome
            """, (cnpj_norm,))
            grupos_rows = cur.fetchall()

            grupos = []
            for g in grupos_rows:
                cur.execute("""
                    SELECT cg.cnpj, cg.nome_fantasia, cg.papel,
                           e.razao_social, e.uf, e.municipio_nome
                    FROM cnpj_grupo cg
                    LEFT JOIN fornecedores e ON e.cnpj = cg.cnpj
                    WHERE cg.grupo_id = %s AND cg.cnpj <> %s
                    ORDER BY
                      CASE cg.papel
                        WHEN 'HOLDING_PAI' THEN 1 WHEN 'AMBOS' THEN 2
                        WHEN 'GRUPO_OPERACIONAL' THEN 3
                        WHEN 'CONSORCIO_LIDER' THEN 4 WHEN 'CONSORCIO_MEMBRO' THEN 5
                        ELSE 6
                      END, cg.nome_fantasia
                """, (g["id"], cnpj_norm))
                grupos.append({
                    "id": g["id"], "nome": g["nome"], "tipo": g["tipo"],
                    "papel": g["papel"],
                    "irmaos": [dict(r) for r in cur.fetchall()],
                })

            cur.execute("SELECT COUNT(*) AS n FROM obras WHERE cnpj = %s", (cnpj_norm,))
            obras_como_cliente = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM matches_obra_prestador WHERE cnpj = %s", (cnpj_norm,))
            matches_como_fornecedor = cur.fetchone()["n"]
    finally:
        conn.close()

    if not empresa and not grupos:
        raise HTTPException(404, "CNPJ não encontrado em fornecedores nem em grupos.")

    base = dict(empresa) if empresa else {"cnpj": cnpj_norm}
    if base.get("capital_social") is not None:
        base["capital_social"] = float(base["capital_social"])
    return {
        **base,
        "grupos": grupos,
        "obras_como_cliente": obras_como_cliente,
        "matches_como_fornecedor": matches_como_fornecedor,
    }


@app.get("/api/fornecedor/{cnpj}/top-matches-pdf")
def fornecedor_top_matches_pdf(cnpj: str):
    """Gera PDF com as 5 obras de maior score_match pra esse CNPJ.
    Público (sem auth) — incentiva fornecedor a se cadastrar pra ver mais."""
    import re as _re
    cnpj_norm = _re.sub(r"[./-]", "", (cnpj or "").strip())
    if len(cnpj_norm) != 14 or not cnpj_norm.isdigit():
        raise HTTPException(400, "CNPJ inválido (esperado 14 dígitos).")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT e.razao_social, e.nome_fantasia
                FROM fornecedores e WHERE e.cnpj=%s
            """, (cnpj_norm,))
            forn = cur.fetchone()
            if not forn:
                raise HTTPException(404, "Fornecedor não encontrado.")

            cur.execute("""
                SELECT DISTINCT ON (m.obra_id)
                    o.id::text AS obra_id, o.nome AS obra_nome, o.empresa,
                    o.uf, o.fase, o.setor, o.valor_formatado,
                    o.classificacao_computed,
                    m.score::int AS score, c.nome AS categoria
                FROM matches_obra_prestador m
                JOIN obras o ON o.id = m.obra_id
                JOIN categorias_servico c ON c.id = m.categoria_id
                WHERE m.cnpj=%s AND (o.visivel IS NULL OR o.visivel=true)
                ORDER BY m.obra_id, m.score DESC NULLS LAST
            """, (cnpj_norm,))
            all_matches = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # Top 5 globais por score
    top5 = sorted(all_matches, key=lambda x: -(x.get("score") or 0))[:5]

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors as _colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from io import BytesIO

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=_colors.HexColor("#fbbf24"), fontSize=18)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, textColor=_colors.HexColor("#1a1a1a"))
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=8, textColor=_colors.HexColor("#6b7280"))

    nome_emp = forn.get("nome_fantasia") or forn.get("razao_social") or cnpj_norm
    story = [
        Paragraph(f"<b>WiNS Hub — Top 5 obras compatíveis</b>", h1),
        Paragraph(f"Fornecedor: <b>{nome_emp}</b> · CNPJ: {cnpj_norm}", body),
        Spacer(1, 0.4*cm),
        Paragraph("Estas são as 5 obras com maior score de compatibilidade técnica (CNAE × UF × porte × categoria) com o seu CNPJ na base WiNS Hub.", small),
        Spacer(1, 0.4*cm),
    ]

    if not top5:
        story.append(Paragraph("<i>Sem matches calculados ainda pra este CNPJ.</i>", body))
    else:
        # FIX overlap: Paragraph (auto-wrap) em todas celulas de texto livre +
        # colWidths ajustados pra caber em 17cm uteis (A4 - 2cm margens)
        # Setor/UF em 2 linhas via <br/> pra setores longos (AUTOMOTIVO_E_AUTOPECAS)
        from xml.sax.saxutils import escape as _xml_escape
        cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=8, leading=10, textColor=_colors.HexColor("#1a1a1a"))
        cell_bold = ParagraphStyle("cell_bold", parent=cell, fontName="Helvetica-Bold")
        data = [["#", "Obra", "Empresa contratante", "Setor / UF", "Capex", "Score", "Categoria"]]
        for i, m in enumerate(top5, 1):
            data.append([
                str(i),
                Paragraph(_xml_escape((m.get('obra_nome') or '—')[:80]), cell_bold),
                Paragraph(_xml_escape((m.get("empresa") or "—")[:50]), cell),
                Paragraph(f"{_xml_escape(m.get('setor') or '—')}<br/>{_xml_escape(m.get('uf') or '—')}", cell),
                Paragraph(_xml_escape((m.get("valor_formatado") or "—")[:30]), cell),
                str(m.get("score") or 0),
                Paragraph(_xml_escape((m.get("categoria") or "—")[:40]), cell),
            ])
        # Larguras somam 17cm (cabe em A4 21cm com margens 2cm cada lado)
        tbl = Table(data, repeatRows=1, colWidths=[0.5*cm, 5.0*cm, 3.2*cm, 2.7*cm, 1.6*cm, 1.0*cm, 3.0*cm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), _colors.HexColor("#fbbf24")),
            ("TEXTCOLOR", (0,0), (-1,0), _colors.HexColor("#1a1a1a")),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 8),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("GRID", (0,0), (-1,-1), 0.4, _colors.HexColor("#e5e7eb")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [_colors.white, _colors.HexColor("#fafaf9")]),
        ]))
        story.append(tbl)

    story.extend([
        Spacer(1, 0.6*cm),
        Paragraph("Quer ver TODAS as obras compatíveis e contato direto dos decisores? "
                  '<b><font color="#fbbf24">Cadastre-se grátis em winshubcomercial.com.br</font></b>', body),
        Spacer(1, 0.2*cm),
        Paragraph(f"Gerado em {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC · WiNS Hub v0.7", small),
    ])
    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()

    headers = {"Content-Disposition": f'inline; filename="wins_top5_{cnpj_norm}.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/api/admin/import_status")
async def import_status(u=Depends(_requer_admin)):
    """Estado da última rodada do orchestrator. Apenas admin."""

    def _iso(dt, dur_ms=None):
        if not dt: return None
        if dur_ms:
            from datetime import timedelta
            dt = dt + timedelta(milliseconds=dur_ms)
        return dt.isoformat()

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT criado_em, status, duracao_ms, erro, novos, buscados
                FROM log_captacao WHERE fonte = 'ORCHESTRATOR'
                ORDER BY criado_em DESC LIMIT 1
            """)
            ultima = cur.fetchone()

            cur.execute("""
                SELECT DISTINCT ON (fonte)
                    fonte, status, novos, buscados, erro, duracao_ms, criado_em
                FROM log_captacao
                WHERE fonte NOT IN ('ORCHESTRATOR', 'MATCHMAKING')
                ORDER BY fonte, criado_em DESC
            """)
            fontes_rows = cur.fetchall()

            cur.execute("""
                SELECT criado_em, status, duracao_ms, novos, buscados, erro
                FROM log_captacao WHERE fonte = 'MATCHMAKING'
                ORDER BY criado_em DESC LIMIT 1
            """)
            mm = cur.fetchone()
    finally:
        conn.close()

    fontes_out = []
    for f in fontes_rows:
        fontes_out.append({
            "fonte": f["fonte"],
            "status": f["status"],
            "inseridos": f["novos"] or 0,
            "atualizados": 0,  # log_captacao não distingue novos vs atualizados
            "erros": 1 if f["status"] == "erro" else 0,
            "finalizado_em": _iso(f["criado_em"], f["duracao_ms"]),
            "duracao_ms": f["duracao_ms"] or 0,
            "mensagem": f["erro"],
        })

    matchmaking_out = None
    if mm:
        status_norm = "executado" if mm["status"] == "sucesso" else mm["status"]
        matchmaking_out = {
            "status": status_norm,
            "motivo_pulo": mm["erro"] if mm["status"] == "pulado" else None,
            "iniciado_em": _iso(mm["criado_em"]),
            "finalizado_em": _iso(mm["criado_em"], mm["duracao_ms"]),
            "matches_gerados": mm["novos"] or 0,
            "obras_processadas": mm["buscados"] or 0,
        }

    return {
        "ultima_execucao": _iso(ultima["criado_em"]) if ultima else None,
        "ultima_status": ultima["status"] if ultima else None,
        "ultima_duracao_ms": ultima["duracao_ms"] if ultima else None,
        "proxima_execucao": "diariamente 02:00 BRT",
        "fontes": fontes_out,
        "matchmaking": matchmaking_out,
    }

def _check_admin_token(token: Optional[str]) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN não configurado no servidor.")
    if not token or not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(401, "Token admin inválido.")

@app.get("/api/admin/dashboard")
async def admin_dashboard(token: str = ""):
    """Painel admin consolidado: KPIs + captadores 24h + usuários por plano + últimos 10 logins."""
    _check_admin_token(token)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Tier KPIs: usa classificacao_computed (canônica pós-Sprint-2 v2.1).
            # Substitui filtro LEGACY (nivel1_nome+cargo_decisor_keyword) que inflava OURO ~7.8x.
            # COUNT(*) direto sem filtros — espelha REGRA IMUTÁVEL de /api/dashboard/stats-public.
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE classificacao_computed='OURO')     AS obras_ouro,
                  COUNT(*) FILTER (WHERE classificacao_computed='PRATA')    AS obras_prata,
                  COUNT(*) FILTER (WHERE classificacao_computed='BRONZE')   AS obras_bronze,
                  COUNT(*) FILTER (WHERE classificacao_computed='PIPELINE') AS obras_pipeline
                FROM obras
            """)
            kpis_tier = dict(cur.fetchone())

            cur.execute("""
                SELECT
                  (SELECT COUNT(*) FROM decisores_obra WHERE excluido_em IS NULL) AS decisores_ativos,
                  (SELECT COUNT(*) FROM matches_obra_prestador) AS matches_total,
                  (SELECT COUNT(*) FROM acessos_log
                    WHERE sucesso AND criado_em >= date_trunc('day', NOW())) AS acessos_hoje,
                  (SELECT COUNT(*) FROM prestadores
                    WHERE ativo AND excluido_em IS NULL) AS usuarios_ativos,
                  (SELECT COUNT(*) FROM obras WHERE criado_em::date = CURRENT_DATE) AS obras_hoje
            """)
            kpis = {**kpis_tier, **dict(cur.fetchone())}

            cur.execute("""
                SELECT lc.fonte,
                       MAX(lc.criado_em) AS ultimo,
                       COUNT(*) FILTER (WHERE lc.criado_em > NOW() - INTERVAL '24 hours') AS execs_24h,
                       COALESCE(SUM(lc.novos)    FILTER (WHERE lc.criado_em > NOW() - INTERVAL '24 hours'), 0)::int AS novos_24h,
                       COALESCE(SUM(lc.buscados) FILTER (WHERE lc.criado_em > NOW() - INTERVAL '24 hours'), 0)::int AS buscados_24h,
                       COUNT(*) FILTER (WHERE lc.status = 'erro' AND lc.criado_em > NOW() - INTERVAL '24 hours') AS erros_24h,
                       (SELECT status FROM log_captacao l2
                         WHERE l2.fonte = lc.fonte ORDER BY criado_em DESC LIMIT 1) AS ultimo_status,
                       (SELECT MAX(o.criado_em) FROM obras o
                         WHERE o.fonte LIKE
                           CASE WHEN lc.fonte = 'captar_noticias_setoriais' THEN 'noticia%'
                                ELSE replace(lc.fonte, 'captar_', '') || '%'
                           END) AS ultima_obra_em
                FROM log_captacao lc
                GROUP BY lc.fonte
                ORDER BY ultimo DESC NULLS LAST
            """)
            captadores = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT p.id::text AS id, p.email, p.nome_empresa, p.plano,
                       p.criado_em, p.ultimo_acesso,
                       (SELECT COUNT(*) FROM acessos_log a
                         WHERE a.prestador_id = p.id AND a.sucesso) AS total_acessos
                FROM prestadores p
                WHERE p.ativo AND p.excluido_em IS NULL
                ORDER BY
                  CASE p.plano WHEN 'PREMIUM' THEN 1 WHEN 'STANDARD' THEN 2 ELSE 3 END,
                  p.ultimo_acesso DESC NULLS LAST
            """)
            usuarios = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT a.criado_em, a.email, a.ip, a.sucesso,
                       p.nome_empresa, p.plano
                FROM acessos_log a
                LEFT JOIN prestadores p ON p.id = a.prestador_id
                ORDER BY a.criado_em DESC
                LIMIT 10
            """)
            logins = [dict(r) for r in cur.fetchall()]
        return {"kpis": kpis, "captadores": captadores, "usuarios": usuarios, "logins": logins}
    finally:
        conn.close()


# ── Review backlog notícias (Serper + Haiku → fila human review) ──────────
@app.get("/api/admin/noticias-backlog")
async def admin_noticias_backlog(token: str = "", limit: int = 50):
    """Lista notícias com status=pending_url, parseando JSON do campo descricao.

    Ordena por capex desc (do JSON), fallback id desc.
    """
    _check_admin_token(token)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, fonte_nome, url, titulo, descricao, status, criado_em,
                       sonnet_analysis, sonnet_confidence, sonnet_tier_recomendado
                FROM noticias_backlog_manual
                WHERE status = 'pending_url'
                ORDER BY id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                # descricao é JSON serializado pelo Haiku
                meta = {}
                try:
                    meta = json.loads(d.get("descricao") or "{}")
                except Exception:
                    pass
                rows.append({
                    "id": d["id"],
                    "fonte_nome": d["fonte_nome"],
                    "url": d["url"],
                    "titulo": d["titulo"],
                    "status": d["status"],
                    "criado_em": d["criado_em"].isoformat() if d.get("criado_em") else None,
                    "empresa": meta.get("empresa"),
                    "cnpj_hint": meta.get("cnpj_hint"),
                    "valor_estimado": meta.get("valor_estimado"),
                    "uf": meta.get("uf"),
                    "municipio": meta.get("municipio"),
                    "setor": meta.get("setor"),
                    "descricao_curta": meta.get("descricao"),
                    "sonnet_analysis": d.get("sonnet_analysis"),
                    "sonnet_confidence": d.get("sonnet_confidence"),
                    "sonnet_tier_recomendado": d.get("sonnet_tier_recomendado"),
                })
            # ordenar por capex desc, NULLs no fim
            rows.sort(key=lambda x: (x.get("valor_estimado") or 0), reverse=True)
        return {"total": len(rows), "noticias": rows}
    finally:
        conn.close()


@app.post("/api/admin/noticias-backlog/{noticia_id}/promover")
async def admin_noticias_backlog_promover(
    noticia_id: int,
    payload: dict,
    token: str = "",
):
    """Promove notícia da fila → INSERT obras + UPDATE status='processado'.

    Body: {"confirmar": true, "classificacao": "OURO|PRATA|PIPELINE"}
    Lookup CNPJ em fornecedores (ILIKE empresa), grava empresa_dominios se houver.
    """
    _check_admin_token(token)
    classificacao = (payload or {}).get("classificacao", "").upper()
    if classificacao not in {"OURO", "PRATA", "BRONZE", "PIPELINE"}:
        raise HTTPException(400, "classificacao deve ser OURO, PRATA, BRONZE ou PIPELINE")
    if not (payload or {}).get("confirmar"):
        raise HTTPException(400, "confirmar=true requerido")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, url, titulo, descricao, status FROM noticias_backlog_manual WHERE id=%s",
                (noticia_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "notícia não encontrada")
            if row["status"] != "pending_url":
                raise HTTPException(409, f"status atual = {row['status']} (esperado pending_url)")

            try:
                meta = json.loads(row.get("descricao") or "{}")
            except Exception:
                meta = {}

            empresa = (meta.get("empresa") or "").strip() or row.get("titulo") or "—"
            capex = meta.get("valor_estimado") or 0
            try:
                capex = int(capex)
            except (TypeError, ValueError):
                capex = 0
            uf = (meta.get("uf") or "").strip()[:2].upper() or None
            municipio = (meta.get("municipio") or "").strip() or None
            setor = (meta.get("setor") or "").strip() or None
            descricao_curta = (meta.get("descricao") or row.get("titulo") or "")[:1000]
            cnpj_hint = (meta.get("cnpj_hint") or "").strip()
            cnpj_hint = "".join(c for c in cnpj_hint if c.isdigit()) if cnpj_hint else ""
            url_fonte = row.get("url")

            # Lookup CNPJ: primeiro tenta cnpj_hint (se passar pelo formato), depois ILIKE
            cnpj_final = None
            if cnpj_hint and len(cnpj_hint) == 14:
                cur.execute(
                    "SELECT cnpj FROM fornecedores WHERE cnpj=%s LIMIT 1",
                    (cnpj_hint,),
                )
                hit = cur.fetchone()
                if hit:
                    cnpj_final = hit["cnpj"]
            if not cnpj_final and empresa:
                # ILIKE com primeiros 2 tokens da empresa (evita match muito amplo)
                tokens = [t for t in re.split(r"\s+", empresa) if len(t) >= 4][:2]
                like = "%" + "%".join(tokens) + "%" if tokens else f"%{empresa[:20]}%"
                cur.execute(
                    "SELECT cnpj FROM fornecedores WHERE razao_social ILIKE %s AND situacao_cadastral='02' ORDER BY length(razao_social) LIMIT 1",
                    (like,),
                )
                hit = cur.fetchone()
                if hit:
                    cnpj_final = hit["cnpj"]

            # INSERT obras
            nome_obra = f"{empresa} — {descricao_curta[:80]}" if descricao_curta else empresa
            cur.execute(
                """
                INSERT INTO obras (
                    nome, empresa, cnpj, setor, municipio, uf,
                    valor_estimado, descricao, fonte, fonte_tipo,
                    classificacao_computed, visivel, data_publicacao, url_fonte
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_DATE, %s)
                RETURNING id, nome, classificacao_computed
                """,
                (
                    nome_obra[:500],
                    empresa[:255],
                    cnpj_final,
                    setor,
                    municipio,
                    uf,
                    capex if capex > 0 else None,
                    descricao_curta,
                    "google_alerts_backlog",
                    "NOTICIA",
                    classificacao,
                    True,
                    url_fonte,
                ),
            )
            obra = dict(cur.fetchone())

            # UPDATE status
            cur.execute(
                "UPDATE noticias_backlog_manual SET status='processado', processado_em=NOW() WHERE id=%s",
                (noticia_id,),
            )
            conn.commit()
        return {"ok": True, "obra": obra, "cnpj_resolvido": cnpj_final}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"erro ao promover: {e}")
    finally:
        conn.close()


@app.post("/api/admin/noticias-backlog/{noticia_id}/rejeitar")
async def admin_noticias_backlog_rejeitar(
    noticia_id: int,
    token: str = "",
):
    """Marca notícia como rejeitada — não insere obra, só tira da fila."""
    _check_admin_token(token)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE noticias_backlog_manual SET status='rejeitado', processado_em=NOW() WHERE id=%s AND status='pending_url' RETURNING id",
                (noticia_id,),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(404, "notícia não encontrada ou já processada")
            conn.commit()
        return {"ok": True, "id": noticia_id}
    finally:
        conn.close()


# ── Painel admin: decisores OURO+PRATA (lista, edição inline, export CSV) ──
@app.get("/api/admin/decisores")
async def admin_decisores(token: str = ""):
    """Lista todas obras OURO/PRATA visíveis com decisor (ou sem) para o painel admin.

    Ordenação por capex desc — quem aparece primeiro é maior valor.
    Front filtra/pagina client-side.
    """
    _check_admin_token(token)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text AS id,
                       empresa, cnpj, classificacao_computed,
                       valor_estimado, uf, setor, fonte,
                       nivel1_nome, nivel1_cargo, nivel1_email,
                       nivel1_linkedin,
                       COALESCE(nivel1_email_smtp_verified, false) AS nivel1_email_smtp_verified,
                       COALESCE(nivel1_origem_enrichment, '') AS nivel1_origem_enrichment
                FROM obras
                WHERE classificacao_computed IN ('OURO','PRATA')
                  AND (visivel IS NULL OR visivel = true)
                ORDER BY valor_estimado DESC NULLS LAST, empresa
            """)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"total": len(rows), "decisores": rows}


@app.put("/api/admin/decisores/{obra_id}")
async def admin_decisor_update(obra_id: str, payload: dict, token: str = ""):
    """Edita decisor de uma obra OURO/PRATA manualmente.

    Body: {nivel1_nome, nivel1_cargo, nivel1_email, nivel1_linkedin}
    Seta nivel1_email_smtp_verified=true automaticamente (assume manual = verificado),
    nivel1_origem_enrichment='manual_admin', nivel1_enrichment_data=NOW().
    Retorna a obra atualizada.
    """
    _check_admin_token(token)
    nome = (payload or {}).get("nivel1_nome", "").strip() or None
    cargo = (payload or {}).get("nivel1_cargo", "").strip() or None
    email = (payload or {}).get("nivel1_email", "").strip() or None
    linkedin = (payload or {}).get("nivel1_linkedin", "").strip() or None
    if not nome and not email:
        raise HTTPException(400, "Pelo menos nome ou email são necessários")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                UPDATE obras SET
                    nivel1_nome = %s,
                    nivel1_cargo = %s,
                    nivel1_email = %s,
                    nivel1_linkedin = %s,
                    nivel1_email_smtp_verified = TRUE,
                    nivel1_email_status = 'manual_verified',
                    nivel1_email_score = COALESCE(nivel1_email_score, 99),
                    nivel1_email_verified_at = NOW(),
                    nivel1_origem_enrichment = COALESCE(nivel1_origem_enrichment,'') || ' +manual_admin',
                    nivel1_enrichment_data = NOW()
                WHERE id = %s
                  AND classificacao_computed IN ('OURO','PRATA')
                RETURNING id::text AS id, empresa, cnpj, classificacao_computed,
                          nivel1_nome, nivel1_cargo, nivel1_email, nivel1_linkedin,
                          nivel1_email_smtp_verified
            """, (nome, cargo, email, linkedin, obra_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Obra não encontrada ou não é OURO/PRATA")
            conn.commit()
        return {"ok": True, "obra": dict(row)}
    finally:
        conn.close()


@app.get("/api/admin/decisores/export")
async def admin_decisores_export(token: str = ""):
    """Export CSV de todas as obras OURO/PRATA visíveis (para Mari trabalhar offline).

    Delimitador ; (Excel-friendly) + BOM UTF-8, igual ao export da fila.
    """
    import csv as _csv
    import io as _io
    _check_admin_token(token)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT empresa, cnpj, classificacao_computed, valor_estimado,
                       uf, setor, fonte,
                       nivel1_nome, nivel1_cargo, nivel1_email, nivel1_linkedin,
                       COALESCE(nivel1_email_smtp_verified, false) AS smtp_verified,
                       COALESCE(nivel1_origem_enrichment, '') AS origem_enrichment
                FROM obras
                WHERE classificacao_computed IN ('OURO','PRATA')
                  AND (visivel IS NULL OR visivel = true)
                ORDER BY valor_estimado DESC NULLS LAST, empresa
            """)
            rows = list(cur.fetchall())
    finally:
        conn.close()

    buf = _io.StringIO()
    writer = _csv.writer(buf, delimiter=';', quoting=_csv.QUOTE_MINIMAL)
    writer.writerow([
        "Empresa", "CNPJ", "Classificacao", "Capex_BRL", "UF", "Setor", "Fonte",
        "Decisor_Nome", "Decisor_Cargo", "Decisor_Email", "Decisor_LinkedIn",
        "SMTP_Verificado", "Origem_Enrichment",
    ])
    for r in rows:
        writer.writerow([
            r["empresa"] or "", r["cnpj"] or "", r["classificacao_computed"] or "",
            r["valor_estimado"] or "", r["uf"] or "", r["setor"] or "", r["fonte"] or "",
            r["nivel1_nome"] or "", r["nivel1_cargo"] or "", r["nivel1_email"] or "",
            r["nivel1_linkedin"] or "",
            "sim" if r["smtp_verified"] else "nao",
            r["origem_enrichment"] or "",
        ])
    csv_body = "﻿" + buf.getvalue()  # BOM UTF-8 pra Excel
    return Response(
        content=csv_body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="decisores_wins_hub.csv"'},
    )


# ── Forçar execução manual dos captadores (V9 admin) ──────────────────────
# Lista alinhada com orchestrator.CAPTADORES + dnit. anp roda com --commit
# (insere obras agregadas PTE). doe/doe_sp fora (HTML scrape + precisa GCP).
RUN_CAPTADORES_MANUAL: list[tuple[str, list[str]]] = [
    ("captar_ibama",              []),
    ("captar_bndes",              []),
    ("captar_aneel",              []),
    ("captar_antaq",              []),
    ("captar_anm",                []),
    ("captar_cvm",                []),
    ("captar_cimm",               []),
    ("captar_agenciainfra",       []),
    ("captar_noticias_setoriais", []),
    ("captar_pncp_obras",         []),
    ("captar_pncp_consulta",      []),
    ("captar_pncp_defesa",        []),
    ("captar_dou_inlabs",         []),
    ("captar_eletrobras_ri",      []),
    ("captar_anp",                ["--commit"]),
    ("captar_dnit",               []),
    ("captar_google_alerts",      []),
    # 16/05 sessão noite: PNCP full (real) + 6 scaffolds estaduais/federais
    ("captar_pncp_full",          []),
    ("captar_der_sp",             []),
    ("captar_cdhu_sp",            []),
    ("captar_sabesp_sp",          []),
    ("captar_antt_rod_v2",        []),
    ("captar_saneamento",         []),
    ("captar_transparencia",      []),
    # 16/05 noite v3: wrapper que reexec captar_bndes.py --saneamento
    ("captar_bndes_saneamento",   []),
]

ADMIN_RUN_LOG_DIR = "/app/logs"
ADMIN_RUN_TIMEOUT_S = 600  # 10 min por captador


async def _executar_captadores_manual(job_id: str):
    import asyncio
    os.makedirs(ADMIN_RUN_LOG_DIR, exist_ok=True)
    log_path = os.path.join(ADMIN_RUN_LOG_DIR, f"manual_{job_id}.log")

    def _append(msg: str):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg)

    _append(f"=== MANUAL CAPTADORES {job_id} START {datetime.utcnow().isoformat()}Z ===\n")
    _append(f"total={len(RUN_CAPTADORES_MANUAL)}\n")

    for nome, args in RUN_CAPTADORES_MANUAL:
        ts = datetime.utcnow().isoformat()
        _append(f"\n[{ts}Z] INICIO {nome} {' '.join(args)}\n")
        try:
            proc = await asyncio.create_subprocess_exec(
                "python", f"/app/scripts/{nome}.py", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=ADMIN_RUN_TIMEOUT_S
                )
                if stdout:
                    _append(stdout.decode("utf-8", errors="replace"))
                _append(
                    f"[{datetime.utcnow().isoformat()}Z] FIM {nome} exit={proc.returncode}\n"
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                _append(
                    f"[{datetime.utcnow().isoformat()}Z] TIMEOUT {nome} (>{ADMIN_RUN_TIMEOUT_S}s)\n"
                )
        except Exception as e:
            _append(
                f"[{datetime.utcnow().isoformat()}Z] ERRO {nome}: {type(e).__name__}: {e}\n"
            )

    _append(f"\n=== MANUAL CAPTADORES {job_id} END {datetime.utcnow().isoformat()}Z ===\n")


@app.post("/api/admin/run-captadores")
async def admin_run_captadores(background_tasks: BackgroundTasks, token: str = ""):
    """ADMIN: força execução de todos captadores em background. Retorna job_id."""
    _check_admin_token(token)
    job_id = f"{int(time.time())}"
    background_tasks.add_task(_executar_captadores_manual, job_id)
    return {
        "job_id": job_id,
        "status": "iniciado",
        "captadores": len(RUN_CAPTADORES_MANUAL),
    }


@app.get("/api/admin/run-captadores/{job_id}")
async def admin_run_captadores_status(job_id: str, token: str = "", tail: int = 200):
    """ADMIN: progresso + tail do log de um job de captadores manuais."""
    _check_admin_token(token)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", job_id):
        raise HTTPException(400, "job_id inválido")
    log_path = os.path.join(ADMIN_RUN_LOG_DIR, f"manual_{job_id}.log")
    if not os.path.exists(log_path):
        raise HTTPException(404, f"job {job_id} não encontrado")
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    tail_n = max(1, min(int(tail), 500))
    tail_lines = lines[-tail_n:]
    concluidos = sum(
        1 for ln in lines
        if (" FIM " in ln) or (" TIMEOUT " in ln) or (" ERRO " in ln)
    )
    finalizado = any("MANUAL CAPTADORES" in ln and "END " in ln for ln in lines[-3:])
    return {
        "job_id": job_id,
        "status": "concluido" if finalizado else "rodando",
        "concluidos": concluidos,
        "total": len(RUN_CAPTADORES_MANUAL),
        "log": "".join(tail_lines),
    }


# ═══════════════════════════════════════════════════════════════
# LEGACY INTENCIONAL — workflow Mari/enriquecimento (audit 21/05)
# Filtro semântico: "obra com decisor preenchido + contato verificado"
# (nivel1_nome + nivel1_email/linkedin + cargo_decisor_keyword).
# NÃO migrar para classificacao_computed='OURO': o TIER canônico filtra
# por CAPEX (≥R$500mi), não por dados de decisor. Endpoints têm consumidores
# de workflow Mari/agente remoto que dependem desse universo específico.
# ═══════════════════════════════════════════════════════════════
@app.get("/api/admin/ouro_parcial")
async def admin_listar_ouro_parcial(token: str = "", limit: int = 50):
    """Lista obras com is_ouro_parcial=true (nome+cargo decisor, sem email/linkedin).
    Usada pelo agente remoto de enriquecimento. Token via querystring por simplicidade."""
    _check_admin_token(token)
    lim = min(max(int(limit), 1), 200)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text AS obra_id, nome, empresa, cnpj, uf, municipio,
                       nivel1_nome AS decisor_nome, nivel1_cargo AS decisor_cargo,
                       nivel1_email, nivel1_linkedin
                FROM obras
                WHERE nivel1_nome IS NOT NULL AND nivel1_nome != ''
                  AND COALESCE(nivel1_email,'') = ''
                  AND COALESCE(nivel1_linkedin,'') = ''
                  AND cargo_decisor_keyword(nivel1_cargo)
                  AND COALESCE(fonte_tipo,'OFICIAL') != 'NOTICIA'
                  AND (visivel IS NULL OR visivel = true)
                ORDER BY lead_score DESC NULLS LAST, urgencia ASC
                LIMIT %s
            """, (lim,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"total": len(rows), "obras": rows}


class EnriquecerReq(BaseModel):
    obra_id: str
    decisor_nome: Optional[str] = None
    linkedin: Optional[str] = None
    email: Optional[str] = None
    fonte: str


@app.post("/api/admin/decisores/enriquecer")
async def admin_enriquecer_decisor(
    req: EnriquecerReq,
    cred: HTTPAuthorizationCredentials = Depends(security),
):
    """Aplica enriquecimento de LinkedIn/email no decisor principal da obra.
    Atualiza obras.nivel1_* e decisores_obra (linha mais recente do mesmo nome),
    registra cada campo alterado em enriquecimento_log."""
    token = cred.credentials if cred else ""
    _check_admin_token(token)

    if not req.linkedin and not req.email:
        raise HTTPException(400, "Forneça pelo menos linkedin OU email.")
    if not req.fonte or len(req.fonte) > 500:
        raise HTTPException(400, "Campo fonte obrigatório (URL ou descrição da origem).")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, nivel1_nome, nivel1_email, nivel1_linkedin "
                "FROM obras WHERE id=%s",
                (req.obra_id,),
            )
            obra = cur.fetchone()
            if not obra:
                raise HTTPException(404, "Obra não encontrada.")

            updates = {}
            if req.linkedin and not (obra["nivel1_linkedin"] or "").strip():
                updates["nivel1_linkedin"] = req.linkedin
            if req.email and not (obra["nivel1_email"] or "").strip():
                updates["nivel1_email"] = req.email
            if not updates:
                return {"obra_id": req.obra_id, "atualizados": [], "ja_preenchido": True}

            set_sql = ", ".join(f"{k} = %s" for k in updates)
            cur.execute(
                f"UPDATE obras SET {set_sql} WHERE id = %s",
                list(updates.values()) + [req.obra_id],
            )

            decisor_id = None
            nome_match = (req.decisor_nome or obra["nivel1_nome"] or "").strip()
            if nome_match:
                cur.execute(
                    "SELECT id FROM decisores_obra "
                    "WHERE obra_id=%s AND nome=%s AND excluido_em IS NULL "
                    "ORDER BY registrado_em DESC LIMIT 1",
                    (req.obra_id, nome_match),
                )
                d = cur.fetchone()
                if d:
                    decisor_id = d["id"]
                    sets = []
                    vals = []
                    if "nivel1_linkedin" in updates:
                        sets.append("linkedin_url = COALESCE(NULLIF(linkedin_url,''), %s)")
                        vals.append(updates["nivel1_linkedin"])
                    if "nivel1_email" in updates:
                        sets.append("email = COALESCE(NULLIF(email,''), %s)")
                        vals.append(updates["nivel1_email"])
                    if sets:
                        vals.append(decisor_id)
                        cur.execute(
                            f"UPDATE decisores_obra SET {', '.join(sets)} WHERE id=%s",
                            vals,
                        )

            for campo, novo in updates.items():
                anterior = obra[campo]
                cur.execute(
                    "INSERT INTO enriquecimento_log "
                    "(obra_id, decisor_id, decisor_nome, campo, valor_anterior, valor_novo, fonte) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (req.obra_id, decisor_id, nome_match or None,
                     campo, anterior, novo, req.fonte),
                )
        conn.commit()
    finally:
        conn.close()

    return {"obra_id": req.obra_id, "atualizados": list(updates.keys()), "ja_preenchido": False}


TIPOS_CARGO_VALIDOS = {
    "GERENTE_SUPRIMENTOS","GERENTE_COMPRAS","SUPPLY_CHAIN",
    "ENGENHEIRO_MECANICO_CIVIL","GERENTE_ENGENHARIA","PROJETISTA",
    "COORDENADOR_MANUTENCAO","GERENTE_INDUSTRIAL","COORDENADOR_OBRAS",
    "GERENTE_PROJETOS","OUTRO",
}


# ═══════════════════════════════════════════════════════════════
# LEGACY INTENCIONAL — workflow Mari/enriquecimento (audit 21/05)
# Filtro semântico: "obra com decisor preenchido + contato verificado"
# (nivel1_nome + nivel1_email/linkedin + cargo_decisor_keyword).
# NÃO migrar para classificacao_computed='OURO': o TIER canônico filtra
# por CAPEX (≥R$500mi), não por dados de decisor. Endpoints têm consumidores
# de workflow Mari/agente remoto que dependem desse universo específico.
# ═══════════════════════════════════════════════════════════════
@app.get("/api/admin/empresas_ouro_gaps")
async def admin_empresas_ouro_gaps(token: str = "", limit: int = 50):
    """Lista empresas das obras-ouro com cobertura de cargos por empresa.
    Retorna, por empresa: lista de obras, valor_max, e quais tipos_cargo já tem decisor cadastrado.
    O agente usa para decidir quais cargos ainda faltam buscar."""
    _check_admin_token(token)
    lim = min(max(int(limit), 1), 200)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH ouro AS (
                    SELECT id::text AS obra_id, nome, empresa, cnpj, uf, valor_estimado
                    FROM obras
                    WHERE nivel1_nome IS NOT NULL AND nivel1_nome != ''
                      AND (COALESCE(nivel1_email,'') != '' OR COALESCE(nivel1_linkedin,'') != '')
                      AND cargo_decisor_keyword(nivel1_cargo)
                      AND COALESCE(fonte_tipo,'OFICIAL') != 'NOTICIA'
                      AND (visivel IS NULL OR visivel = TRUE)
                ),
                cob AS (
                    SELECT o.empresa, array_agg(DISTINCT d.tipo_cargo)
                           FILTER (WHERE d.tipo_cargo IS NOT NULL) AS cargos_ja
                    FROM ouro o
                    LEFT JOIN decisores_obra d
                      ON d.obra_id::text = o.obra_id AND d.excluido_em IS NULL
                    GROUP BY o.empresa
                )
                SELECT o.empresa,
                       MAX(o.valor_estimado) AS valor_max,
                       COUNT(*) AS qtd_obras,
                       array_agg(DISTINCT o.cnpj) FILTER (WHERE o.cnpj IS NOT NULL) AS cnpjs,
                       array_agg(DISTINCT o.obra_id) AS obra_ids,
                       MAX(c.cargos_ja) AS cargos_ja
                FROM ouro o LEFT JOIN cob c ON c.empresa = o.empresa
                GROUP BY o.empresa
                ORDER BY valor_max DESC NULLS LAST
                LIMIT %s
            """, (lim,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    todos_cargos = sorted(TIPOS_CARGO_VALIDOS - {"OUTRO"})
    for r in rows:
        ja = set(r.get("cargos_ja") or [])
        r["cargos_faltantes"] = [c for c in todos_cargos if c not in ja]
        if r.get("valor_max") is not None:
            r["valor_max"] = float(r["valor_max"])
    return {"total": len(rows), "empresas": rows}


class CadastrarDecisorReq(BaseModel):
    obra_id: str
    nome: str
    cargo: str
    tipo_cargo: str
    linkedin: Optional[str] = None
    email: Optional[str] = None
    telefone: Optional[str] = None
    fonte: str
    observacoes: Optional[str] = None


@app.post("/api/admin/decisores/cadastrar")
async def admin_cadastrar_decisor(
    req: CadastrarDecisorReq,
    cred: HTTPAuthorizationCredentials = Depends(security),
):
    """Insere um novo decisor em decisores_obra com tipo_cargo padronizado.
    Idempotente por (obra_id, tipo_cargo, lower(nome)) — re-POST do mesmo decisor não duplica."""
    token = cred.credentials if cred else ""
    _check_admin_token(token)

    if req.tipo_cargo not in TIPOS_CARGO_VALIDOS:
        raise HTTPException(400, f"tipo_cargo inválido. Use um de: {sorted(TIPOS_CARGO_VALIDOS)}")
    if not req.linkedin and not req.email and not req.telefone:
        raise HTTPException(400, "Forneça pelo menos um contato (linkedin, email ou telefone).")
    if not req.fonte or len(req.fonte) > 500:
        raise HTTPException(400, "Campo fonte obrigatório (URL ou descrição da origem).")
    nome = (req.nome or "").strip()
    if not nome:
        raise HTTPException(400, "Nome obrigatório.")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT empresa FROM obras WHERE id = %s", (req.obra_id,))
            obra_row = cur.fetchone()
            if not obra_row:
                raise HTTPException(404, "Obra não encontrada.")
            empresa_obra = obra_row.get("empresa") or ""

            cur.execute("""
                SELECT id FROM decisores_obra
                WHERE obra_id = %s AND tipo_cargo = %s
                  AND lower(nome) = lower(%s) AND excluido_em IS NULL
                LIMIT 1
            """, (req.obra_id, req.tipo_cargo, nome))
            existing = cur.fetchone()
            if existing:
                return {"obra_id": req.obra_id, "decisor_id": str(existing["id"]),
                        "status": "ja_existe"}

            permite, motivo = decisor_inserivel(cur, nome, req.cargo or "", empresa_obra)
            if not permite:
                raise HTTPException(422, f"decisor_gate rejeitou: {motivo}")

            cur.execute("""
                INSERT INTO decisores_obra
                    (obra_id, nome, cargo, tipo_cargo, linkedin_url, email, telefone,
                     fonte, registrado_por, observacoes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'admin_api',%s)
                RETURNING id
            """, (req.obra_id, nome, req.cargo, req.tipo_cargo,
                  req.linkedin or None, req.email or None, req.telefone or None,
                  req.fonte, req.observacoes or None))
            new_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    return {"obra_id": req.obra_id, "decisor_id": str(new_id), "status": "criado"}


# ═══════════════════════════════════════════════════════════════
# LEGACY INTENCIONAL — workflow Mari/enriquecimento (audit 21/05)
# Filtro semântico: "obra com decisor preenchido + contato verificado"
# (nivel1_nome + nivel1_email/linkedin + cargo_decisor_keyword).
# NÃO migrar para classificacao_computed='OURO': o TIER canônico filtra
# por CAPEX (≥R$500mi), não por dados de decisor. Endpoints têm consumidores
# de workflow Mari/agente remoto que dependem desse universo específico.
# ═══════════════════════════════════════════════════════════════
@app.get("/api/admin/em_execucao_sem_decisor")
async def admin_listar_em_execucao_sem_decisor(token: str = "", limit: int = 20):
    """Lista obras EM_EXECUCAO sem decisor cadastrado e ainda não esgotadas pela routine.
    Filtra obras já marcadas em enriquecimento_log com fonte LIKE 'WEBSEARCH_ROUTINE%'."""
    _check_admin_token(token)
    lim = min(max(int(limit), 1), 100)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text AS obra_id, nome, empresa, cnpj, uf, municipio,
                       valor_estimado, lead_score
                FROM obras
                WHERE fase = 'EM_EXECUCAO'
                  AND (nivel1_nome IS NULL OR nivel1_nome = '')
                  AND COALESCE(fonte_tipo,'OFICIAL') != 'NOTICIA'
                  AND (visivel IS NULL OR visivel = true)
                  AND id NOT IN (
                      SELECT obra_id FROM enriquecimento_log
                      WHERE fonte LIKE %s
                  )
                ORDER BY lead_score DESC NULLS LAST, urgencia ASC NULLS LAST
                LIMIT %s
            """, ('WEBSEARCH_ROUTINE%', lim))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"total": len(rows), "obras": rows}


class MarcarEsgotadaReq(BaseModel):
    obra_id: str
    fonte: str = "WEBSEARCH_ROUTINE_VAZIO"


@app.post("/api/admin/obras/marcar_esgotada")
async def admin_marcar_obra_esgotada(
    req: MarcarEsgotadaReq,
    cred: HTTPAuthorizationCredentials = Depends(security),
):
    """Registra em enriquecimento_log que a routine de WebSearch esgotou os 10 cargos
    da lista do Anderson sem encontrar decisor. Idempotente por (obra_id, fonte)."""
    token = cred.credentials if cred else ""
    _check_admin_token(token)

    if not req.fonte or len(req.fonte) > 500:
        raise HTTPException(400, "fonte obrigatória.")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT 1 FROM obras WHERE id = %s", (req.obra_id,))
            if not cur.fetchone():
                raise HTTPException(404, "Obra não encontrada.")

            cur.execute("""
                SELECT id FROM enriquecimento_log
                WHERE obra_id = %s AND fonte = %s
                LIMIT 1
            """, (req.obra_id, req.fonte))
            if cur.fetchone():
                return {"obra_id": req.obra_id, "status": "ja_existe"}

            cur.execute("""
                INSERT INTO enriquecimento_log
                    (obra_id, decisor_id, decisor_nome, campo, valor_anterior, valor_novo, fonte)
                VALUES (%s, NULL, NULL, 'websearch_routine', NULL, 'esgotado', %s)
                RETURNING id
            """, (req.obra_id, req.fonte))
            new_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    return {"obra_id": req.obra_id, "log_id": str(new_id), "status": "criado"}


@app.get("/api/admin/decisores_anderson_sem_linkedin")
async def admin_listar_anderson_sem_linkedin(token: str = "", limit: int = 100):
    """Lista decisores `fonte LIKE 'anderson_csv%'` com tipo_cargo classificado
    (entram no funil) mas sem linkedin_url. Alimenta a routine one-time de WebSearch."""
    _check_admin_token(token)
    lim = min(max(int(limit), 1), 500)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT d.id::text AS decisor_id, d.nome, d.cargo, d.tipo_cargo,
                       d.email, d.obra_id::text AS obra_id,
                       o.nome AS obra_nome, o.empresa, o.uf, o.lead_score
                FROM decisores_obra d
                JOIN obras o ON o.id = d.obra_id
                WHERE d.excluido_em IS NULL
                  AND d.fonte LIKE 'anderson_csv%%'
                  AND d.tipo_cargo IS NOT NULL
                  AND d.tipo_cargo != 'OUTRO'
                  AND COALESCE(d.linkedin_url, '') = ''
                  AND (o.visivel IS NULL OR o.visivel = true)
                ORDER BY o.lead_score DESC NULLS LAST, o.urgencia ASC NULLS LAST
                LIMIT %s
            """, (lim,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"total": len(rows), "decisores": rows}


PADROES_EMAIL_VALIDOS = {
    "nome.sobrenome",       # joao.silva@empresa.com
    "nome_sobrenome",       # joao_silva@empresa.com
    "nomesobrenome",        # joaosilva@empresa.com
    "inicial.sobrenome",    # j.silva@empresa.com
    "inicial_sobrenome",    # j_silva@empresa.com
    "inicialsobrenome",     # jsilva@empresa.com (estilo PRIO)
    "primeironome",         # joao@empresa.com
    "outro",                # registrar mas não inferir
}


class PadraoEmailReq(BaseModel):
    dominio_email: str
    padrao_email: str
    fonte: str
    amostras: Optional[list] = None  # [{"email": "...", "nome": "...", "url_evidencia": "..."}]


@app.post("/api/admin/fornecedor_meta/padrao_email")
async def admin_set_padrao_email(
    req: PadraoEmailReq,
    cred: HTTPAuthorizationCredentials = Depends(security),
):
    """Cadastra/atualiza o padrão de email de um domínio em fornecedor_meta.
    Atualiza TODOS os CNPJs que compartilham o mesmo dominio_email."""
    token = cred.credentials if cred else ""
    _check_admin_token(token)

    if req.padrao_email not in PADROES_EMAIL_VALIDOS:
        raise HTTPException(400, f"padrao_email inválido. Use um de: {sorted(PADROES_EMAIL_VALIDOS)}")
    dom = (req.dominio_email or "").strip().lower()
    if not dom or "." not in dom:
        raise HTTPException(400, "dominio_email inválido")
    if not req.fonte or len(req.fonte) > 500:
        raise HTTPException(400, "fonte obrigatória (URL ou descrição da origem)")

    obs_extra = ""
    if req.amostras:
        try:
            import json as _json
            obs_extra = " | amostras=" + _json.dumps(req.amostras, ensure_ascii=False)[:1000]
        except Exception:
            pass

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE fornecedor_meta
                SET padrao_email = %s,
                    dominio_email_fonte = COALESCE(NULLIF(%s,''), dominio_email_fonte),
                    observacoes = COALESCE(observacoes,'') || %s,
                    atualizado_em = NOW()
                WHERE dominio_email = %s
                """,
                (req.padrao_email, req.fonte, obs_extra, dom),
            )
            updated = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    if updated == 0:
        raise HTTPException(404, f"Nenhum CNPJ com dominio_email='{dom}' em fornecedor_meta")

    return {"dominio_email": dom, "padrao_email": req.padrao_email,
            "cnpjs_atualizados": updated}


def _normalize_token(s: str) -> str:
    """Lowercase + remove acentos + remove não-alfanum."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ASCII", "ignore").decode("ASCII")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _gerar_email_por_padrao(nome_completo: str, dominio: str, padrao: str) -> Optional[str]:
    """Gera email candidato dado nome + domínio + padrão. None se padrão não suporta inferência."""
    if not nome_completo or not dominio or not padrao or padrao == "outro":
        return None
    tokens = [t for t in re.split(r"\s+", nome_completo.strip()) if t]
    if len(tokens) < 1:
        return None
    primeiro = _normalize_token(tokens[0])
    ultimo = _normalize_token(tokens[-1]) if len(tokens) > 1 else ""
    if not primeiro:
        return None
    inicial = primeiro[0] if primeiro else ""

    local = None
    if padrao == "primeironome":
        local = primeiro
    elif not ultimo:
        # Padrões com sobrenome exigem 2+ tokens
        return None
    elif padrao == "nome.sobrenome":   local = f"{primeiro}.{ultimo}"
    elif padrao == "nome_sobrenome":   local = f"{primeiro}_{ultimo}"
    elif padrao == "nomesobrenome":    local = f"{primeiro}{ultimo}"
    elif padrao == "inicial.sobrenome":local = f"{inicial}.{ultimo}"
    elif padrao == "inicial_sobrenome":local = f"{inicial}_{ultimo}"
    elif padrao == "inicialsobrenome": local = f"{inicial}{ultimo}"
    if not local:
        return None
    return f"{local}@{dominio.lower()}"


@app.post("/api/admin/decisores/inferir_emails")
async def admin_inferir_emails(
    dry_run: int = 1,
    limit: int = 100,
    cred: HTTPAuthorizationCredentials = Depends(security),
):
    """Para cada decisor sem email cuja empresa tem dominio_email + padrao_email cadastrados,
    gera o email candidato pelo padrão.

    dry_run=1 (default): SÓ mostra o que faria, não persiste.
    dry_run=0: persiste em decisores_obra.email + registra em enriquecimento_log.

    Idempotente: nunca sobrescreve email já preenchido."""
    token = cred.credentials if cred else ""
    _check_admin_token(token)
    lim = min(max(int(limit), 1), 500)

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT d.id::text AS decisor_id, d.obra_id::text, d.nome, d.cargo, d.tipo_cargo,
                       o.empresa, o.cnpj,
                       fm.dominio_email, fm.padrao_email, fm.dominio_email_fonte
                FROM decisores_obra d
                JOIN obras o            ON o.id  = d.obra_id
                JOIN fornecedor_meta fm ON fm.cnpj = o.cnpj
                WHERE d.excluido_em IS NULL
                  AND COALESCE(d.email,'') = ''
                  AND fm.dominio_email IS NOT NULL AND fm.dominio_email != ''
                  AND fm.padrao_email IS NOT NULL AND fm.padrao_email != ''
                  AND fm.padrao_email != 'outro'
                ORDER BY o.empresa, d.nome
                LIMIT %s
                """,
                (lim,),
            )
            candidatos = cur.fetchall()
    finally:
        conn.close()

    plano = []
    pulados = []
    for c in candidatos:
        email = _gerar_email_por_padrao(c["nome"], c["dominio_email"], c["padrao_email"])
        if not email:
            pulados.append({
                "decisor_id": c["decisor_id"], "nome": c["nome"],
                "motivo": "nome_insuficiente_para_padrao",
                "padrao": c["padrao_email"],
            })
            continue
        plano.append({
            "decisor_id": c["decisor_id"], "obra_id": c["obra_id"],
            "empresa": c["empresa"], "nome": c["nome"], "cargo": c["cargo"],
            "tipo_cargo": c["tipo_cargo"],
            "dominio": c["dominio_email"], "padrao": c["padrao_email"],
            "padrao_fonte": c["dominio_email_fonte"],
            "email_inferido": email,
        })

    aplicados = 0
    if not dry_run and plano:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                for p in plano:
                    fonte = f"inferencia_padrao:{p['padrao']}|fonte_padrao:{p['padrao_fonte'] or 'desconhecida'}"
                    cur.execute(
                        """
                        UPDATE decisores_obra
                        SET email = %s
                        WHERE id = %s AND COALESCE(email,'') = ''
                        """,
                        (p["email_inferido"], p["decisor_id"]),
                    )
                    if cur.rowcount > 0:
                        aplicados += 1
                        cur.execute(
                            """
                            INSERT INTO enriquecimento_log
                              (obra_id, decisor_id, decisor_nome, campo, valor_anterior, valor_novo, fonte)
                            VALUES (%s, %s, %s, 'email', NULL, %s, %s)
                            """,
                            (p["obra_id"], p["decisor_id"], p["nome"], p["email_inferido"], fonte),
                        )
            conn.commit()
        finally:
            conn.close()

    return {
        "dry_run": bool(dry_run),
        "candidatos_encontrados": len(candidatos),
        "plano": plano,
        "pulados": pulados,
        "aplicados": aplicados,
    }


TAG_LABEL = {
    "tem_itsm":                    {"label": "ITSM próprio", "icon": "🛠️", "pitch": "TI estruturada — fala com gerente de TI/infra"},
    "tem_helpdesk":                {"label": "Helpdesk",      "icon": "📞", "pitch": "Atendimento interno ativo"},
    "tem_treinamento_corporativo": {"label": "Treinamento corporativo", "icon": "🎓", "pitch": "Demanda contínua de instrutor / consultor"},
    "tem_universidade_corporativa":{"label": "Universidade corp.",      "icon": "🎓", "pitch": "Programa formal de capacitação"},
    "tem_portal_fornecedor":       {"label": "Portal de fornecedor",    "icon": "🤝", "pitch": "Procurement digitalizado — gateway pra cadastro"},
    "tem_portal_compras":          {"label": "Portal de compras",       "icon": "🛒", "pitch": "Procurement online — investigar processo"},
    "tem_assinatura_digital":      {"label": "Assinatura digital",      "icon": "✍️", "pitch": "Compliance / jurídico ativo"},
    "tem_portal_documentos":       {"label": "Portal de documentos",    "icon": "📄", "pitch": "Gestão documental madura"},
    "tem_integracao_api":          {"label": "Integração API",          "icon": "🔌", "pitch": "Time de integrações / arquitetura"},
    "tem_api_publica":             {"label": "API pública",             "icon": "🔌", "pitch": "Ecossistema técnico aberto"},
    "tem_portal_aplicativos":      {"label": "Portal de aplicativos",   "icon": "📱", "pitch": "Catálogo interno de software"},
    "tem_ambiente_homol":          {"label": "Ambiente homologação",    "icon": "🧪", "pitch": "TI madura (separação dev/homol/prod)"},
    "tem_ambiente_dev":            {"label": "Ambiente dev",            "icon": "🧪", "pitch": "Time de dev interno"},
    "tem_intranet":                {"label": "Intranet",                "icon": "🏢", "pitch": "Comunicação interna estruturada"},
    "tem_extranet":                {"label": "Extranet",                "icon": "🌐", "pitch": "Colaboração com parceiros"},
    "tem_portal_corporativo":      {"label": "Portal corporativo",      "icon": "🏢", "pitch": "Ecossistema digital interno"},
    "tem_portal_carreira":         {"label": "Portal de carreira",      "icon": "👤", "pitch": "RH ativo — vagas técnicas pra mapear"},
    "tem_rh_interno":              {"label": "RH digital",              "icon": "👥", "pitch": "Operações de pessoas estruturadas"},
    "tem_crm":                     {"label": "CRM próprio",             "icon": "📊", "pitch": "Vendas estruturadas"},
    "tem_erp":                     {"label": "ERP próprio",             "icon": "🗄️", "pitch": "Backoffice consolidado"},
    "tem_ecommerce":               {"label": "E-commerce",              "icon": "🛍️", "pitch": "Vendas online ativas"},
    "tem_ouvidoria":               {"label": "Ouvidoria",               "icon": "📣", "pitch": "Compliance/governança ativa"},
    "tem_blog_corporativo":        {"label": "Blog corporativo",        "icon": "📝", "pitch": "Marketing de conteúdo"},
}


@app.get("/api/empresas/{cnpj}/intel")
async def empresa_intel(cnpj: str, u=Depends(get_user)):
    """Retorna a inteligência comercial coletada para a empresa.
    Requer login (qualquer plano). GRATUITO vê tags sem pitch hint."""
    plano = u.get("plano", "GRATUITO") if u else "GRATUITO"
    cnpj_norm = re.sub(r"[./-]", "", (cnpj or "").strip())
    if len(cnpj_norm) != 14:
        raise HTTPException(400, "CNPJ inválido")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT cnpj, empresa, dominio, subdominios, tags, fonte, coletado_em, erro
                FROM empresa_intel
                WHERE cnpj = %s
                ORDER BY coletado_em DESC
                LIMIT 1
                """,
                (cnpj_norm,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {"cnpj": cnpj_norm, "intel": None, "tags": []}

    # Enriquecer com is_representante pra reps verem pitch
    if u:
        _conn = get_conn()
        try:
            with _conn.cursor(cursor_factory=RealDictCursor) as _cur:
                _cur.execute("SELECT COALESCE(is_representante,false) AS is_representante FROM prestadores WHERE id=%s", (u["sub"],))
                _row = _cur.fetchone()
                if _row:
                    u = {**u, "is_representante": _row["is_representante"]}
        finally:
            _conn.close()
    revelar_pitch = pode_ver_conteudo_pago(u)
    tags_out = []
    for t in (row["tags"] or []):
        meta = TAG_LABEL.get(t, {"label": t, "icon": "🔍", "pitch": ""})
        tag_obj = {"key": t, "label": meta["label"], "icon": meta["icon"]}
        if revelar_pitch and meta.get("pitch"):
            tag_obj["pitch"] = meta["pitch"]
        tags_out.append(tag_obj)

    return {
        "cnpj": row["cnpj"],
        "empresa": row["empresa"],
        "dominio": row["dominio"],
        "subdominios": row["subdominios"] or [],
        "tags": tags_out,
        "coletado_em": row["coletado_em"].isoformat() if row["coletado_em"] else None,
        "fonte": row["fonte"],
        "erro": row["erro"],
        "acesso_pitch": revelar_pitch,
    }


def _mascarar_email_decisor(email):
    """Mascara user-part pra paywall (preserva domínio)."""
    if not email or "@" not in email:
        return None
    user, dom = email.split("@", 1)
    if len(user) <= 2:
        return f"{user[0]}***@{dom}"
    return f"{user[0]}.{user[2]}***@{dom}"


def _normalizar_tipo_cargo_cache(cargo):
    """Mapeia cargo livre do cache LLM pra TIPO_CARGO_ORDEM."""
    if not cargo or not cargo.strip():
        return "DECISOR_AUTO"
    c = cargo.upper()
    if "PROCUREMENT" in c or "SUPRIMENT" in c or "SUPPLY" in c:
        return "GERENTE_SUPRIMENTOS"
    if "COMPRAS" in c or "BUYER" in c or "PURCHAS" in c:
        return "GERENTE_COMPRAS"
    if "GERENTE" in c and "ENGENHARIA" in c:
        return "GERENTE_ENGENHARIA"
    if "ENGENHEIRO" in c or "ENGINEER" in c:
        return "ENGENHEIRO_MECANICO_CIVIL"
    if "PROJETOS" in c or "PROJECT" in c:
        return "GERENTE_PROJETOS"
    if "INDUSTRIAL" in c:
        return "GERENTE_INDUSTRIAL"
    if "MANUTEN" in c or "MAINTEN" in c:
        return "COORDENADOR_MANUTENCAO"
    if "OBRAS" in c or "CONSTRU" in c:
        return "COORDENADOR_OBRAS"
    return "OUTRO"


@app.get("/api/obras/{oid}/detalhe")
async def detalhe_obra_completo(oid: str, u=Depends(get_user)):
    """Página pública da obra. Decisor segue mascarado para deslogado/GRATUITO via filtrar_obra + pode_ver_decisores_obra."""
    import uuid as _uuid
    try:
        _uuid.UUID(oid)
    except (ValueError, AttributeError):
        raise HTTPException(404, "Obra não encontrada")

    # JWT só tem sub/plano/is_representante. Enriquece com email do DB
    # quando logado para preservar bypass admin em pode_ver_decisores_obra.
    if u:
        _conn_u = get_conn()
        try:
            with _conn_u.cursor(cursor_factory=RealDictCursor) as _cur_u:
                _cur_u.execute(
                    "SELECT email, COALESCE(plano,'GRATUITO') AS plano, "
                    "COALESCE(is_representante,false) AS is_representante "
                    "FROM prestadores WHERE id=%s",
                    (u["sub"],),
                )
                _row_u = _cur_u.fetchone()
                if _row_u:
                    u = {**u, **dict(_row_u)}
        finally:
            _conn_u.close()

    plano = (u.get("plano") if u else None) or "GRATUITO"

    from routes.prestadores import TIPO_CARGO_LABEL, TIPO_CARGO_ORDEM

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT obras.*, "
                "EXISTS (SELECT 1 FROM decisores_obra d WHERE d.obra_id = obras.id AND d.excluido_em IS NULL) AS tem_decisor_externo "
                "FROM obras WHERE id=%s",
                (oid,),
            )
            obra = cur.fetchone()
            if not obra:
                raise HTTPException(404, "Obra não encontrada.")

        desbloqueada = False
        if u:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM interacoes WHERE prestador_id=%s AND obra_id=%s AND tipo='DESBLOQUEIO' LIMIT 1",
                    (u["sub"], oid),
                )
                desbloqueada = cur.fetchone() is not None

            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO interacoes (obra_id,prestador_id,tipo,plano_momento) "
                        "VALUES (%s,%s,'VISUALIZACAO',%s) ON CONFLICT DO NOTHING",
                        (oid, u["sub"], plano),
                    )
                conn.commit()
            except Exception:
                conn.rollback()

        obra_filtrada = filtrar_obra(dict(obra), plano, desbloqueada)

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id::text, nome, cargo, tipo_cargo, "
                "linkedin_url, email, telefone, fonte "
                "FROM decisores_obra "
                "WHERE obra_id=%s AND excluido_em IS NULL "
                "ORDER BY tipo_cargo NULLS LAST, registrado_em DESC",
                (oid,),
            )
            decisores_rows = cur.fetchall()

        decisores = []
        for r in decisores_rows:
            tipo = r["tipo_cargo"] or "OUTRO"
            decisores.append({
                "id": r["id"],
                "tipo_cargo": tipo,
                "label": TIPO_CARGO_LABEL.get(tipo, tipo),
                "nome": r["nome"],
                "cargo": r["cargo"],
                "linkedin": r["linkedin_url"] or None,
                "email": r["email"] or None,
                "telefone": r["telefone"] or None,
                "fonte": r["fonte"],
                "is_auto_descoberto": False,
                "confianca": None,
            })

        # Camadas 3+4: decisores do cache LLM enricher (só pagantes não-rep)
        if pode_ver_decisores_obra(u) and obra.get("cnpj"):
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id::text, nome_pessoa, cargo_raw, cargo_normalizado, "
                    "       filtro_llm_confianca, email, linkedin_slug, score_relevancia "
                    "FROM empresa_decisores_cache "
                    "WHERE cnpj=%s AND trabalha_atualmente=true AND excluido_em IS NULL "
                    "  AND COALESCE(filtro_llm_confianca, confianca) IN ('alta','media') "
                    "ORDER BY (COALESCE(filtro_llm_confianca, confianca)='alta') DESC, score_relevancia DESC NULLS LAST "
                    "LIMIT 10",
                    (obra["cnpj"],),
                )
                cache_rows = cur.fetchall()
            for r in cache_rows:
                tipo = _normalizar_tipo_cargo_cache(r.get("cargo_normalizado") or r.get("cargo_raw"))
                lk = f"https://linkedin.com/in/{r['linkedin_slug']}" if r.get("linkedin_slug") else None
                decisores.append({
                    "id": "cache_" + r["id"],
                    "tipo_cargo": tipo,
                    "label": TIPO_CARGO_LABEL.get(tipo, tipo),
                    "nome": r["nome_pessoa"],
                    "cargo": r.get("cargo_raw") or "",
                    "linkedin": lk,
                    "email": r.get("email") or None,
                    "telefone": None,
                    "fonte": "AUTO_MATCH",
                    "is_auto_descoberto": True,
                    "confianca": r.get("filtro_llm_confianca"),
                })

        # Decisor (nome/email/linkedin/telefone) é gratuito pra todos logados a partir de v0.1.23-alpha.
        # Mantido só `desbloqueada` como flag histórica (não restringe campos).
        # Paywall removido: Auto-Match (R$10/busca) continua sob wallet; só o decisor foi liberado.

        decisores.sort(key=lambda d: (
            1 if d.get("is_auto_descoberto") else 0,
            TIPO_CARGO_ORDEM.index(d["tipo_cargo"]) if d["tipo_cargo"] in TIPO_CARGO_ORDEM else 99,
            0 if d.get("confianca") == "alta" else 1,
            d["nome"] or "",
        ))

        intel = None
        if obra.get("cnpj"):
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT subdominios, tags, fonte, coletado_em "
                    "FROM empresa_intel WHERE cnpj=%s ORDER BY coletado_em DESC LIMIT 1",
                    (obra["cnpj"],),
                )
                row = cur.fetchone()
            if row and row["tags"]:
                tags_out = []
                for t in (row["tags"] or []):
                    meta = TAG_LABEL.get(t, {"label": t, "icon": "🔍", "pitch": ""})
                    tags_out.append({"key": t, "label": meta["label"], "icon": meta["icon"],
                                     "pitch": meta.get("pitch", "")})
                intel = {
                    "subdominios": row["subdominios"] or [],
                    "tags": tags_out,
                    "coletado_em": row["coletado_em"].isoformat() if row["coletado_em"] else None,
                    "fonte": row["fonte"],
                }

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT c.codigo AS cat_codigo, c.nome AS cat_nome, c.ordem AS cat_ordem,
                       m.ranking, m.cnpj, m.nivel_proximidade, m.score,
                       e.nome_fantasia, e.razao_social, e.municipio_nome, e.uf,
                       e.telefone_1, e.telefone_2, e.email, e.cnae_principal
                FROM matches_obra_prestador m
                JOIN categorias_servico c ON c.id = m.categoria_id
                LEFT JOIN fornecedores e ON e.cnpj = m.cnpj
                WHERE m.obra_id = %s
                ORDER BY c.ordem, m.ranking
                """,
                (oid,),
            )
            forn_rows = cur.fetchall()

        cats = {}
        for r in forn_rows:
            code = r["cat_codigo"]
            cats.setdefault(code, {
                "codigo": code, "nome": r["cat_nome"], "ordem": r["cat_ordem"],
                "qtd_matches": 0, "prestadores": [],
            })
            cats[code]["qtd_matches"] += 1
            if len(cats[code]["prestadores"]) < 5:
                cats[code]["prestadores"].append({
                    "ranking": r["ranking"],
                    "cnpj": r["cnpj"],
                    "nome_fantasia": r["nome_fantasia"] or r["razao_social"] or "",
                    "municipio": r["municipio_nome"], "uf": r["uf"],
                    "telefone": (r["telefone_1"] or r["telefone_2"]) if desbloqueada else None,
                    "email": r["email"] if desbloqueada else None,
                    "cnae_principal": r["cnae_principal"],
                    "score": float(r["score"]) if r["score"] else 0,
                })
        fornecedores = sorted(cats.values(), key=lambda x: x["ordem"])

        # Reps não veem decisores; flag decisores_bloqueados sinaliza pro frontend
        if not pode_ver_decisores_obra(u):
            decisores = []
            decisores_bloqueados = True
        else:
            decisores_bloqueados = False

        # CNPJs vinculados pra montar o desbloqueio
        minhas_empresas = []
        if u:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT cnpj, razao_social, tipo FROM prestador_empresas "
                    "WHERE prestador_id=%s AND ativo ORDER BY criado_em",
                    (u["sub"],),
                )
                minhas_empresas = [dict(r) for r in cur.fetchall()]

        # Paywall do decisor removido em v0.1.23-alpha — decisor liberado pra todos logados.
        # Auto-Match e wallet continuam intocados (endpoints separados).
        paywall_info = None

        return {
            "obra": obra_filtrada,
            "decisores": decisores,
            "decisores_bloqueados": decisores_bloqueados,
            "decisores_count": len(decisores),
            "intel": intel,
            "fornecedores_categorias": fornecedores,
            "desbloqueada": desbloqueada,
            "minhas_empresas": minhas_empresas,
            "paywall_info": paywall_info,
        }
    finally:
        conn.close()


@app.post("/api/matches/regenerar")
async def matches_regenerar(u=Depends(requer_auth)):
    """Regenera matches_v2 do prestador (síncrono se pequeno, async se massivo).
    Usa função PL/pgSQL regenerar_matches_v2_para_prestador."""
    if MATCHMAKER_VERSION != 'v2':
        raise HTTPException(409, "Matchmaker v2 não está ativo (feature flag).")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT regenerar_matches_v2_para_prestador(%s, 30)", (u["sub"],))
            inseridos = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "matches_inseridos": inseridos}


@app.get("/api/matches/status")
async def matches_status_endpoint(u=Depends(requer_auth)):
    """Polling pelo frontend após login para saber se matchmaking on-demand terminou."""
    conn = get_conn()
    try:
        return _matches_status(u["sub"], conn)
    finally:
        conn.close()


# ============================================================
# Pagamentos — Mercado Pago (Checkout Pro / Preference one-time)
# ============================================================
MATCHMAKER_VERSION = os.getenv("MATCHMAKER_VERSION", "v2").lower()  # v1=matches_obra_prestador, v2=matches_v2

MP_MODE = os.getenv("MP_MODE", "production").lower()
if MP_MODE == "test":
    MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN_TEST", "")
else:
    MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
MP_USE_SANDBOX = os.getenv("MP_USE_SANDBOX", "false").lower() == "true"
log.info("MP_MODE=%s use_sandbox=%s token_prefix=%s", MP_MODE, MP_USE_SANDBOX,
         (MP_ACCESS_TOKEN[:8] + "...") if MP_ACCESS_TOKEN else "(vazio)")
MP_API_BASE     = "https://api.mercadopago.com"
PLANOS = {
    "GRATUITO": {"preco_centavos": 0,     "nome": "Gratuito"},
    "STANDARD": {"preco_centavos": 29700, "nome": "Standard"},
    "PREMIUM":  {"preco_centavos": 69700, "nome": "Premium"},
}

# === PRICING DEFINITIVO (06/05/2026) ===
# 4 modalidades × 2 planos pagos. Mantém PLANOS pra compat com fluxo existente.
PRECOS_MENSALIDADE = {
    "STANDARD": {
        "MENSAL":     {"valor_mes_centavos": 19700, "valor_total_centavos": 19700,  "duracao_meses": 1,  "desconto_pct": 0},
        "TRIMESTRAL": {"valor_mes_centavos": 17700, "valor_total_centavos": 53100,  "duracao_meses": 3,  "desconto_pct": 10},
        "SEMESTRAL":  {"valor_mes_centavos": 16700, "valor_total_centavos": 100200, "duracao_meses": 6,  "desconto_pct": 15},
        "ANUAL":      {"valor_mes_centavos": 15700, "valor_total_centavos": 188400, "duracao_meses": 12, "desconto_pct": 20},
    },
    "PREMIUM": {
        "MENSAL":     {"valor_mes_centavos": 49700, "valor_total_centavos": 49700,  "duracao_meses": 1,  "desconto_pct": 0},
        "TRIMESTRAL": {"valor_mes_centavos": 44700, "valor_total_centavos": 134100, "duracao_meses": 3,  "desconto_pct": 10},
        "SEMESTRAL":  {"valor_mes_centavos": 42200, "valor_total_centavos": 253200, "duracao_meses": 6,  "desconto_pct": 15},
        "ANUAL":      {"valor_mes_centavos": 39700, "valor_total_centavos": 476400, "duracao_meses": 12, "desconto_pct": 20},
    },
}

DESBLOQUEIOS_INCLUSOS = {
    "GRATUITO": 0,
    "STANDARD": 3,
    "PREMIUM":  10,
}

LIMITE_OBRA_INCLUSA_BRL = 10_000_000  # R$ 10 Mi


def preco_avulso_por_valor_obra(valor_obra_brl):
    """Preço avulso (centavos) baseado no valor estimado da obra."""
    if valor_obra_brl is None or valor_obra_brl <= 10_000_000:
        return 9700      # R$ 97
    elif valor_obra_brl <= 100_000_000:
        return 29700     # R$ 297
    elif valor_obra_brl <= 1_000_000_000:
        return 69700     # R$ 697
    else:
        return 99700     # R$ 997


class CriarPrefReq(BaseModel):
    plano: Optional[str] = None
    modalidade: Optional[str] = None       # MENSAL | TRIMESTRAL | SEMESTRAL | ANUAL
    renunciar_avaliacao: Optional[bool] = False
    obra_id: Optional[str] = None
    cnpj_empresa: Optional[str] = None

@app.post("/api/pagamento/criar_preferencia")
def criar_preferencia(req: CriarPrefReq, u=Depends(requer_auth)):
    if not MP_ACCESS_TOKEN:
        raise HTTPException(503, "MP não configurado (MP_ACCESS_TOKEN ausente para MP_MODE=%s)." % MP_MODE)

    is_plano = bool(req.plano)
    is_desbloqueio = bool(req.obra_id and req.cnpj_empresa)
    if is_plano == is_desbloqueio:
        raise HTTPException(400, "Envie {plano} (assinatura) OU {obra_id, cnpj_empresa} (desbloqueio).")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT email, nome_empresa FROM prestadores WHERE id=%s", (u["sub"],))
            prestador = cur.fetchone()
            if not prestador:
                raise HTTPException(404, "Prestador não encontrado.")

        if is_plano:
            if req.plano not in ("STANDARD", "PREMIUM"):
                raise HTTPException(400, "Plano inválido. Use STANDARD ou PREMIUM.")
            modalidade = (req.modalidade or "MENSAL").upper()
            if modalidade not in PRECOS_MENSALIDADE[req.plano]:
                raise HTTPException(400, "Modalidade inválida. Use MENSAL, TRIMESTRAL, SEMESTRAL ou ANUAL.")
            mod_cfg = PRECOS_MENSALIDADE[req.plano][modalidade]
            preco_centavos = mod_cfg["valor_total_centavos"]
            info = PLANOS[req.plano]
            titulo = f"Plano {info['nome']} {modalidade.title()} — WiNS HUB ({mod_cfg['duracao_meses']} meses)"
            tipo = "plano"
            obra_id_db = None
            cnpj_db = None
            metadata = {
                "prestador_id": u["sub"], "tipo": tipo, "plano": req.plano,
                "modalidade": modalidade,
                "renunciou_avaliacao": bool(req.renunciar_avaliacao),
            }
            back_path = "/planos"
        else:
            _validar_uuid(req.obra_id)
            cnpj_db = _cnpj_digits(req.cnpj_empresa)
            if len(cnpj_db) != 14:
                raise HTTPException(400, "CNPJ inválido.")
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT 1 FROM prestador_empresas WHERE prestador_id=%s AND cnpj=%s AND ativo",
                            (u["sub"], cnpj_db))
                if not cur.fetchone():
                    raise HTTPException(403, "CNPJ não vinculado à sua conta.")
                cur.execute("SELECT id, nome, valor_estimado FROM obras WHERE id=%s", (req.obra_id,))
                obra = cur.fetchone()
                if not obra:
                    raise HTTPException(404, "Obra não encontrada.")
            faixa_key, faixa = _faixa_da_obra(obra.get("valor_estimado"))
            # Bloquear GRATUITO de desbloquear decisores
            plano_atual = u.get("plano", "GRATUITO")
            if plano_atual == "GRATUITO":
                raise HTTPException(
                    status_code=403,
                    detail={
                        "erro": "plano_requerido",
                        "mensagem": "Para desbloquear decisores você precisa de um plano ativo.",
                        "cta_url": "/planos"
                    }
                )

            preco_centavos = faixa["preco"]
            titulo = f"Desbloqueio decisor — {(obra['nome'] or 'Obra')[:60]}"
            tipo = "desbloqueio"
            obra_id_db = req.obra_id
            metadata = {
                "prestador_id": u["sub"], "tipo": tipo,
                "obra_id": req.obra_id, "cnpj_empresa": cnpj_db,
                "faixa": faixa_key,
            }
            back_path = f"/obra/{req.obra_id}"

        modalidade_db = metadata.get("modalidade") if is_plano else None
        renunciou_db = bool(metadata.get("renunciou_avaliacao")) if is_plano else False
        # Lookup lead_outbound vinculado ao prestador (retrocompat / audit)
        lead_outbound_id_db = None
        with conn.cursor() as cur_lead:
            cur_lead.execute("""
                SELECT id FROM leads_outbound
                WHERE prestador_id=%s AND representante_id IS NOT NULL
                ORDER BY cadastrou_em DESC NULLS LAST, criado_em DESC LIMIT 1
            """, (u["sub"],))
            row_lead = cur_lead.fetchone()
            if row_lead:
                lead_outbound_id_db = row_lead[0]
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO pagamentos
                     (prestador_id, tipo, plano, preco_centavos, obra_id, cnpj_empresa,
                      modalidade, renunciou_avaliacao, lead_outbound_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (u["sub"], tipo, req.plano, preco_centavos, obra_id_db, cnpj_db,
                 modalidade_db, renunciou_db, lead_outbound_id_db)
            )
            pagamento_id = str(cur.fetchone()["id"])
        conn.commit()

        payload = {
            "items": [{
                "title": titulo,
                "quantity": 1,
                "unit_price": preco_centavos / 100.0,
                "currency_id": "BRL",
            }],
            "payer": {"email": prestador["email"]},
            "back_urls": {
                "success": f"{APP_URL}{back_path}?status=aprovado",
                "failure": f"{APP_URL}{back_path}?status=recusado",
                "pending": f"{APP_URL}{back_path}?status=pendente",
            },
            "auto_return": "approved",
            "notification_url": f"{APP_URL}/api/pagamento/webhook",
            "external_reference": pagamento_id,
            "metadata": metadata,
            "statement_descriptor": "WINSHUB",
        }
        headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=15.0) as client:
                r = client.post(f"{MP_API_BASE}/checkout/preferences", json=payload, headers=headers)
        except Exception as e:
            log.exception("Erro chamando MP: %s", e)
            raise HTTPException(502, "Falha ao contatar Mercado Pago.")
        if r.status_code >= 400:
            log.error("MP criar_preferencia %s: %s", r.status_code, r.text[:500])
            raise HTTPException(502, f"Mercado Pago retornou {r.status_code}.")
        data = r.json()
        with conn.cursor() as cur:
            cur.execute("UPDATE pagamentos SET mp_preference_id=%s WHERE id=%s",
                        (data.get("id"), pagamento_id))
        conn.commit()
        resp = {
            "init_point": data.get("init_point"),
            "preference_id": data.get("id"),
            "pagamento_id": pagamento_id,
            "tipo": tipo,
            "preco_centavos": preco_centavos,
            "mp_mode": MP_MODE,
        }
        if MP_USE_SANDBOX and data.get("sandbox_init_point"):
            resp["sandbox_init_point"] = data.get("sandbox_init_point")
        return resp
    finally:
        conn.close()


@app.post("/api/pagamento/webhook")
async def pagamento_webhook(request: Request):
    """Notificação MP — type=payment com data.id. Busca detalhes e atualiza prestador."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    log.info("MP webhook: %s", body)

    tipo = body.get("type") or body.get("topic")
    payment_id = (body.get("data") or {}).get("id") or body.get("id")
    if tipo != "payment" or not payment_id:
        return {"ok": True, "ignored": True, "type": tipo}

    if not MP_ACCESS_TOKEN:
        log.error("MP webhook recebido mas MP_ACCESS_TOKEN não configurado")
        return {"ok": False, "erro": "token_ausente"}

    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(f"{MP_API_BASE}/v1/payments/{payment_id}", headers=headers)
    except Exception as e:
        log.exception("MP webhook fetch payment falhou: %s", e)
        return {"ok": False, "erro": "fetch_falhou"}
    if r.status_code != 200:
        log.error("MP get payment %s: %s %s", payment_id, r.status_code, r.text[:300])
        return {"ok": False, "erro": "fetch_status_ruim"}

    payment = r.json()
    status = payment.get("status")
    external_ref = payment.get("external_reference")
    payer = payment.get("payer") or {}
    if not external_ref:
        return {"ok": True, "ignored": "sem_external_reference"}

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT id, prestador_id, tipo, plano, preco_centavos, obra_id, cnpj_empresa,
                                  modalidade, renunciou_avaliacao
                           FROM pagamentos WHERE id=%s""", (external_ref,))
            row = cur.fetchone()
            if not row:
                log.warning("MP webhook: pagamento %s não encontrado no DB", external_ref)
                return {"ok": True, "ignored": "pagamento_local_inexistente"}
            with conn.cursor() as cur2:
                local_status = "aprovado" if status == "approved" else ("recusado" if status in ("rejected","cancelled") else "pendente")
                cur2.execute(
                    "UPDATE pagamentos SET mp_payment_id=%s, mp_status=%s, status_local=%s, atualizado_em=NOW() WHERE id=%s",
                    (str(payment_id), status, local_status, row["id"])
                )
                if status == "approved" and row["prestador_id"]:
                    if row["tipo"] == "plano" and row["plano"]:
                        modalidade_pag = (row.get("modalidade") or "MENSAL").upper()
                        mod_cfg = PRECOS_MENSALIDADE.get(row["plano"], {}).get(modalidade_pag)
                        if mod_cfg:
                            duracao = mod_cfg["duracao_meses"]
                            preco_mes = mod_cfg["valor_mes_centavos"] / 100.0
                            preco_total = mod_cfg["valor_total_centavos"] / 100.0
                            credito_centavos = int(mod_cfg["valor_total_centavos"])
                            cur2.execute(
                                """UPDATE prestadores SET
                                     plano=%s,
                                     plano_expira=NOW()+ make_interval(months => %s),
                                     mp_payer_id=%s,
                                     modalidade=%s,
                                     preco_pago_mes=%s,
                                     preco_pago_total=%s,
                                     ciclo_inicio=NOW(),
                                     ciclo_fim=NOW()+ make_interval(months => %s),
                                     proximo_billing=NOW()+ make_interval(months => %s),
                                     creditos_ganhos=COALESCE(creditos_ganhos,0)+%s
                                   WHERE id=%s""",
                                (row["plano"], duracao, str(payer.get("id") or ""),
                                 modalidade_pag, preco_mes, preco_total,
                                 duracao, duracao, credito_centavos, row["prestador_id"])
                            )
                            cur2.execute(
                                "INSERT INTO interacoes (prestador_id, tipo, plano_momento, valor_cobrado) "
                                "VALUES (%s, 'CREDITO_ASSINATURA', %s, %s)",
                                (row["prestador_id"], row["plano"], credito_centavos)
                            )
                        else:
                            # Fallback (modalidade desconhecida) — comportamento legado: 30 dias
                            # No crédito wallet (sem mod_cfg, valor_total não confiável)
                            cur2.execute(
                                "UPDATE prestadores SET plano=%s, plano_expira=NOW()+INTERVAL '30 days', mp_payer_id=%s WHERE id=%s",
                                (row["plano"], str(payer.get("id") or ""), row["prestador_id"])
                            )
                            log.warning("MP webhook plano %s sem mod_cfg — wallet NÃO creditado pra prestador %s",
                                        row["plano"], row["prestador_id"])
                        log.info("Plano %s/%s ativado pra prestador %s (pagamento %s)",
                                 row["plano"], modalidade_pag, row["prestador_id"], payment_id)
                    elif row["tipo"] == "desbloqueio" and row["obra_id"] and row["cnpj_empresa"]:
                        cur3 = conn.cursor(cursor_factory=RealDictCursor)
                        try:
                            cur3.execute("""SELECT id, nome, empresa, valor_estimado, valor_formatado, uf,
                                                   municipio, fase, setor, nivel1_nome, nivel1_cargo,
                                                   nivel1_email, nivel1_linkedin
                                            FROM obras WHERE id=%s""", (row["obra_id"],))
                            obra_row = cur3.fetchone()
                        finally:
                            cur3.close()
                        faixa_key, _faixa = _faixa_da_obra((obra_row or {}).get("valor_estimado"))
                        pitch = _gerar_pitch_simples(dict(obra_row), row["cnpj_empresa"], conn) if obra_row else ""
                        cur2.execute("""
                            INSERT INTO desbloqueios (prestador_id, obra_id, cnpj_empresa, faixa_valor, valor_cobrado, pitch_gerado)
                            VALUES (%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (prestador_id, obra_id, cnpj_empresa) DO UPDATE
                              SET valor_cobrado=EXCLUDED.valor_cobrado, pitch_gerado=EXCLUDED.pitch_gerado
                        """, (row["prestador_id"], row["obra_id"], row["cnpj_empresa"],
                              faixa_key, row["preco_centavos"], pitch))
                        log.info("Desbloqueio registrado: prestador=%s obra=%s cnpj=%s (pagamento %s)",
                                 row["prestador_id"], row["obra_id"], row["cnpj_empresa"], payment_id)

                # ── Comissão de representante (savepoint pra não quebrar pagamento se falhar) ──
                if status == "approved" and row["prestador_id"]:
                    try:
                        with conn.cursor() as cur_sp:
                            cur_sp.execute("SAVEPOINT comissao_sp")
                        if row["tipo"] == "plano" and row["plano"]:
                            if eh_primeira_assinatura(conn, row["prestador_id"]):
                                cid = calcular_comissao_lead(conn, row["prestador_id"], row["preco_centavos"], 'INICIAL')
                                if cid:
                                    cur2.execute("""
                                        UPDATE leads_outbound
                                        SET assinou_em=NOW(), status='ASSINOU',
                                            valor_pago_centavos=%s, plano_assinado=%s
                                        WHERE prestador_id=%s AND assinou_em IS NULL
                                    """, (row["preco_centavos"], row["plano"], row["prestador_id"]))
                        elif row["tipo"] == "desbloqueio":
                            calcular_comissao_lead(conn, row["prestador_id"], row["preco_centavos"], 'AVULSO')
                        with conn.cursor() as cur_sp:
                            cur_sp.execute("RELEASE SAVEPOINT comissao_sp")
                    except Exception as e:
                        log.exception("Erro criando comissão (rollback savepoint, pagamento mantido): %s", e)
                        try:
                            with conn.cursor() as cur_sp:
                                cur_sp.execute("ROLLBACK TO SAVEPOINT comissao_sp")
                        except Exception:
                            pass
        conn.commit()
        return {"ok": True, "status": status, "local": local_status, "tipo": row["tipo"]}
    finally:
        conn.close()


@app.get("/api/pagamento/status")
def pagamento_status(u=Depends(requer_auth)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT plano, plano_expira,
                       (plano IS NOT NULL AND plano != 'GRATUITO'
                        AND (plano_expira IS NULL OR plano_expira > NOW())) AS ativo
                FROM prestadores WHERE id=%s
            """, (u["sub"],))
            atual = dict(cur.fetchone() or {})
            cur.execute("""
                SELECT id::text AS id, tipo, plano, preco_centavos, status_local, mp_status,
                       mp_preference_id, obra_id::text AS obra_id, cnpj_empresa,
                       criado_em, atualizado_em
                FROM pagamentos WHERE prestador_id=%s
                ORDER BY criado_em DESC LIMIT 5
            """, (u["sub"],))
            historico = [dict(r) for r in cur.fetchall()]
        return {"plano_atual": atual, "historico": historico}
    finally:
        conn.close()


@app.get("/api/pagamento/status/{payment_id}")
def pagamento_status_one(payment_id: str, u=Depends(requer_auth)):
    """Detalhes de um pagamento específico (do próprio prestador)."""
    _validar_uuid(payment_id)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text AS id, tipo, plano, preco_centavos,
                       status_local, mp_status, mp_payment_id, mp_preference_id,
                       obra_id::text AS obra_id, cnpj_empresa,
                       criado_em, atualizado_em
                FROM pagamentos
                WHERE id=%s AND prestador_id=%s
            """, (payment_id, u["sub"]))
            row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Pagamento não encontrado.")
        return dict(row)
    finally:
        conn.close()


# ============================================================
# Sistema de desbloqueio por obra (multi-CNPJ + faixas)
# ============================================================
FAIXAS_VALOR = {
    "ate_10mi":  {"max": 10_000_000,    "preco": 9700,  "label": "até R$ 10 Mi"},
    "10_100mi":  {"max": 100_000_000,   "preco": 29700, "label": "R$ 10–100 Mi"},
    "100mi_1bi": {"max": 1_000_000_000, "preco": 69700, "label": "R$ 100 Mi–1 Bi"},
    "acima_1bi": {"max": None,          "preco": 99700, "label": "acima de R$ 1 Bi"},
}
# DEPRECATED 2026-05-12: modelo wallet único, ver project_winshub_pricing.md.
# DESBLOQUEIOS_PLANO removido; tabela desbloqueios_plano mantida só pra audit histórico.

def _faixa_da_obra(valor_estimado):
    if not valor_estimado or float(valor_estimado) <= 0:
        return ("ate_10mi", FAIXAS_VALOR["ate_10mi"])
    v = float(valor_estimado)
    if v <= FAIXAS_VALOR["ate_10mi"]["max"]:   return ("ate_10mi",  FAIXAS_VALOR["ate_10mi"])
    if v <= FAIXAS_VALOR["10_100mi"]["max"]:   return ("10_100mi",  FAIXAS_VALOR["10_100mi"])
    if v <= FAIXAS_VALOR["100mi_1bi"]["max"]:  return ("100mi_1bi", FAIXAS_VALOR["100mi_1bi"])
    return ("acima_1bi", FAIXAS_VALOR["acima_1bi"])

def _saldo_wallet(conn, prestador_id):
    """Saldo wallet em centavos (creditos_ganhos - creditos_consumidos)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(creditos_ganhos,0)-COALESCE(creditos_consumidos,0) AS saldo "
            "FROM prestadores WHERE id=%s",
            (prestador_id,)
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

def _gerar_pitch_simples(obra, cnpj_empresa, conn):
    razao = ""; cnae = ""
    score = "—"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT pe.razao_social, er.cnae_principal
            FROM prestador_empresas pe
            LEFT JOIN fornecedores er ON er.cnpj = pe.cnpj
            WHERE pe.cnpj = %s LIMIT 1
        """, (cnpj_empresa,))
        row = cur.fetchone()
        if row:
            razao = row.get("razao_social") or ""
            cnae = row.get("cnae_principal") or ""
        cur.execute("""
            SELECT MAX(score)::int AS s FROM matches_obra_prestador
            WHERE cnpj = %s AND obra_id = %s
        """, (cnpj_empresa, str(obra["id"])))
        r2 = cur.fetchone()
        if r2 and r2["s"]: score = r2["s"]
    decisor_nome = (obra.get("nivel1_nome") or "decisor").split()[0]
    nome_obra = (obra.get("nome") or "")[:80]
    valor_fmt = obra.get("valor_formatado") or "valor não divulgado"
    fase = (obra.get("fase") or "").replace("_", " ").title()
    return f"""Olá {decisor_nome},

Vi que {obra.get('empresa') or 'sua empresa'} está com {nome_obra}
em fase de {fase} — obra estimada em {valor_fmt}.

Nossa empresa, {razao}, atua em {cnae or 'serviços compatíveis'}
e temos experiência em projetos similares na região {obra.get('uf') or 'Brasil'}.

Score de compatibilidade: {score}/100

Podemos agendar uma conversa rápida para apresentar nossa capacidade técnica?

Atenciosamente,
[Seu nome]""".strip()

# --- /api/me/saldo_desbloqueios ---

@app.get("/api/me/saldo_desbloqueios")
def me_saldo_desbloqueios(u=Depends(requer_auth)):
    """Saldo wallet + total histórico de desbloqueios."""
    conn = get_conn()
    try:
        saldo_atual = _saldo_wallet(conn, u["sub"])
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE valor_cobrado > 0) AS pagos,
                       COALESCE(SUM(valor_cobrado), 0)::int AS gasto_centavos
                FROM desbloqueios WHERE prestador_id = %s
            """, (u["sub"],))
            stats = dict(cur.fetchone())
        return {
            "saldo_atual_centavos": saldo_atual,
            "saldo_atual_reais": saldo_atual / 100,
            "historico": stats,
        }
    finally:
        conn.close()

# --- /api/minhas_empresas (CRUD) ---

class AdicionarEmpresaReq(BaseModel):
    cnpj: str
    tipo: Optional[str] = "representante"

@app.get("/api/minhas_empresas")
def listar_minhas_empresas(u=Depends(requer_auth)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT pe.cnpj, pe.razao_social, pe.tipo, pe.criado_em,
                       COALESCE(m.matches_count, 0) AS obras_compativeis
                FROM prestador_empresas pe
                LEFT JOIN (
                    SELECT cnpj, COUNT(*) AS matches_count
                    FROM matches_obra_prestador
                    GROUP BY cnpj
                ) m ON m.cnpj = pe.cnpj
                WHERE pe.prestador_id = %s AND pe.ativo
                ORDER BY pe.criado_em
            """, (u["sub"],))
            return {"empresas": [dict(r) for r in cur.fetchall()]}
    finally:
        conn.close()

@app.post("/api/minhas_empresas")
def adicionar_minha_empresa(req: AdicionarEmpresaReq, u=Depends(requer_auth)):
    digits = _cnpj_digits(req.cnpj)
    if len(digits) != 14:
        raise HTTPException(400, "CNPJ deve ter 14 dígitos.")
    if req.tipo not in ("representante", "proprio"):
        raise HTTPException(400, "Tipo inválido. Use 'representante' ou 'proprio'.")
    dados, erro_tipo = consultar_cnpj_com_erro(digits)
    if erro_tipo == "NAO_ENCONTRADO":
        raise HTTPException(400, "CNPJ não cadastrado na Receita Federal.")
    if erro_tipo == "FORMATO_INVALIDO":
        raise HTTPException(400, "CNPJ inválido. Verifique os dígitos verificadores.")
    if erro_tipo == "RATE_LIMIT":
        raise HTTPException(429, "Muitas consultas no momento. Aguarde alguns segundos.")
    if erro_tipo == "SERVICO_INDISPONIVEL":
        raise HTTPException(503, "Receita Federal temporariamente indisponível. Tente novamente.")
    if erro_tipo == "ERRO_REDE" or not dados:
        raise HTTPException(500, "Erro ao consultar Receita Federal.")
    situacao = (dados.get("descricao_situacao_cadastral") or "").upper().strip()
    if situacao != "ATIVA":
        raise HTTPException(400, f"Empresa com situação '{situacao or 'desconhecida'}'. Apenas ATIVAS podem ser vinculadas.")
    razao_social = dados.get("razao_social") or ""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO prestador_empresas (prestador_id, cnpj, razao_social, tipo, ativo)
                VALUES (%s, %s, %s, %s, true)
                ON CONFLICT (prestador_id, cnpj) DO UPDATE
                  SET ativo = true,
                      razao_social = COALESCE(EXCLUDED.razao_social, prestador_empresas.razao_social),
                      tipo = EXCLUDED.tipo
                RETURNING cnpj, razao_social, tipo, criado_em
            """, (u["sub"], digits, razao_social, req.tipo))
            empresa = dict(cur.fetchone())
            cur.execute("SELECT COUNT(*) AS c FROM matches_obra_prestador WHERE cnpj=%s", (digits,))
            empresa["obras_compativeis"] = cur.fetchone()["c"]
        conn.commit()
        return empresa
    finally:
        conn.close()

@app.delete("/api/minhas_empresas/{cnpj}")
def remover_minha_empresa(cnpj: str, u=Depends(requer_auth)):
    digits = _cnpj_digits(cnpj)
    if len(digits) != 14:
        raise HTTPException(400, "CNPJ inválido.")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE prestador_empresas SET ativo = false
                WHERE prestador_id = %s AND cnpj = %s AND ativo
                RETURNING cnpj
            """, (u["sub"], digits))
            if not cur.fetchone():
                raise HTTPException(404, "Empresa não vinculada à sua conta.")
        conn.commit()
        return {"ok": True, "cnpj": digits}
    finally:
        conn.close()

# --- Preview / desbloquear / pitch ---

class DesbloquearReq(BaseModel):
    cnpj_empresa: str

def _validar_uuid(oid):
    import uuid as _uuid
    try: _uuid.UUID(oid)
    except (ValueError, AttributeError):
        raise HTTPException(404, "Obra não encontrada")

@app.get("/api/obras/{oid}/desbloqueio_preview")
def desbloqueio_preview(oid: str, u=Depends(requer_auth)):
    _validar_uuid(oid)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, nome, valor_estimado, valor_formatado, empresa, uf, fase
                FROM obras WHERE id = %s
            """, (oid,))
            obra = cur.fetchone()
            if not obra:
                raise HTTPException(404, "Obra não encontrada")
            cur.execute("""
                SELECT id::text, cnpj_empresa, criado_em FROM desbloqueios
                WHERE prestador_id = %s AND obra_id = %s
            """, (u["sub"], oid))
            ja = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT cnpj, razao_social, tipo
                FROM prestador_empresas
                WHERE prestador_id = %s AND ativo
                ORDER BY criado_em
            """, (u["sub"],))
            empresas = [dict(r) for r in cur.fetchall()]
        saldo_atual = _saldo_wallet(conn, u["sub"])
        faixa_key, faixa = _faixa_da_obra(obra.get("valor_estimado"))
        preco = faixa["preco"]
        return {
            "obra_id": str(obra["id"]),
            "obra_nome": obra["nome"],
            "valor_estimado": float(obra["valor_estimado"]) if obra["valor_estimado"] else None,
            "valor_formatado": obra["valor_formatado"],
            "faixa": {"key": faixa_key, "label": faixa["label"], "preco_centavos": preco},
            "minhas_empresas": empresas,
            "preco_centavos": preco,
            "saldo_atual_centavos": saldo_atual,
            "saldo_suficiente": saldo_atual >= preco,
            "falta_centavos": max(0, preco - saldo_atual),
            "ja_desbloqueadas": ja,
        }
    finally:
        conn.close()

def _resposta_desbloqueio(conn, desb, obra, cnpj, u):
    saldo_atual = _saldo_wallet(conn, u["sub"])
    return {
        "ok": True,
        "desbloqueio_id": str(desb["id"]),
        "obra_id": str(obra["id"]),
        "obra_nome": obra["nome"],
        "cnpj_empresa": cnpj,
        "faixa_valor": desb["faixa_valor"],
        "valor_cobrado_centavos": desb["valor_cobrado"],
        "criado_em": desb["criado_em"].isoformat() if desb.get("criado_em") else None,
        "decisor": {
            "nome": obra.get("nivel1_nome"),
            "cargo": obra.get("nivel1_cargo"),
            "email": obra.get("nivel1_email"),
            "linkedin": obra.get("nivel1_linkedin"),
        },
        "pitch": desb["pitch_gerado"],
        "saldo_atual_centavos": saldo_atual,
    }

@app.post("/api/obras/{oid}/desbloquear")
def desbloquear_obra_v2(oid: str, req: DesbloquearReq, u=Depends(requer_auth)):
    _validar_uuid(oid)
    digits = _cnpj_digits(req.cnpj_empresa)
    if len(digits) != 14:
        raise HTTPException(400, "CNPJ inválido.")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT 1 FROM prestador_empresas WHERE prestador_id=%s AND cnpj=%s AND ativo",
                        (u["sub"], digits))
            if not cur.fetchone():
                raise HTTPException(403, "Esse CNPJ não está vinculado à sua conta. Adicione em /perfil.")
            cur.execute("""
                SELECT id, nome, empresa, valor_estimado, valor_formatado, uf, municipio, fase, setor,
                       nivel1_nome, nivel1_cargo, nivel1_email, nivel1_linkedin
                FROM obras WHERE id = %s
            """, (oid,))
            obra = cur.fetchone()
            if not obra:
                raise HTTPException(404, "Obra não encontrada")
            tem_decisor_ouro = (
                bool((obra.get("nivel1_nome") or "").strip())
                and (bool((obra.get("nivel1_email") or "").strip())
                     or bool((obra.get("nivel1_linkedin") or "").strip()))
                and _cargo_e_decisor(obra.get("nivel1_cargo"))
            )
            tem_decisor_cache = False
            if obra.get("cnpj"):
                cur.execute("""
                    SELECT 1 FROM empresa_decisores_cache
                    WHERE cnpj=%s AND trabalha_atualmente=true AND excluido_em IS NULL
                      AND filtro_llm_confianca IN ('alta','media')
                    LIMIT 1
                """, (obra["cnpj"],))
                tem_decisor_cache = cur.fetchone() is not None
            if not (tem_decisor_ouro or tem_decisor_cache):
                raise HTTPException(400, "Essa obra não tem decisores disponíveis.")
            cur.execute("""
                SELECT id, faixa_valor, valor_cobrado, pitch_gerado, criado_em
                FROM desbloqueios
                WHERE prestador_id = %s AND obra_id = %s AND cnpj_empresa = %s
            """, (u["sub"], oid, digits))
            ja = cur.fetchone()
        if ja:
            return _resposta_desbloqueio(conn, dict(ja), dict(obra), digits, u)
        faixa_key, faixa = _faixa_da_obra(obra.get("valor_estimado"))
        preco_centavos = faixa["preco"]
        saldo_atual = _saldo_wallet(conn, u["sub"])
        if saldo_atual < preco_centavos:
            return {
                "requer_pagamento": True,
                "obra_id": str(obra["id"]),
                "cnpj_empresa": digits,
                "faixa": {"key": faixa_key, "label": faixa["label"]},
                "preco_centavos": preco_centavos,
                "saldo_atual_centavos": saldo_atual,
                "falta_centavos": preco_centavos - saldo_atual,
                "motivo": "saldo_insuficiente",
            }
        pitch = _gerar_pitch_simples(dict(obra), digits, conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO desbloqueios (prestador_id, obra_id, cnpj_empresa, faixa_valor, valor_cobrado, pitch_gerado)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (prestador_id, obra_id, cnpj_empresa) DO UPDATE
                  SET pitch_gerado = EXCLUDED.pitch_gerado
                RETURNING id, faixa_valor, valor_cobrado, pitch_gerado, criado_em
            """, (u["sub"], oid, digits, faixa_key, preco_centavos, pitch))
            registro = dict(cur.fetchone())
            cur.execute(
                "UPDATE prestadores SET creditos_consumidos = COALESCE(creditos_consumidos,0) + %s WHERE id=%s",
                (preco_centavos, u["sub"]),
            )
            cur.execute(
                "INSERT INTO interacoes (obra_id, prestador_id, tipo, plano_momento, valor_cobrado) "
                "VALUES (%s, %s, 'DESBLOQUEIO', %s, %s)",
                (oid, u["sub"], (u.get("plano") or "GRATUITO"), preco_centavos),
            )
        conn.commit()
        resp = _resposta_desbloqueio(conn, registro, dict(obra), digits, u)
        if isinstance(resp, dict):
            resp.update({
                "pago_com_creditos": True,
                "creditos_usados_centavos": preco_centavos,
                "creditos_usados_reais": preco_centavos / 100,
                "saldo_restante_centavos": saldo_atual - preco_centavos,
                "saldo_restante_reais": (saldo_atual - preco_centavos) / 100,
            })
        return resp
    finally:
        conn.close()

@app.get("/api/obras/{oid}/pitch/{cnpj}")
def gerar_pitch_endpoint(oid: str, cnpj: str, u=Depends(requer_auth)):
    _validar_uuid(oid)
    digits = _cnpj_digits(cnpj)
    if len(digits) != 14:
        raise HTTPException(400, "CNPJ inválido.")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT 1 FROM desbloqueios
                WHERE prestador_id = %s AND obra_id = %s AND cnpj_empresa = %s
            """, (u["sub"], oid, digits))
            if not cur.fetchone():
                raise HTTPException(403, "Pitch disponível só após desbloqueio.")
            cur.execute("""
                SELECT id, nome, empresa, valor_estimado, valor_formatado, uf, fase, nivel1_nome
                FROM obras WHERE id = %s
            """, (oid,))
            obra = cur.fetchone()
            if not obra:
                raise HTTPException(404, "Obra não encontrada")
        pitch = _gerar_pitch_simples(dict(obra), digits, conn)
        return {"obra_id": oid, "cnpj_empresa": digits, "pitch": pitch}
    finally:
        conn.close()

app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")

@app.get("/ranking-megaobras")
async def ranking_megaobras_page():
    with open("/app/frontend/ranking-megaobras.html") as f: html = f.read()
    return HTMLResponse(html)

@app.get("/auto-match-demo", include_in_schema=False)
async def auto_match_demo_page():
    from fastapi.responses import FileResponse
    return FileResponse("/app/frontend/static/auto-match-demo.html")

@app.get("/{path:path}")
async def frontend(path:str=""):
    with open("/app/frontend/index.html") as f: html = f.read()
    return HTMLResponse(html)

if __name__=="__main__":
    import uvicorn
    uvicorn.run("main:app",host="0.0.0.0",port=int(os.getenv("PORT","8000")),workers=int(os.getenv("WORKERS","2")))


