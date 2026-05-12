"""SMTP RCPT TO probe (valida se email existe sem enviar mensagem).

CUIDADO: alguns servidores fazem greylisting/anti-bot e podem retornar
2xx para qualquer destinatario (catch-all) ou bloquear o IP de origem.
Estrategia conservadora:
- timeout curto (5s)
- 1 tentativa, sem retry
- fail-open: se erro de rede/bloqueio, retornar status='greylisted' (nao 'invalid')
"""
import logging
import smtplib
import socket
from typing import Tuple, Optional

log = logging.getLogger("sales_intel.validar_smtp")

# Email de origem que NAO existe (so pra MAIL FROM, nunca recebe nada).
# Dominio do WiNS Hub usado pra autoritar o probe.
PROBE_FROM = "noreply@winshubcomercial.com.br"
TIMEOUT_S = 5.0


def smtp_probe(email: str, mx_host: str) -> Tuple[str, Optional[int], Optional[str]]:
    """Probe SMTP (HELO/MAIL FROM/RCPT TO).
    Retorna (status, codigo_smtp, mensagem).
    status: 'verified_smtp' | 'invalid' | 'greylisted'."""
    if not email or not mx_host:
        return "greylisted", None, "sem mx ou email"

    try:
        with smtplib.SMTP(mx_host, 25, timeout=TIMEOUT_S) as smtp:
            smtp.helo("winshubcomercial.com.br")
            code_from, msg_from = smtp.mail(PROBE_FROM)
            if code_from >= 400:
                return "greylisted", code_from, str(msg_from)[:200]
            code_rcpt, msg_rcpt = smtp.rcpt(email)
            msg = (msg_rcpt.decode("utf-8", "ignore") if isinstance(msg_rcpt, bytes) else str(msg_rcpt))[:200]
            if 200 <= code_rcpt < 300:
                return "verified_smtp", code_rcpt, msg
            if code_rcpt in (550, 551, 553, 554):
                # User not found / mailbox unavailable
                return "invalid", code_rcpt, msg
            if code_rcpt in (421, 450, 451, 452):
                # tempfail / greylisting
                return "greylisted", code_rcpt, msg
            # outros 4xx/5xx: conservador, marcar greylisted
            return "greylisted", code_rcpt, msg
    except (socket.timeout, smtplib.SMTPServerDisconnected,
            smtplib.SMTPConnectError, ConnectionRefusedError):
        return "greylisted", None, "timeout/connection"
    except smtplib.SMTPException as e:
        log.debug(f"SMTPException probe {email}: {e}")
        return "greylisted", None, str(e)[:120]
    except Exception as e:
        log.debug(f"smtp_probe exception {email}: {e}")
        return "greylisted", None, str(e)[:120]
