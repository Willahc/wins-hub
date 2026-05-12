"""
WiNS HUB — Reset de senha por email via Resend.
Modulo independente, importado pelo main.py.
"""
import os
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr

log = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM    = os.getenv("RESEND_FROM", "WiNS HUB <contato@winshubcomercial.com.br>")
APP_URL        = os.getenv("APP_URL", "https://winshubcomercial.com.br")
TOKEN_TTL_HOURS = 1


def build_router(get_conn, hash_senha):
    """Builder pattern: recebe a conexao DB e o hasher do main.py."""
    router = APIRouter()

    class EsqueciReq(BaseModel):
        email: EmailStr

    class ResetReq(BaseModel):
        token: str
        senha: str

    # ------------------------------------------------------------
    # POST /api/auth/esqueci  -> dispara email com link
    # ------------------------------------------------------------
    @router.post("/api/auth/esqueci")
    async def esqueci(req: EsqueciReq, request: Request, bt: BackgroundTasks):
        email = req.email.lower().strip()

        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, nome_empresa FROM prestadores WHERE LOWER(email)=%s AND ativo=TRUE",
                    (email,),
                )
                user = cur.fetchone()

            # IMPORTANTE: respondemos sucesso mesmo se nao existir,
            # pra nao revelar quais emails estao cadastrados
            if user:
                token = secrets.token_urlsafe(32)
                expires = datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)
                ip = (
                    request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                    or (request.client.host if request.client else None)
                )
                ua = request.headers.get("user-agent")

                with conn.cursor() as cur:
                    # Invalida tokens antigos do mesmo usuario
                    cur.execute(
                        "DELETE FROM password_resets WHERE user_id=%s",
                        (str(user["id"]),),
                    )
                    cur.execute(
                        """
                        INSERT INTO password_resets (user_id, token, expires_at, ip_origem, user_agent)
                        VALUES (%s, %s, %s, %s::inet, %s)
                        """,
                        (str(user["id"]), token, expires, ip, ua),
                    )
                conn.commit()

                # Envia email em background pra nao travar a resposta
                bt.add_task(
                    _enviar_email_reset,
                    email,
                    user["nome_empresa"] or "",
                    token,
                )
            else:
                log.info("Reset solicitado pra email nao cadastrado: %s", email)
        finally:
            conn.close()

        return {
            "ok": True,
            "mensagem": "Se o email estiver cadastrado, um link de recuperacao foi enviado.",
        }

    # ------------------------------------------------------------
    # GET /api/auth/reset/{token}  -> serve a tela de nova senha
    # ------------------------------------------------------------
    @router.get("/api/auth/reset/{token}")
    async def validar_token(token: str):
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT pr.id, pr.expires_at, pr.used_at, p.email
                    FROM password_resets pr
                    JOIN prestadores p ON p.id = pr.user_id
                    WHERE pr.token = %s
                    """,
                    (token,),
                )
                r = cur.fetchone()
        finally:
            conn.close()

        if not r:
            raise HTTPException(404, "Link invalido ou expirado.")
        if r["used_at"]:
            raise HTTPException(400, "Este link ja foi usado. Solicite um novo.")
        if r["expires_at"] < datetime.now(timezone.utc):
            raise HTTPException(400, "Link expirado. Solicite um novo.")

        return {"ok": True, "email": r["email"]}

    # ------------------------------------------------------------
    # POST /api/auth/reset  -> aplica a nova senha
    # ------------------------------------------------------------
    @router.post("/api/auth/reset")
    async def aplicar_reset(req: ResetReq):
        if len(req.senha) < 6:
            raise HTTPException(400, "Senha precisa ter pelo menos 6 caracteres.")

        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT pr.id, pr.user_id, pr.expires_at, pr.used_at
                    FROM password_resets pr
                    WHERE pr.token = %s
                    """,
                    (req.token,),
                )
                r = cur.fetchone()

            if not r:
                raise HTTPException(404, "Link invalido.")
            if r["used_at"]:
                raise HTTPException(400, "Este link ja foi usado.")
            if r["expires_at"] < datetime.now(timezone.utc):
                raise HTTPException(400, "Link expirado.")

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE prestadores SET senha_hash=%s WHERE id=%s",
                    (hash_senha(req.senha), str(r["user_id"])),
                )
                cur.execute(
                    "UPDATE password_resets SET used_at=NOW() WHERE id=%s",
                    (str(r["id"]),),
                )
            conn.commit()
        finally:
            conn.close()

        return {"ok": True, "mensagem": "Senha alterada com sucesso."}

    return router


# ----------------------------------------------------------------
# Funcao standalone que dispara email via Resend
# ----------------------------------------------------------------
def _enviar_email_reset(email: str, nome: str, token: str) -> None:
    if not RESEND_API_KEY:
        log.error("RESEND_API_KEY nao configurada. Email NAO enviado.")
        return

    link = f"{APP_URL}/reset?token={token}"
    saudacao = f"Ola{(' ' + nome) if nome else ''},"

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0a0e1a;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#e8ecf4;">
  <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#0a0e1a;">
    <tr>
      <td align="center" style="padding:40px 16px;">
        <table cellpadding="0" cellspacing="0" border="0" width="540" style="max-width:540px;background:#0f1525;border:1px solid #1f2942;border-radius:12px;">
          <tr>
            <td style="padding:32px 36px 8px 36px;">
              <span style="font-size:24px;font-weight:800;letter-spacing:1.5px;color:#f5b800;">WiNS HUB</span>
              <span style="font-size:11px;color:#6b7693;text-transform:uppercase;letter-spacing:1.2px;margin-left:8px;">Inteligencia Comercial</span>
            </td>
          </tr>
          <tr><td style="padding:24px 36px 8px 36px;">
            <h2 style="margin:0;font-size:18px;font-weight:600;color:#e8ecf4;">Recuperacao de senha</h2>
          </td></tr>
          <tr><td style="padding:8px 36px 16px 36px;color:#a8b1c9;font-size:14px;line-height:1.6;">
            <p style="margin:0 0 12px 0;">{saudacao}</p>
            <p style="margin:0 0 12px 0;">Recebemos uma solicitacao para redefinir a senha da sua conta no WiNS HUB. Clique no botao abaixo para criar uma nova senha:</p>
          </td></tr>
          <tr><td align="center" style="padding:8px 36px 24px 36px;">
            <a href="{link}" style="display:inline-block;background:#f5b800;color:#0a0e1a;text-decoration:none;padding:12px 28px;border-radius:6px;font-weight:700;letter-spacing:0.5px;">Redefinir senha</a>
          </td></tr>
          <tr><td style="padding:0 36px 24px 36px;color:#6b7693;font-size:12px;line-height:1.6;">
            <p style="margin:0 0 8px 0;">Ou copie este link no navegador:</p>
            <p style="margin:0 0 16px 0;word-break:break-all;color:#a8b1c9;">{link}</p>
            <p style="margin:0 0 8px 0;">Este link expira em <strong style="color:#a8b1c9;">1 hora</strong>.</p>
            <p style="margin:0;">Se voce nao solicitou essa recuperacao, ignore este email — sua senha continua a mesma.</p>
          </td></tr>
          <tr><td style="padding:16px 36px 24px 36px;border-top:1px solid #1f2942;color:#6b7693;font-size:11px;text-align:center;letter-spacing:0.3px;">
            WiNS HUB &middot; Inteligencia Comercial<br>
            <a href="https://winshubcomercial.com.br" style="color:#6b7693;text-decoration:none;">winshubcomercial.com.br</a>
          </td></tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    payload = {
        "from": RESEND_FROM,
        "to": [email],
        "subject": "Recuperacao de senha - WiNS HUB",
        "html": html,
    }
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post("https://api.resend.com/emails", json=payload, headers=headers)
        if r.status_code >= 400:
            log.error("Resend respondeu %s: %s", r.status_code, r.text)
        else:
            log.info("Email de reset enviado pra %s (Resend ID: %s)", email, r.json().get("id"))
    except Exception as exc:
        log.exception("Falha ao enviar email via Resend: %s", exc)
