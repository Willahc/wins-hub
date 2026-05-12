"""Gerador de emails outbound via Claude Sonnet 4.6."""
import json
import logging

from sales_intelligence.llm_enricher.client import get_client, MODEL_SONNET
from .modelos import OutreachInput, OutreachOutput
from .prompts import OUTREACH_V2 as OUTREACH_DEFAULT

log = logging.getLogger("outreach.gerador")

# Pricing Sonnet 4.6: $3/MTok input, $15/MTok output
SONNET_INPUT_USD_PER_TOKEN = 3.0 / 1_000_000
SONNET_OUTPUT_USD_PER_TOKEN = 15.0 / 1_000_000


def _strip_md_fences(raw: str) -> str:
    """Remove ```json ... ``` ou ``` ... ``` wrappers se presentes."""
    s = raw.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl > 0:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _fallback_outreach(dados: OutreachInput, razao: str,
                       tokens_in: int = 0, tokens_out: int = 0,
                       custo: float = 0.0) -> OutreachOutput:
    """Template generico caso Sonnet falhe."""
    return OutreachOutput(
        assunto=f"Sobre {dados.obra_nome}"[:60],
        corpo=(f"{dados.nome_pessoa}, vi que a {dados.empresa_nome} "
               f"esta envolvida em {dados.obra_nome}. A {dados.fornecedor_nome} "
               f"oferece servicos que podem ser relevantes. "
               f"Podemos conversar brevemente?"),
        cta="Podemos conversar brevemente?",
        raciocinio=f"FALLBACK: {razao}",
        custo_usd=custo,
        tokens_input=tokens_in,
        tokens_output=tokens_out,
    )


def gerar_outreach(dados: OutreachInput) -> OutreachOutput:
    """Gera email outbound personalizado via Sonnet 4.6.

    Em caso de falha: retorna OutreachOutput com template fallback +
    raciocinio='FALLBACK: <motivo>'. Anti-alucinacao: sempre devolve
    objeto valido, nunca None nem exception.
    """
    prompt = OUTREACH_DEFAULT.format(
        nome_pessoa=dados.nome_pessoa,
        cargo_raw=dados.cargo_raw,
        empresa_nome=dados.empresa_nome,
        obra_nome=dados.obra_nome,
        obra_valor_formatado=dados.obra_valor_formatado,
        obra_fase=dados.obra_fase,
        obra_descricao=dados.obra_descricao or "(sem descricao detalhada)",
        obra_uf=dados.obra_uf or "(UF nao informada)",
        fornecedor_nome=dados.fornecedor_nome,
        fornecedor_servicos=", ".join(dados.fornecedor_servicos),
        fornecedor_referencias=", ".join(dados.fornecedor_referencias or []) or "(sem referencias listadas)",
        estilo=dados.estilo,
        idioma=dados.idioma,
    )

    try:
        client = get_client()
        r = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = r.content[0].text.strip()
        custo = (r.usage.input_tokens * SONNET_INPUT_USD_PER_TOKEN +
                 r.usage.output_tokens * SONNET_OUTPUT_USD_PER_TOKEN)

        raw_strip = _strip_md_fences(raw)
        try:
            parsed = json.loads(raw_strip)
        except json.JSONDecodeError as e:
            log.warning(f"JSON parse falhou: {raw[:300]}")
            return _fallback_outreach(
                dados, f"parse JSON: {e}",
                r.usage.input_tokens, r.usage.output_tokens, custo,
            )

        return OutreachOutput(
            assunto=parsed.get("assunto", f"Sobre {dados.obra_nome}")[:60],
            corpo=parsed.get("corpo", "(corpo vazio)"),
            cta=parsed.get("cta", "Podemos conversar brevemente?"),
            raciocinio=parsed.get("raciocinio", "(sem raciocinio)"),
            custo_usd=custo,
            tokens_input=r.usage.input_tokens,
            tokens_output=r.usage.output_tokens,
        )

    except Exception as e:
        log.error(f"Sonnet erro: {e}")
        return _fallback_outreach(dados, f"API erro: {str(e)[:120]}")
