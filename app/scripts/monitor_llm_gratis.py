#!/usr/bin/env python3
"""
monitor_llm_gratis.py — Saúde da cadeia LLM GRATUITA (Groq/Gemini/OpenRouter)
que substitui o Haiku enquanto a conta Anthropic está sem saldo
(ver HAIKU_HABILITADO em services/llm_extracao / llm_haiku_compat).

Testa cada provedor com uma chamada mínima e classifica:
  - OK        : >=1 provedor respondendo  (pipeline de notícias/enrichment vivo)
  - DEGRADADO : provedor primário (groq) caiu mas há fallback
  - CRITICO   : os 3 provedores fora -> extração/enrichment via LLM PARADO

DRY-RUN por padrão. Com --commit envia email Resend quando CRITICO (ou DEGRADADO
se --alerta-degradado). Roda barato (3 chamadas curtas); seguro p/ cron diário.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/app")
from services.llm_extracao import _groq, _gemini, _openrouter  # noqa: E402

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_FROM = os.environ.get("RESEND_FROM", "WiNS HUB <contato@winshubcomercial.com.br>")
ALERT_TO = os.environ.get("QUOTA_ALERT_TO", "williamvnvn@gmail.com")

PROMPT = 'Responda SOMENTE com este JSON, nada mais: {"ping": "ok"}'


def testar_provedores():
    res = {}
    for nome, fn in (("groq", _groq), ("gemini", _gemini), ("openrouter", _openrouter)):
        t0 = time.time()
        try:
            out = fn(PROMPT, 30, True)
            res[nome] = {"ok": bool(out and out.strip()), "ms": int((time.time() - t0) * 1000)}
        except Exception as e:  # pragma: no cover
            res[nome] = {"ok": False, "ms": int((time.time() - t0) * 1000), "erro": str(e)[:120]}
    return res


def enviar_alerta(status, res):
    if not RESEND_API_KEY:
        print("RESEND_API_KEY ausente — skipping email", file=sys.stderr)
        return False
    linhas = "".join(
        f"<li>{n}: {'✅ OK' if v['ok'] else '❌ FORA'} ({v['ms']}ms)"
        f"{' — ' + v['erro'] if v.get('erro') else ''}</li>"
        for n, v in res.items()
    )
    subject = f"{'🔴' if status=='CRITICO' else '🟡'} WiNS HUB — LLM gratuito {status}"
    body = (
        f"<p>A cadeia LLM gratuita que substitui o Haiku está <b>{status}</b>.</p>"
        f"<ul>{linhas}</ul>"
        f"<p>{'CRÍTICO: extração de notícias e enrichment via LLM estão PARADOS. ' if status=='CRITICO' else ''}"
        f"Verifique limites de free tier (Groq/Gemini/OpenRouter) ou considere religar o Haiku pago "
        f"(HAIKU_HABILITADO=true no .env + recriar container).</p>"
    )
    payload = json.dumps({"from": RESEND_FROM, "to": [ALERT_TO],
                          "subject": subject, "html": body}).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=payload,
                                 headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                                          "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"Email Resend OK -> {ALERT_TO}: {r.status}")
            return True
    except urllib.error.HTTPError as e:
        print(f"Resend HTTPError {e.code}: {e.read()[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Resend erro: {e}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="Envia email Resend em CRITICO. Sem flag = dry-run.")
    ap.add_argument("--alerta-degradado", action="store_true", help="Também alerta em DEGRADADO.")
    a = ap.parse_args()

    res = testar_provedores()
    ok = sum(1 for v in res.values() if v["ok"])
    if ok == 0:
        status = "CRITICO"
    elif not res["groq"]["ok"]:
        status = "DEGRADADO"
    else:
        status = "OK"

    print(f"MONITOR_LLM: status={status} provedores_ok={ok}/3 | {json.dumps(res)}")

    deve_alertar = status == "CRITICO" or (status == "DEGRADADO" and a.alerta_degradado)
    if deve_alertar:
        if a.commit:
            enviar_alerta(status, res)
        else:
            print(f"DRY-RUN: status={status} — alerta pendente (use --commit p/ enviar).")
    sys.exit(0)


if __name__ == "__main__":
    main()
