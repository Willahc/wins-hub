"""
services/whatsapp_zapi.py — Cliente Z-API (substitui o cliente Meta Cloud API).

Z-API coloca instância+token na URL; o Client-Token (token de segurança da conta)
vai no header e é OPCIONAL (só usado se ZAPI_CLIENT_TOKEN existir no .env).

.env:
  ZAPI_INSTANCE_ID   id da instância
  ZAPI_TOKEN         token da instância
  ZAPI_CLIENT_TOKEN  (opcional) token de segurança da conta -> header Client-Token
  ZAPI_VERIFY_TOKEN  (opcional) segredo p/ proteger o webhook via ?token=<ele>

Envio:   POST {BASE}/instances/{id}/token/{token}/send-text  body {phone, message}
Receção: Z-API faz POST na URL configurada no painel; sem GET de verificação.
"""
import logging
import os

import httpx

log = logging.getLogger("whatsapp.zapi")

INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID", "").strip()
TOKEN = os.getenv("ZAPI_TOKEN", "").strip()
CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "").strip()
VERIFY_TOKEN = os.getenv("ZAPI_VERIFY_TOKEN", "").strip()
BASE = os.getenv("ZAPI_BASE", "https://api.z-api.io").rstrip("/")


def is_configured() -> bool:
    return bool(INSTANCE_ID and TOKEN)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if CLIENT_TOKEN:
        h["Client-Token"] = CLIENT_TOKEN
    return h


def webhook_authorized(token_query: str) -> bool:
    """Se ZAPI_VERIFY_TOKEN estiver setado, exige ?token=<ele> no webhook.
    Sem o env setado, libera (não quebra um setup de URL simples)."""
    if not VERIFY_TOKEN:
        return True
    return token_query == VERIFY_TOKEN


def parse_inbound(payload: dict) -> list:
    """Extrai mensagens de texto recebidas. Ignora fromMe=True (eco das nossas
    próprias mensagens) e callbacks sem texto (status, conexão, etc.)."""
    if not isinstance(payload, dict) or payload.get("fromMe"):
        return []
    numero = str(payload.get("phone", "")).strip()
    # corpo pode vir como 'body' (formato simples) ou 'text.message' (formato Z-API)
    corpo = payload.get("body")
    if not corpo:
        txt = payload.get("text")
        if isinstance(txt, dict):
            corpo = txt.get("message") or txt.get("body")
        elif isinstance(txt, str):
            corpo = txt
    corpo = (corpo or "").strip()
    nome = payload.get("senderName") or payload.get("chatName") or ""
    if not numero or not corpo:
        return []
    return [{"numero": numero, "mensagem": corpo, "nome": nome}]


def send_text(numero: str, texto: str) -> dict:
    """Envia texto via Z-API. No-op se a instância não estiver configurada."""
    if not is_configured():
        log.info("send_text PENDENTE (Z-API não configurada) -> %s: %s", numero, texto[:60])
        return {"_pendente": True}
    url = f"{BASE}/instances/{INSTANCE_ID}/token/{TOKEN}/send-text"
    try:
        r = httpx.post(url, headers=_headers(),
                       json={"phone": numero, "message": texto}, timeout=20)
        if r.status_code in (200, 201):
            return r.json() if r.content else {"ok": True}
        log.warning("send_text %s: %s", r.status_code, r.text[:200])
        return {"_erro": r.status_code, "body": r.text[:200]}
    except Exception as e:
        log.warning("send_text erro: %s", e)
        return {"_erro": str(e)[:80]}
