"""Filtro: detecta se decisor ainda trabalha na empresa-alvo via Claude Haiku."""
import json
import logging
from .client import get_client, MODEL_HAIKU
from .modelos import DecisorInput, FiltroResult
from .prompts import FILTRO_EX_FUNCIONARIO_V1

log = logging.getLogger("llm_enricher.filtro")

# Pricing Haiku 4.5: $1/MTok input, $5/MTok output
HAIKU_INPUT_USD_PER_TOKEN = 1.0 / 1_000_000
HAIKU_OUTPUT_USD_PER_TOKEN = 5.0 / 1_000_000


def _strip_md_fences(raw: str) -> str:
    """Remove ```json ... ``` ou ``` ... ``` wrappers se presentes."""
    s = raw.strip()
    if s.startswith("```"):
        # remove primeira linha (ex: ```json) e ultima ``` se houver
        nl = s.find("\n")
        if nl > 0:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def filtrar_ex_funcionario(decisor: DecisorInput, empresa_alvo: str) -> FiltroResult:
    """Pergunta ao Claude Haiku se o decisor ainda trabalha na empresa-alvo.

    Em caso de erro/parse falho, retorna conservador (trabalha_atualmente=True, baixa).
    """
    prompt = FILTRO_EX_FUNCIONARIO_V1.format(
        nome_pessoa=decisor.nome_pessoa,
        cargo_raw=decisor.cargo_raw,
        snippet_origem=decisor.snippet_origem,
        empresa_alvo=empresa_alvo,
    )

    try:
        client = get_client()
        r = client.messages.create(
            model=MODEL_HAIKU,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = r.content[0].text.strip()
        custo = (r.usage.input_tokens * HAIKU_INPUT_USD_PER_TOKEN +
                 r.usage.output_tokens * HAIKU_OUTPUT_USD_PER_TOKEN)

        # Haiku as vezes envolve em ```json...```. Strip fences defensivamente.
        raw_strip = _strip_md_fences(raw)
        try:
            parsed = json.loads(raw_strip)
        except json.JSONDecodeError as e:
            log.warning(f"JSON parse falhou: {raw[:200]}")
            return FiltroResult(
                trabalha_atualmente=True,  # conservador
                confianca="baixa",
                razao=f"parse JSON falhou: {e}",
                custo_usd=custo,
                tokens_input=r.usage.input_tokens,
                tokens_output=r.usage.output_tokens,
            )

        return FiltroResult(
            trabalha_atualmente=parsed.get("trabalha_atualmente", True),
            confianca=parsed.get("confianca", "baixa"),
            empresa_atual_inferida=parsed.get("empresa_atual_inferida"),
            empresa_anterior_inferida=parsed.get("empresa_anterior_inferida"),
            razao=parsed.get("razao", "sem razao"),
            custo_usd=custo,
            tokens_input=r.usage.input_tokens,
            tokens_output=r.usage.output_tokens,
        )

    except Exception as e:
        log.error(f"filtro Claude erro: {e}")
        return FiltroResult(
            trabalha_atualmente=True,  # conservador
            confianca="baixa",
            razao=f"erro API: {str(e)[:100]}",
        )
