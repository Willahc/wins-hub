"""
frente_c_haiku_v2.py — Validação Haiku 4.5 de decisores single-obra score<threshold.

Melhorias vs v1:
- asyncio.gather: calls em paralelo (~3s vs 39s)
- Prompt 60% menor (task binária não precisa de contexto extenso)
- Cache: pula decisores já avaliados (haiku_validation em componentes)
- Threshold dinâmico via --min-score e --min-confidence
- Reutilizável: função validate_batch() importável pelo orchestrator
- Retry com backoff em 429/500
- Dry-run por padrão, --commit explícito

Schema-aware: decisores_obra tem nome/cargo/linkedin_url/email (não nivel1_*).

Uso:
  python3 frente_c_haiku_v2.py                         # dry-run, threshold padrão
  python3 frente_c_haiku_v2.py --commit                # aplica score + reclassifica
  python3 frente_c_haiku_v2.py --min-score 5 --min-confidence 90 --commit
  python3 frente_c_haiku_v2.py --limit 50 --commit     # batch maior
  python3 frente_c_haiku_v2.py --source '' --commit    # qualquer fonte (default só enrichment_19_05)
"""

import argparse
import sys as _scompat
if "/app" not in _scompat.path: _scompat.path.insert(0, "/app")
from services.llm_haiku_compat import _haiku_client, _haiku_async_client  # free-first 25/06
import asyncio
import json
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import anthropic
import asyncpg

# ─── Config ──────────────────────────────────────────────────────────────────

def _build_dsn() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    user = urllib.parse.quote_plus(os.environ.get("DB_USER", "postgres"))
    pwd = urllib.parse.quote_plus(os.environ.get("DB_PASSWORD", ""))
    host = os.environ.get("DB_HOST", "db")
    port = os.environ.get("DB_PORT", "5432")
    db = os.environ.get("DB_NAME", "wins_hub")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"

DATABASE_URL = _build_dsn()
MODEL = "claude-haiku-4-5-20251001"
MAX_CONCURRENT = 8
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Prompt (SYSTEM + USER separados — bordas claras instrução vs dados) ─────

SYSTEM_PROMPT = """\
Você é auditor de match decisor↔obra. Decide se um decisor (nome + cargo) atua EXATAMENTE na empresa-alvo da obra.

REGRA: Match=true SOMENTE se o cargo do decisor menciona literalmente a empresa-alvo da obra (nome, fragmento distintivo ou sigla DELA).

CRÍTICO: o cargo pode mencionar uma OUTRA empresa diferente da empresa-alvo. NESSE CASO match=false — o decisor trabalha em outra empresa, NÃO na empresa-alvo.

Contra-exemplos (match=false):
- Cargo "Eng de projetos na CTEEP" + obra "NEOENERGIA GUANABARA TRANSMISSAO" → false (decisor trabalha na CTEEP, não na Neoenergia)
- Cargo "Diretor de Projetos na Vale" + obra "Cooperativa Agroindustrial Tradicao" → false (Vale ≠ Cooperativa)
- Cargo "Coord Suprimentos na HBR Energy" + obra "CITLUX EMPREENDIMENTOS" → false (sem prova literal que CITLUX = HBR)
- Cargo "Procurement Manager na ADM" + obra "Cargill" → false (ADM ≠ Cargill)

Exemplos aceitos (match=true):
- Cargo "Gerente Engenharia TAESA" + obra "Transmissora Alianca de Energia Eletrica S.A." → true (TAESA = expansão literal da empresa-alvo)
- Cargo "Procurement Manager @ ADM" + obra "Archer Daniels Midland (ADM)" → true (ADM bate)
- Cargo "Coord Compras na CSN" + obra "Companhia Siderurgica Nacional" → true (CSN = expansão da empresa-alvo)

Match=false adicional se:
- Cargo é genérico (Diretor / Administrador / Presidente / Sócio) sem citar empresa
- Coincidência só de nome próprio (Ems Mariano ≠ EMS S.A.)
- Empresa-alvo é entidade pública (Município/Estado/Ministério/Prefeitura/Secretaria/Governo/Agência)
- Empresa-alvo é SPE sem indicação literal do grupo controlador no cargo

Siglas conhecidas (use apenas para expandir nome da empresa-ALVO, NÃO para legitimar siglas no cargo):
TAESA = Transmissora Alianca de Energia Eletrica
CSN = Companhia Siderurgica Nacional
CTEEP = Companhia de Transmissao de Energia Eletrica Paulista
MRS = MRS Logistica
ADM = Archer Daniels Midland
JBS = JBS S.A.
BRF = BRF S.A.

Responda APENAS o JSON — sem markdown, sem fence:
{"match": true|false, "confianca": 0-100, "motivo": "1 frase curta citando se cargo bate ou não com empresa-alvo"}"""

USER_TEMPLATE = """\
Decisor: {nome}
Cargo: {cargo}
Empresa da obra (ALVO da auditoria): {empresa}"""

# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class Candidato:
    dob_id: str
    obra_id: str
    nome: str
    cargo: str
    empresa: str
    tier_atual: str
    tem_contato: bool
    score_atual: int

@dataclass
class Resultado:
    candidato: Candidato
    match: bool = False
    confianca: int = 0
    motivo: str = ""
    acao: str = "MANTIDO"
    erro: Optional[str] = None

# ─── Haiku client ─────────────────────────────────────────────────────────────

_client = _haiku_async_client()

async def _chamar_haiku(candidato: Candidato) -> dict:
    user_msg = USER_TEMPLATE.format(
        nome=candidato.nome,
        cargo=candidato.cargo or "não informado",
        empresa=candidato.empresa,
    )
    for tentativa in range(RETRY_ATTEMPTS):
        try:
            resp = await _client.messages.create(
                model=MODEL,
                max_tokens=150,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            texto = resp.content[0].text.strip()
            texto = texto.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(texto)
        except anthropic.RateLimitError:
            delay = RETRY_BASE_DELAY * (2 ** tentativa)
            log.warning("429 rate limit — aguardando %.1fs", delay)
            await asyncio.sleep(delay)
        except (json.JSONDecodeError, IndexError) as e:
            log.warning("Parse error tentativa %d: %s", tentativa + 1, e)
            await asyncio.sleep(RETRY_BASE_DELAY)
        except Exception as e:
            if tentativa == RETRY_ATTEMPTS - 1:
                raise
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** tentativa))
    raise RuntimeError(f"Haiku falhou após {RETRY_ATTEMPTS} tentativas")


# ─── Core ──────────────────────────────────────────────────────────────────────

async def _processar(
    semaphore: asyncio.Semaphore,
    pool: asyncpg.Pool,
    candidato: Candidato,
    min_confidence: int,
    commit: bool,
) -> Resultado:
    resultado = Resultado(candidato=candidato)

    async with pool.acquire() as conn:
        cached = await conn.fetchval(
            "SELECT confianca_match_componentes ? 'haiku_validation' FROM decisores_obra WHERE id = $1",
            candidato.dob_id,
        )
    if cached:
        resultado.acao = "SKIP_CACHE"
        return resultado

    async with semaphore:
        try:
            resposta = await _chamar_haiku(candidato)
        except Exception as e:
            resultado.acao = "ERRO"
            resultado.erro = str(e)
            return resultado

    resultado.match = bool(resposta.get("match", False))
    resultado.confianca = int(resposta.get("confianca", 0) or 0)
    resultado.motivo = (resposta.get("motivo") or "")[:200]

    promover = (
        resultado.match
        and resultado.confianca >= min_confidence
        and candidato.tem_contato
    )

    if commit and promover:
        novo_score = min(100, resultado.confianca)
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE decisores_obra
                    SET confianca_match = $1,
                        confianca_match_componentes =
                            COALESCE(confianca_match_componentes, '{}'::jsonb)
                            || jsonb_build_object(
                                'haiku_validation', jsonb_build_object(
                                    'match', $2::bool,
                                    'confianca', $3::int,
                                    'motivo', $4::text,
                                    'modelo', $5::text,
                                    'ts', NOW()::text
                                )
                            ),
                        confianca_match_calculada_em = NOW()
                    WHERE id = $6
                    """,
                    novo_score, resultado.match, resultado.confianca,
                    resultado.motivo, MODEL, candidato.dob_id,
                )
                await conn.execute(
                    "SELECT recompute_classificacao_obra($1)",
                    candidato.obra_id,
                )
        resultado.acao = "ATUALIZADO"
    elif commit:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE decisores_obra
                SET confianca_match_componentes =
                    COALESCE(confianca_match_componentes, '{}'::jsonb)
                    || jsonb_build_object(
                        'haiku_validation', jsonb_build_object(
                            'match', $1::bool,
                            'confianca', $2::int,
                            'motivo', $3::text,
                            'ts', NOW()::text
                        )
                    )
                WHERE id = $4
                """,
                resultado.match, resultado.confianca,
                resultado.motivo, candidato.dob_id,
            )

    return resultado


async def validate_batch(
    pool: asyncpg.Pool,
    min_score: int = 10,
    min_confidence: int = 95,
    limit: int = 100,
    commit: bool = False,
    tiers: tuple = ("OURO", "PRATA", "BRONZE"),
    source: str = "enrichment_19_05",
    single_obra_only: bool = True,
) -> list[Resultado]:
    """
    Valida decisores com score < min_score. Schema-aware (nome/cargo/linkedin_url/email).
    Por default filtra registrado_por='enrichment_19_05' e single-obra (Frente C original).
    """
    params: list = [min_score, list(tiers)]
    source_clause = ""
    if source:
        params.append(source)
        source_clause = f"AND d.registrado_por = ${len(params)}"
    params.append(limit)
    limit_placeholder = f"${len(params)}"

    single_obra_cte = ""
    single_obra_join = ""
    if single_obra_only:
        # Single-obra ESCOPO-RELATIVO: única no universo de
        # (registrado_por=source AND confianca_match<min_score). Pessoa pode ter
        # outras obras de score legítimo — não invalida análise da órfã.
        if source:
            freq_filter = f"d.registrado_por = '{source}' AND d.confianca_match < {min_score}"
        else:
            freq_filter = f"d.confianca_match < {min_score}"
        single_obra_cte = f"""
        WITH freq AS (
          SELECT d.nome, COUNT(*) AS qtd_obras
          FROM decisores_obra d
          WHERE d.excluido_em IS NULL
            AND {freq_filter}
          GROUP BY d.nome
        )
        """
        single_obra_join = "INNER JOIN freq f ON f.nome = d.nome AND f.qtd_obras = 1"

    query = f"""
        {single_obra_cte}
        SELECT
            d.id::text       AS dob_id,
            d.obra_id::text  AS obra_id,
            COALESCE(d.nome, '')         AS nome,
            COALESCE(d.cargo, '')        AS cargo,
            COALESCE(o.empresa, '')      AS empresa,
            COALESCE(o.classificacao_computed, '') AS tier_atual,
            (COALESCE(d.linkedin_url,'') <> '' OR COALESCE(d.email,'') <> '') AS tem_contato,
            COALESCE(d.confianca_match, 0) AS score_atual
        FROM decisores_obra d
        INNER JOIN obras o ON o.id = d.obra_id
        {single_obra_join}
        WHERE d.excluido_em IS NULL
          AND d.confianca_match < $1
          AND o.classificacao_computed = ANY($2::text[])
          AND COALESCE(d.nome, '') <> ''
          AND d.nome NOT SIMILAR TO '%(S\\.A\\.|LTDA|S/A|EIRELI)%'
          AND NOT EXISTS (
              SELECT 1 FROM decisores_obra d2
              WHERE d2.obra_id = d.obra_id
                AND d2.id <> d.id
                AND d2.excluido_em IS NULL
                AND d2.confianca_match >= $1
          )
          {source_clause}
        ORDER BY o.valor_estimado DESC NULLS LAST
        LIMIT {limit_placeholder}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    candidatos = [Candidato(**dict(r)) for r in rows]
    log.info(
        "Candidatos: %d | commit=%s | min_confidence=%d | source=%r | single_obra=%s",
        len(candidatos), commit, min_confidence, source or "(all)", single_obra_only,
    )

    if not candidatos:
        return []

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    tasks = [
        _processar(semaphore, pool, c, min_confidence, commit)
        for c in candidatos
    ]

    t0 = time.perf_counter()
    resultados = await asyncio.gather(*tasks, return_exceptions=False)
    elapsed = time.perf_counter() - t0

    atualizados = [r for r in resultados if r.acao == "ATUALIZADO"]
    mantidos    = [r for r in resultados if r.acao == "MANTIDO"]
    skip_cache  = [r for r in resultados if r.acao == "SKIP_CACHE"]
    erros       = [r for r in resultados if r.acao == "ERRO"]

    print(f"\n{'-'*60}")
    print(f"{'FRENTE C - HAIKU v2':^60}")
    print(f"{'-'*60}")
    print(f"  Candidatos  : {len(candidatos)}")
    print(f"  Atualizados : {len(atualizados)} {'(DRY-RUN)' if not commit else 'OK'}")
    print(f"  Mantidos    : {len(mantidos)}")
    print(f"  Cache skip  : {len(skip_cache)}")
    print(f"  Erros       : {len(erros)}")
    print(f"  Tempo       : {elapsed:.1f}s")
    print(f"{'-'*60}")

    if atualizados or (not commit and any(r.match for r in resultados)):
        print("\nMATCHES ENCONTRADOS:")
        for r in resultados:
            if r.match and r.confianca >= min_confidence:
                status = "COMMIT " if r.acao == "ATUALIZADO" else "DRY-RUN"
                print(
                    f"  {status} | conf={r.confianca:3d} | "
                    f"{r.candidato.nome[:30]:<30} | "
                    f"{r.candidato.empresa[:35]:<35}"
                )
                print(f"           {r.motivo}")

    if erros:
        print("\nERROS:")
        for r in erros:
            print(f"  X {r.candidato.nome} | {r.erro}")

    if commit and atualizados:
        async with pool.acquire() as conn:
            tiers_depois = await conn.fetch(
                """
                SELECT classificacao_computed AS tier, COUNT(*) AS qtd
                FROM obras
                WHERE classificacao_computed IS NOT NULL
                GROUP BY 1 ORDER BY 1
                """
            )
        print("\nVITRINE POS-COMMIT:")
        for t in tiers_depois:
            print(f"  {t['tier']}: {t['qtd']}")

    print(f"{'-'*60}\n")
    return resultados


# ─── CLI ──────────────────────────────────────────────────────────────────────

async def _main():
    parser = argparse.ArgumentParser(description="Frente C Haiku v2")
    parser.add_argument("--commit",          action="store_true")
    parser.add_argument("--min-score",       type=int, default=10)
    parser.add_argument("--min-confidence",  type=int, default=95)
    parser.add_argument("--limit",           type=int, default=100)
    parser.add_argument("--tiers",           nargs="+",
                        default=["OURO", "PRATA", "BRONZE", "PIPELINE"])
    parser.add_argument("--source",          default="enrichment_19_05",
                        help="filtro registrado_por (vazio = todos)")
    parser.add_argument("--all-obras", action="store_true",
                        help="desativa filtro single-obra")
    args = parser.parse_args()

    if not args.commit:
        log.info("DRY-RUN ativo — use --commit para aplicar mudanças")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    try:
        await validate_batch(
            pool=pool,
            min_score=args.min_score,
            min_confidence=args.min_confidence,
            limit=args.limit,
            commit=args.commit,
            tiers=tuple(args.tiers),
            source=args.source,
            single_obra_only=not args.all_obras,
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
