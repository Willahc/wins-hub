"""
services/llm_haiku_compat.py — Shim do cliente Anthropic para rodar SEM saldo.

Free-first (25/06): a conta Anthropic ficou sem crédito. Quando
HAIKU_HABILITADO=false, _haiku_client() devolve um cliente FAKE cuja interface
imita anthropic.Anthropic (client.messages.create -> objeto com .content[0].text
e .usage.input_tokens/output_tokens) mas por baixo usa a cadeia LLM GRATUITA
(Groq 70B -> Gemini -> OpenRouter, em services/llm_extracao.completar).

Assim qualquer script que faça `client = _haiku_client()` e leia
`resp.content[0].text` funciona sem mais nenhuma mudança.

Voltar ao Haiku quando houver receita: HAIKU_HABILITADO=true (ou remover a var).
"""
import logging
import os

log = logging.getLogger("llm.haiku_compat")

HAIKU_OFF = os.getenv("HAIKU_HABILITADO", "true").strip().lower() in ("false", "0", "no", "off")


def _prompt_de(messages, system):
    partes = []
    if system:
        partes.append(system if isinstance(system, str) else str(system))
    for m in (messages or []):
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for b in c:
                partes.append(b.get("text", "") if isinstance(b, dict) else str(b))
        elif c:
            partes.append(c)
    return "\n\n".join(p for p in partes if p)


class _Bloco:
    def __init__(self, text):
        self.text = text
        self.type = "text"


class _Usage:
    input_tokens = 0
    output_tokens = 0


class _Resp:
    def __init__(self, text):
        self.content = [_Bloco(text)]
        self.usage = _Usage()
        self.stop_reason = "end_turn"
        self.role = "assistant"


class _Messages:
    def create(self, *, model=None, max_tokens=1024, messages=None, system=None, **kw):
        from services.llm_extracao import completar
        prompt = _prompt_de(messages, system)
        return _Resp(completar(prompt, max_tokens=max_tokens or 1024))


class FakeAnthropic:
    """Imita anthropic.Anthropic (síncrono) sobre a cadeia LLM gratuita."""
    def __init__(self, *a, **k):
        self.messages = _Messages()


class _AsyncMessages:
    async def create(self, *, model=None, max_tokens=1024, messages=None, system=None, **kw):
        from services.llm_extracao import completar
        prompt = _prompt_de(messages, system)
        return _Resp(completar(prompt, max_tokens=max_tokens or 1024))


class FakeAsyncAnthropic:
    def __init__(self, *a, **k):
        self.messages = _AsyncMessages()


def _haiku_client(*a, **k):
    if HAIKU_OFF:
        log.info("HAIKU_HABILITADO=false -> cliente LLM gratuito (Groq/Gemini/OpenRouter)")
        return FakeAnthropic()
    import anthropic
    return anthropic.Anthropic(*a, **k)


def _haiku_async_client(*a, **k):
    if HAIKU_OFF:
        log.info("HAIKU_HABILITADO=false -> cliente LLM gratuito async")
        return FakeAsyncAnthropic()
    import anthropic
    return anthropic.AsyncAnthropic(*a, **k)
