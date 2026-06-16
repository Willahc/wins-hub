"""
routes/whatsapp.py — Webhook inbound (Z-API) — Mari, assistente comercial.

Tier 1: voz da Mari, intenções (ACESSO/SERVICO_MARI/DUVIDA/OBRAS/DECISOR/OUTRO),
preços atualizados (spec Mari v1.0 = fonte de verdade), Fluxo 2 (onboarding) e
Fluxo 3 (atendimento), fallbacks.
Tier 2 (parte 1): CNPJ -> consultar_cnpj_check (BrasilAPI/cache) -> confirma empresa
-> e-mail -> cria conta status='pendente' + e-mail de confirmação (reusa a infra
existente). SEM pagamento (PIX/aceite ficam pro próximo sprint, com humano no circuito).

Estado multi-turno em public.whatsapp_estado (passo + dados jsonb).
Webhook (painel Z-API): https://winshubcomercial.com.br/whatsapp/webhook?token=<ZAPI_VERIFY_TOKEN>
"""
import json
import logging
import re
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse
from psycopg2.extras import RealDictCursor

from services import whatsapp_zapi as wa
from services.whatsapp_ai import gerar

log = logging.getLogger("whatsapp.router")

INSTANCIA = "wins-comercial"
SITE = "winshubcomercial.com.br"

# ── Voz e tabelas (spec Mari v1.0 — fonte de verdade) ────────────────────────
PERSONA = (
    f"Você é a Mari, assistente comercial do WiNS Hub Comercial ({SITE}), plataforma "
    "de inteligência de obras industriais no Brasil (dados de decisores, CAPEX e fase "
    "de contratação em tempo real). Tom profissional e direto, B2B, sem rodeios, sem "
    "emojis excessivos. Português brasileiro.")
PLANOS_IA = (
    "Planos da plataforma: Gratuito (grátis); Essencial (R$ 297/mês); "
    "Profissional (R$ 697/mês — mais popular); Enterprise (R$ 1.997/mês, R$ 2.500 na "
    "wallet com bônus 25%). Serviços da Mari: Plano 2 — Lead Qualificado (R$ 500 na "
    "entrega + 1% do contrato se fechar); Plano 3 — Pacote Completo (R$ 1.500 + 1%); "
    "análise avançada de orçamento sobe a alíquota de sucesso para 2%.")

# ── Mensagens estáticas (spec Mari) ──────────────────────────────────────────
M2_1_WELCOME = (
    "Olá! Sou a *Mari*, assistente comercial do *WiNS Hub*. 👋\n\n"
    "O WiNS Hub conecta fornecedores industriais a megaobras no Brasil — com dados de "
    "decisores, CAPEX e fase de contratação em tempo real.\n\n"
    "O que você está buscando?\n\n"
    "1️⃣ *Acesso à plataforma* — você prospecta as obras direto pelo sistema\n"
    "2️⃣ *Indicação de leads* — eu mapeio as obras compatíveis e entrego os contatos pra você\n\n"
    "Responda com *1*, *2* ou descreva o que precisa.")
M2_2_PEDE_CNPJ = (
    "Para criar seu acesso, me manda o *CNPJ* da sua empresa.\n\n"
    "Vou puxar os dados direto da Receita Federal para agilizar o cadastro.")
M2_6_FALLBACK = (
    "Não entendi bem sua mensagem. Posso te ajudar com:\n\n"
    "1️⃣ Criar seu acesso à plataforma\n"
    "2️⃣ Tirar dúvidas sobre os planos\n"
    "3️⃣ Contratar o serviço de indicação de leads\n\n"
    "Com qual dessas você quer começar?")
M2_7_SERVICOS = (
    "Além da plataforma, ofereço dois serviços de inteligência comercial direto por aqui:\n\n"
    "🎯 *Plano 2 — Lead Qualificado*\n"
    "Mapeio as obras compatíveis com o seu perfil, você escolhe, e entrego os dados "
    "completos + contato direto do decisor.\n"
    "*R$ 500 na entrega + 1% do valor do contrato se fechar.*\n\n"
    "🚀 *Plano 3 — Pacote Completo*\n"
    "Tudo do Plano 2 + faço a ponte comercial: preparo o pitch, o e-mail e a apresentação "
    "da sua empresa para você mandar direto ao decisor.\n"
    "*R$ 1.500 na entrega + 1% do valor do contrato se fechar.*\n\n"
    "📊 *Análise avançada de orçamento:* se quiser que eu analise os dados da proposta para "
    "otimizar a negociação, a alíquota de sucesso sobe para *2%* nos dois planos.\n\n"
    "Qual faz mais sentido para o seu momento?")
M3_2_DECISOR = (
    "Os dados de contato dos decisores — e-mail verificado e telefone quando disponível — "
    "estão disponíveis a partir do plano *Profissional* (R$ 697/mês).\n\n"
    "O Profissional também inclui:\n"
    "✅ Match CNAE com score de compatibilidade\n"
    "✅ Score de janela de entrada\n"
    "✅ LinkedIn do decisor\n\n"
    "Ou, se preferir sem assinatura, posso mapear as obras e entregar os contatos direto "
    "aqui pelo *Plano 2* — R$ 500 por entrega + 1% se fechar.\n\n"
    "Como prefere seguir?")
M3_3_SERVICO = (
    "Claro! Posso trabalhar direto aqui pelo WhatsApp.\n\n"
    "🎯 *Plano 2 — Lead Qualificado:* R$ 500 + 1% se fechar\n"
    "🚀 *Plano 3 — Pacote Completo:* R$ 1.500 + 1% se fechar (inclui pitch + e-mail + apresentação)\n\n"
    "Qual dos dois faz mais sentido para você agora?")
M3_5_FALLBACK_TOTAL = (
    "Olá! Estou com uma instabilidade técnica no momento, mas sua mensagem já foi registrada.\n\n"
    f"Para acesso imediato à plataforma: *{SITE}*\n\n"
    "Em breve retorno. Obrigada pela paciência!")

# ── System prompts dinâmicos ─────────────────────────────────────────────────
SYS_DUVIDA = (
    PERSONA + " Responda a dúvida em no máximo 3 parágrafos curtos. " + PLANOS_IA +
    " Use sempre esses valores ao citar planos/serviços; nunca invente preços. "
    "Finalize sempre com uma próxima ação clara.")
SYS_OBRAS = (
    PERSONA + " O usuário é fornecedor cadastrado. Use APENAS as obras da lista de "
    "contexto (nunca invente). Liste no máximo 3, no formato: "
    "'📌 *{nome}* — {empresa}\\n💰 R$ {capex} | 📍 {municipio}/{uf} | 🔄 {fase}'. "
    "Depois informe que os contatos dos decisores (e-mail verificado + telefone quando "
    f"disponível) estão no plano Profissional (R$ 697/mês) ou via Plano 2 da Mari (R$ 500). "
    "Sem introdução longa.")
SYS_GENERICA = (
    PERSONA + " O usuário já tem cadastro. Responda direto, no máximo 2 parágrafos. "
    "Nunca invente dados sobre obras, valores ou empresas. Se não souber com certeza, "
    f"direcione para {SITE} ou ofereça encaminhar para o time.")
SYS_INTENT = (
    PERSONA + " Classifique a intenção da mensagem do usuário em UMA palavra, dentre: "
    "ACESSO (quer acesso/cadastro na plataforma), SERVICO_MARI (quer que a Mari mapeie e "
    "entregue leads), DUVIDA (pergunta sobre planos/produto/como funciona), OBRAS (quer ver "
    "obras/oportunidades), DECISOR (quer contato de decisor), OUTRO. Responda só a palavra.")

# ── Helpers puros ────────────────────────────────────────────────────────────
UFS = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG",
       "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}
CNPJ_RE = re.compile(r"\d[\d.\-/]{12,17}\d")
EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w{2,}")


def _detect_uf(texto):
    for tok in re.findall(r"\b[A-Za-z]{2}\b", texto or ""):
        if tok.upper() in UFS:
            return tok.upper()
    return None


def _capex(v):
    if not v:
        return "capex n/d"
    v = float(v)
    if v >= 1e9:
        return f"R$ {v / 1e9:.1f} bi"
    if v >= 1e6:
        return f"R$ {v / 1e6:.0f} mi"
    return f"R$ {v:,.0f}"


def extrai_cnpj(texto):
    for m in CNPJ_RE.finditer(texto or ""):
        d = re.sub(r"\D", "", m.group(0))
        if len(d) == 14:
            return d
    return None


def extrai_email(texto):
    m = EMAIL_RE.search(texto or "")
    return m.group(0).lower() if m else None


def classificar(mensagem):
    m = (mensagem or "").strip().lower()
    if m in ("1", "1️⃣"):
        return "ACESSO"
    if m in ("2", "2️⃣"):
        return "SERVICO_MARI"
    out = gerar(SYS_INTENT, f"Mensagem: '{mensagem}'", max_tokens=4, template="OUTRO").upper()
    for k in ("ACESSO", "CADASTRO", "SERVICO_MARI", "SERVICO", "DUVIDA", "OBRAS", "DECISOR"):
        if k in out:
            if k == "CADASTRO":
                return "ACESSO"
            if k == "SERVICO":
                return "SERVICO_MARI"
            return k
    return "OUTRO"


def _msg_confirma_empresa(dp):
    return ("Encontrei o cadastro:\n\n"
            f"🏢 *{dp.get('razao_social') or 'Empresa'}*\n"
            f"📍 {dp.get('municipio') or '--'} — {dp.get('uf') or '--'}\n"
            f"🏭 {dp.get('cnae_descricao') or '--'}\n\n"
            "É essa a empresa? Se sim, me manda seu *e-mail corporativo* para finalizar o acesso.")


def _msg_conta_criada():
    return ("Acesso criado! ✅\n\n"
            "Te enviei um *e-mail de confirmação* — clique no link para ativar a conta. "
            f"Depois, defina sua senha em *{SITE}* (opção \"Esqueci minha senha\").\n\n"
            "Seu plano inicial é o *Gratuito* (vitrine de obras + contagem de decisores).\n\n"
            "Quando quiser o contato direto dos decisores, dá pra subir pro *Profissional* "
            "(R$ 697/mês) ou pedir uma indicação de leads comigo (Plano 2 — R$ 500). É só falar.")


def _email_boasvindas_whatsapp(email, nome, confirm_token, reset_token):
    """Boas-vindas p/ cadastro via WhatsApp: ação principal = criar senha (o login já
    tem ativo=TRUE, falta a senha); secundária = confirmar e-mail (status)."""
    from main import enviar_email, APP_URL  # lazy: evita import circular
    reset_link = f"{APP_URL}/reset?token={reset_token}"
    confirm_link = f"{APP_URL}/confirmar/{confirm_token}"
    saud = f"Ola{(' ' + nome) if nome else ''},"
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
          <h2 style="margin:0;font-size:18px;font-weight:600;color:#e8ecf4;">Bem-vindo! Crie sua senha</h2>
        </td></tr>
        <tr><td style="padding:8px 36px 16px 36px;color:#a8b1c9;font-size:14px;line-height:1.6;">
          <p style="margin:0 0 12px 0;">{saud}</p>
          <p style="margin:0 0 12px 0;">Sua conta no WiNS HUB foi criada no plano <strong style="color:#e8ecf4;">Gratuito</strong>. Para acessar a plataforma, crie sua senha no botao abaixo:</p>
        </td></tr>
        <tr><td align="center" style="padding:8px 36px 20px 36px;">
          <a href="{reset_link}" style="display:inline-block;background:#f5b800;color:#0a0e1a;text-decoration:none;padding:12px 28px;border-radius:6px;font-weight:700;letter-spacing:0.5px;">Criar minha senha</a>
        </td></tr>
        <tr><td style="padding:0 36px 20px 36px;color:#6b7693;font-size:12px;line-height:1.6;">
          <p style="margin:0 0 8px 0;">Ou copie este link no navegador:</p>
          <p style="margin:0 0 16px 0;word-break:break-all;color:#a8b1c9;">{reset_link}</p>
          <p style="margin:0 0 8px 0;">Este link expira em <strong style="color:#a8b1c9;">48 horas</strong>. Depois de criar a senha, entre em <a href="{APP_URL}/login" style="color:#f5b800;text-decoration:none;">winshubcomercial.com.br</a> com seu e-mail.</p>
        </td></tr>
        <tr><td style="padding:0 36px 24px 36px;color:#6b7693;font-size:12px;line-height:1.6;border-top:1px solid #1f2942;padding-top:16px;">
          <p style="margin:0;">Opcional: <a href="{confirm_link}" style="color:#a8b1c9;">confirmar este e-mail</a> (marca seu contato como verificado). Se voce nao se cadastrou, ignore esta mensagem.</p>
        </td></tr>
        <tr><td style="padding:16px 36px 24px 36px;border-top:1px solid #1f2942;color:#6b7693;font-size:11px;text-align:center;letter-spacing:0.3px;">
          WiNS HUB &middot; Inteligencia Comercial<br>
          <a href="https://winshubcomercial.com.br" style="color:#6b7693;text-decoration:none;">winshubcomercial.com.br</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    enviar_email(email, "Bem-vindo ao WiNS HUB — crie sua senha", html, contexto="Boas-vindas WhatsApp")


def build_router(get_conn):
    router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

    # ── persistência / estado ────────────────────────────────────────────
    def _salvar(numero, role, mensagem):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO whatsapp_conversas (numero,instancia,role,mensagem) "
                            "VALUES (%s,%s,%s,%s)", (numero, INSTANCIA, role, mensagem))
            conn.commit()
        except Exception as e:
            log.warning("salvar conversa erro: %s", e)
        finally:
            conn.close()

    def _conta_msgs(numero):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM whatsapp_conversas WHERE numero=%s", (numero,))
                return cur.fetchone()[0]
        finally:
            conn.close()

    def _historico(numero, limite=6):
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT role, mensagem FROM whatsapp_conversas WHERE numero=%s "
                            "ORDER BY criado_em DESC LIMIT %s", (numero, limite))
                return list(reversed(cur.fetchall()))
        finally:
            conn.close()

    def _get_estado(numero):
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT passo, dados FROM whatsapp_estado WHERE numero=%s", (numero,))
                return cur.fetchone()
        finally:
            conn.close()

    def _set_estado(numero, passo, dados):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO whatsapp_estado (numero,passo,dados,atualizado_em) "
                    "VALUES (%s,%s,%s,NOW()) ON CONFLICT (numero) DO UPDATE SET "
                    "passo=EXCLUDED.passo, dados=EXCLUDED.dados, atualizado_em=NOW()",
                    (numero, passo, json.dumps(dados)))
            conn.commit()
        finally:
            conn.close()

    def _clear_estado(numero):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM whatsapp_estado WHERE numero=%s", (numero,))
            conn.commit()
        finally:
            conn.close()

    def _prestador(numero):
        ult8 = "".join(filter(str.isdigit, numero))[-8:]
        if len(ult8) < 8:
            return None
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, nome_empresa, plano, uf, segmento FROM prestadores "
                    "WHERE regexp_replace(COALESCE(telefone,'')||COALESCE(telefone_comercial,''),"
                    "'\\D','','g') LIKE %s AND COALESCE(status,'ativo')<>'pendente' LIMIT 1",
                    (f"%{ult8}%",))
                return cur.fetchone()
        except Exception as e:
            log.warning("lookup prestador erro: %s", e)
            return None
        finally:
            conn.close()

    def _buscar_obras(mensagem, uf_prest=None, limite=8):
        uf = _detect_uf(mensagem) or uf_prest
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                sql = ("SELECT nome, empresa, setor, uf, valor_estimado, fase FROM obras "
                       "WHERE classificacao_computed IN ('OURO','PRATA') AND visivel=true ")
                params = []
                if uf:
                    sql += "AND uf=%s "
                    params.append(uf)
                sql += "ORDER BY valor_estimado DESC NULLS LAST LIMIT %s"
                params.append(limite)
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()

    # ── Tier 2: CNPJ -> conta ────────────────────────────────────────────
    def _consultar_cnpj(cnpj):
        from main import consultar_cnpj_check  # lazy: evita import circular
        res = consultar_cnpj_check(cnpj)
        if isinstance(res, dict):
            return res
        # consultar_cnpj_check devolve JSONResponse quando ja_tem_prestador=True
        body = getattr(res, "body", None)
        if body:
            try:
                return json.loads(body)
            except Exception:
                pass
        return {"ja_tem_prestador": True}

    def _criar_conta(cnpj, email, dados):
        """Cria prestador + token de reset (criar senha) + e-mail de boas-vindas."""
        import bcrypt
        nome = dados.get("razao") or f"CNPJ {cnpj}"
        token = secrets.token_urlsafe(32)
        expiry = datetime.utcnow() + timedelta(hours=24)
        senha_hash = bcrypt.hashpw(secrets.token_urlsafe(9).encode(), bcrypt.gensalt()).decode()
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM prestadores WHERE email=%s", (email,))
                if cur.fetchone():
                    return ("email_existe", None)
                cur.execute("SELECT nome_empresa FROM prestadores WHERE cnpj=%s "
                            "AND excluido_em IS NULL", (cnpj,))
                r = cur.fetchone()
                if r:
                    return ("cnpj_existe", r[0])
                cur.execute(
                    "INSERT INTO prestadores (nome_empresa,razao_social,email,senha_hash,"
                    "telefone,cnpj,uf,plano,status,email_token,email_token_expiry,source,"
                    "senha_temporaria) VALUES (%s,%s,%s,%s,%s,%s,%s,'GRATUITO','pendente',"
                    "%s,%s,'whatsapp',true) RETURNING id",
                    (nome, dados.get("razao"), email, senha_hash, dados.get("numero"),
                     cnpj, dados.get("uf"), token, expiry))
                pid = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO prestador_empresas (prestador_id,cnpj,razao_social,tipo,ativo) "
                    "VALUES (%s,%s,%s,'proprio',true) ON CONFLICT (prestador_id,cnpj) DO NOTHING",
                    (str(pid), cnpj, nome))
                # token de reset p/ o usuário CRIAR a senha (login já exige só ativo=TRUE)
                reset_token = secrets.token_urlsafe(32)
                cur.execute(
                    "INSERT INTO password_resets (user_id, token, expires_at, ip_origem, user_agent) "
                    "VALUES (%s,%s,%s,'whatsapp','whatsapp-onboarding')",
                    (str(pid), reset_token, datetime.utcnow() + timedelta(hours=48)))
            conn.commit()
            try:
                _email_boasvindas_whatsapp(email, nome, token, reset_token)
            except Exception as e:
                log.warning("email boas-vindas falhou: %s", e)
            return ("ok", None)
        except Exception as e:
            conn.rollback()
            log.exception("criar_conta erro: %s", e)
            return ("erro", None)
        finally:
            conn.close()

    # ── Fluxos ───────────────────────────────────────────────────────────
    def _fluxo_onboarding(numero, mensagem, nome):
        # primeira mensagem do número -> boas-vindas (M2-1)
        if _conta_msgs(numero) <= 1:
            _clear_estado(numero)
            return M2_1_WELCOME

        estado = _get_estado(numero) or {}
        passo = estado.get("passo")
        dados = estado.get("dados") or {}

        # aguardando e-mail (após confirmar empresa)
        if passo == "aguardando_email":
            email = extrai_email(mensagem)
            if not email:
                return "Não reconheci um e-mail válido. Pode me mandar seu *e-mail corporativo*?"
            dados["numero"] = numero
            status, info = _criar_conta(dados.get("cnpj"), email, dados)
            _clear_estado(numero)
            if status == "ok":
                return _msg_conta_criada()
            if status == "email_existe":
                return f"Esse e-mail já tem conta no WiNS Hub. Faça login ou recupere a senha em {SITE}."
            if status == "cnpj_existe":
                return (f"Esse CNPJ já está cadastrado (empresa: {info}). "
                        f"Faça login ou recupere a senha em {SITE}.")
            return "Tive um problema ao criar a conta agora. Pode tentar de novo em instantes?"

        # CNPJ informado (ou estávamos esperando CNPJ)
        cnpj = extrai_cnpj(mensagem)
        if cnpj or passo == "aguardando_cnpj":
            if not cnpj:
                return M2_2_PEDE_CNPJ
            try:
                chk = _consultar_cnpj(cnpj)
            except Exception as e:
                log.info("consultar_cnpj falhou (%s): %s", cnpj, e)
                return ("Não encontrei esse CNPJ ativo na Receita Federal. "
                        "Confere os números e me manda de novo?")
            if chk.get("ja_tem_prestador"):
                _clear_estado(numero)
                return (f"Esse CNPJ já tem conta no WiNS Hub. "
                        f"Faça login ou recupere a senha em {SITE}.")
            dp = chk.get("dados_publicos") or {}
            _set_estado(numero, "aguardando_email",
                        {"cnpj": cnpj, "razao": dp.get("razao_social"), "uf": dp.get("uf")})
            return _msg_confirma_empresa(dp)

        # senão, classifica intenção
        intent = classificar(mensagem)
        if intent == "ACESSO":
            _set_estado(numero, "aguardando_cnpj", {})
            return M2_2_PEDE_CNPJ
        if intent == "SERVICO_MARI":
            return M2_7_SERVICOS
        if intent == "DUVIDA":
            return gerar(SYS_DUVIDA, mensagem, max_tokens=260, template=M2_6_FALLBACK)
        return M2_6_FALLBACK

    def _fluxo_atendimento(numero, mensagem, prest):
        intent = classificar(mensagem)
        if intent == "OBRAS":
            obras = _buscar_obras(mensagem, uf_prest=prest.get("uf"))
            ctx = "\n".join(
                f"- {o['nome']} | {o['empresa']} | {o.get('setor') or 'setor n/d'} | "
                f"{o.get('uf') or '--'} | {_capex(o.get('valor_estimado'))} | fase {o.get('fase') or 'n/d'}"
                for o in obras) or "(nenhuma obra no contexto)"
            user = (f"Setor do fornecedor: {prest.get('segmento') or 'n/d'}. "
                    f"Obras disponíveis no banco (use só estas, no máx 3):\n{ctx}\n\n"
                    f"Mensagem: '{mensagem}'")
            return gerar(SYS_OBRAS, user, max_tokens=260,
                         template=f"Temos obras OURO/PRATA no seu perfil. Acesse {SITE} para ver as recomendadas.")
        if intent == "DECISOR":
            plano = (prest.get("plano") or "").upper()
            if plano in ("PROFISSIONAL", "ENTERPRISE"):
                return (f"Os contatos dos decisores estão liberados no seu plano *{plano.title()}* — "
                        f"acesse {SITE} para ver e-mail verificado e telefone (quando disponível).")
            return M3_2_DECISOR
        if intent == "SERVICO_MARI":
            return M3_3_SERVICO
        hist = _historico(numero)
        conversa = "\n".join(f"{h['role']}: {h['mensagem']}" for h in hist)
        return gerar(SYS_GENERICA, f"Histórico recente:\n{conversa}\n\nMensagem: '{mensagem}'",
                     max_tokens=200, template=M3_5_FALLBACK_TOTAL)

    def _processar(numero, mensagem, nome):
        if not mensagem:
            return
        try:
            _salvar(numero, "user", mensagem)
            prest = _prestador(numero)
            resp = _fluxo_atendimento(numero, mensagem, prest) if prest \
                else _fluxo_onboarding(numero, mensagem, nome)
            wa.send_text(numero, resp)
            _salvar(numero, "assistant", resp)
        except Exception as e:
            log.exception("processar inbound erro: %s", e)

    # ── endpoints ────────────────────────────────────────────────────────
    @router.post("/webhook")
    async def receive(request: Request, bg: BackgroundTasks):
        if not wa.webhook_authorized(request.query_params.get("token", "")):
            return PlainTextResponse("forbidden", status_code=403)
        try:
            payload = await request.json()
        except Exception:
            return {"status": "ignored"}
        for msg in wa.parse_inbound(payload):
            bg.add_task(_processar, msg["numero"], msg["mensagem"], msg["nome"])
        return {"status": "received"}

    @router.get("/health")
    async def health():
        from services.whatsapp_ai import provedores_status
        return {"zapi_configurada": wa.is_configured(), "ia": provedores_status()}

    return router
