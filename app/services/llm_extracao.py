"""
services/llm_extracao.py — LLMs GRATUITOS (substituto do Haiku/Anthropic).

Free-first: quando a conta Anthropic está sem saldo, esta cadeia mantém o
pipeline funcionando. Ordem de fallback (todas free tier, chaves já no .env):
Groq 70B -> Gemini Flash -> OpenRouter 70B.

- extrair_json(prompt): força/parseia JSON (uso: extração de campos de notícia).
- completar(prompt): texto livre (uso: substituir chamadas Haiku genéricas).
Nenhuma lança; retornam None / "" na falha total.
"""
import json
import logging
import os
import re

import httpx

log = logging.getLogger("llm.extracao")

GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

GROQ_MODEL = os.getenv("EX_GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_MODEL = os.getenv("EX_GEMINI_MODEL", "gemini-2.5-flash")
OPENROUTER_MODEL = os.getenv("EX_OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

_TIMEOUT = float(os.getenv("EX_LLM_TIMEOUT", "30"))


def _parse_json(text):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _groq(prompt, max_tokens, json_mode):
    if not GROQ_KEY:
        return None
    body = {"model": GROQ_MODEL, "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "user", "content": prompt}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                       headers={"Authorization": f"Bearer {GROQ_KEY}"},
                       json=body, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        log.warning("groq %s: %s", r.status_code, r.text[:160])
    except Exception as e:
        log.warning("groq erro: %s", e)
    return None


def _gemini(prompt, max_tokens, json_mode):
    if not GEMINI_KEY:
        return None
    gen = {"maxOutputTokens": max_tokens, "temperature": 0}
    gen["thinkingConfig"] = {"thinkingBudget": 0}  # 2.5-flash: sem thinking
    if json_mode:
        gen["responseMimeType"] = "application/json"
    try:
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen},
            timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        log.warning("gemini %s: %s", r.status_code, r.text[:160])
    except Exception as e:
        log.warning("gemini erro: %s", e)
    return None


def _openrouter(prompt, max_tokens, json_mode):
    if not OPENROUTER_KEY:
        return None
    try:
        r = httpx.post("https://openrouter.ai/api/v1/chat/completions",
                       headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                       json={"model": OPENROUTER_MODEL, "max_tokens": max_tokens,
                             "temperature": 0,
                             "messages": [{"role": "user", "content": prompt}]},
                       timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        log.warning("openrouter %s: %s", r.status_code, r.text[:160])
    except Exception as e:
        log.warning("openrouter erro: %s", e)
    return None


def _cadeia(prompt, max_tokens, json_mode):
    for fn in (_groq, _gemini, _openrouter):
        raw = fn(prompt, max_tokens, json_mode)
        if raw:
            log.info("via %s", fn.__name__.lstrip("_"))
            return raw
    return None


def extrair_json(prompt: str, *, max_tokens: int = 700) -> dict | None:
    raw = _cadeia(prompt, max_tokens, True)
    return _parse_json(raw) if raw else None


def completar(prompt: str, *, max_tokens: int = 1024) -> str:
    """Texto livre via cadeia grátis. Retorna "" se todos falharem."""
    return _cadeia(prompt, max_tokens, False) or ""


def provedores_status() -> dict:
    return {"groq": bool(GROQ_KEY), "gemini": bool(GEMINI_KEY), "openrouter": bool(OPENROUTER_KEY)}
