"""
services/whatsapp_ai.py — Fallback chain de IA gratuita para WhatsApp.

Ordem: Groq -> Gemini -> OpenRouter -> template fixo.
Cada provedor tem timeout curto; na falha (rede, quota, 4xx/5xx) cai pro próximo.
gerar() SEMPRE retorna string (o template fixo nunca falha) — assim o fluxo de
atendimento nunca trava por causa de IA indisponível.

Chaves vêm do .env (já presentes): GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY.
"""
import logging
import os

import httpx

log = logging.getLogger("whatsapp.ai")

GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

GROQ_MODEL = os.getenv("WA_GROQ_MODEL", "llama-3.1-8b-instant")
GEMINI_MODEL = os.getenv("WA_GEMINI_MODEL", "gemini-1.5-flash")
OPENROUTER_MODEL = os.getenv("WA_OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free")

_TIMEOUT = float(os.getenv("WA_AI_TIMEOUT", "12"))


def _groq(system, user, max_tokens):
    if not GROQ_KEY:
        return None
    try:
        r = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={"model": GROQ_MODEL, "max_tokens": max_tokens, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}]},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            return (r.json()["choices"][0]["message"]["content"] or "").strip() or None
        log.warning("groq %s: %s", r.status_code, r.text[:120])
    except Exception as e:  # rede, json, chave inválida
        log.warning("groq erro: %s", e)
    return None


def _gemini(system, user, max_tokens):
    if not GEMINI_KEY:
        return None
    try:
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
                  "generationConfig": {"maxOutputTokens": max_tokens}},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            return (r.json()["candidates"][0]["content"]["parts"][0]["text"] or "").strip() or None
        log.warning("gemini %s: %s", r.status_code, r.text[:120])
    except Exception as e:
        log.warning("gemini erro: %s", e)
    return None


def _openrouter(system, user, max_tokens):
    if not OPENROUTER_KEY:
        return None
    try:
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            json={"model": OPENROUTER_MODEL, "max_tokens": max_tokens, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}]},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            return (r.json()["choices"][0]["message"]["content"] or "").strip() or None
        log.warning("openrouter %s: %s", r.status_code, r.text[:120])
    except Exception as e:
        log.warning("openrouter erro: %s", e)
    return None


def gerar(system: str, user: str, *, max_tokens: int = 200, template: str = "") -> str:
    """Groq -> Gemini -> OpenRouter -> template. Nunca lança; nunca retorna vazio
    se um template for fornecido."""
    for fn in (_groq, _gemini, _openrouter):
        out = fn(system, user, max_tokens)
        if out:
            log.info("resposta via %s", fn.__name__.lstrip("_"))
            return out
    log.info("todos provedores falharam -> template fixo")
    return template


def provedores_status() -> dict:
    """Diagnóstico: quais chaves estão presentes."""
    return {"groq": bool(GROQ_KEY), "gemini": bool(GEMINI_KEY), "openrouter": bool(OPENROUTER_KEY)}
