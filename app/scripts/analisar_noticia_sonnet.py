#!/usr/bin/env python3
"""
Analise inteligente de noticias via Claude Sonnet 4.6.
Chamado pelo captador captar_google_alerts apos extracao Haiku, ou standalone.

Uso:
  python analisar_noticia_sonnet.py --id 16          # analisa noticia especifica
  python analisar_noticia_sonnet.py --todas          # processa todas pendentes
  
Como importavel (do captador):
  from analisar_noticia_sonnet import analisar_e_persistir
  client = anthropic.Anthropic()
  analisar_e_persistir(noticia_id, cur, anthropic_client=client)
"""
import argparse
import json
import logging
import os
import re

import anthropic
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "host":     os.getenv("DB_HOST", "db"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME", "wins_hub"),
}

MODEL = "claude-sonnet-4-6"  # familia Claude 4.X

SYSTEM_PROMPT = """Voce e um analista senior de inteligencia comercial B2B especializado em obras e projetos de infraestrutura industrial no Brasil.

Sua tarefa e analisar noticias capturadas e decidir se representam uma oportunidade real para fornecedores de engenharia B2B (civil, eletrica industrial, TI/automacao, mecanica, utilities industriais).

Retorne APENAS um JSON valido, sem markdown, sem texto extra."""


def build_user_prompt(noticia: dict) -> str:
    valor = noticia.get("valor_estimado") or 0
    try:
        valor_m = int(valor) / 1e6
    except (TypeError, ValueError):
        valor_m = 0
    return f"""Analise esta noticia de obra/investimento:

Titulo: {noticia.get('titulo', '')}
Empresa: {noticia.get('empresa', '')}
Descricao extraida: {noticia.get('descricao', '')}
Valor estimado: R$ {valor_m:.0f}M
UF: {noticia.get('uf', '')}
Municipio: {noticia.get('municipio', '')}
Setor: {noticia.get('setor', '')}
CNPJ hint: {noticia.get('cnpj_hint') or 'nao disponivel'}
URL fonte: {noticia.get('url', '')}

Analise seguindo estes criterios em ordem:

0. FILTRO HARD-REJECT (avaliar PRIMEIRO, antes de tudo):
   Se a noticia anuncia INSTRUMENTO FINANCEIRO (nao obra), tier_recomendado=REJEITAR e PARE.
   Hard-reject patterns:
   - "BNDES aprova / libera / financia R$X" sem nome de obra contratavel especifica
   - "Linha de credito / Fundo / Programa" de qualquer banco/agencia (FINEP, FINAME, FCO, FCO-Verde, BNB, etc)
   - "Debenture", "fundo de investimento", "FII", "FIDC" emissao/aquisicao
   - "Letra Financeira", "CRA", "CRI", "Eurobond"
   - "Carta de credito", "letra de cambio"
   - Anuncio agregado de portfolio (ex: "BNDES aprovou R$15bi para 12 projetos em 2026")
   - "Subvencao", "incentivo fiscal" sem obra fisica
   - Aquisicao de ativos financeiros / M&A / IPO / follow-on
   Exemplo: "BNDES libera R$21bi para infraestrutura em 2026" = REJEITAR (e politica, nao obra). Mesmo se algumas das obras subjacentes forem reais, a noticia NAO eh o lead.
   Exemplo: "Klabin contrata construcao de nova maquina de papel em Ortigueira (R$2bi)" = NAO eh hard-reject (e obra real).

1. NATUREZA B2B: E obra real que contrata fornecedores de engenharia? (expansao fabril, nova planta, data center, galpao logistico, hospital, infraestrutura, energia, transporte, saneamento, instalacoes tecnicas de TI/automacao = SIM. Aquisicao financeira, M&A, operacao rotineira, produto de consumo, linha de credito/fundo/programa de financiamento (BNDES/FCO/FINEP/FINAME) = NAO)

2. CAPEX REAL: O valor informado e o capex da obra contratavel por fornecedores locais? Ou e faturamento/receita/valor de financiamento agregado? Estime o capex real contratavel.

3. DECISOR IDENTIFICAVEL: A noticia menciona nome de diretor/gerente/VP de engenharia, obras, operacoes ou compras?

4. CNPJ: Se cnpj_hint for null, infira o CNPJ mais provavel da empresa principal mencionada. Se nao souber com certeza, deixe null.

5. TIER RECOMENDADO:
   - OURO: capex real >= R$500M + obra contratavel + decisor identificado
   - PRATA: capex real R$100-500M + obra contratavel
   - BRONZE: capex real R$50-100M + obra contratavel
   - PIPELINE: capex real R$10-50M ou incerteza sobre contratabilidade
   - REJEITAR: nao e obra B2B ou capex < R$10M

6. CONFIANCA na recomendacao: alta (>=85%), media (60-84%), baixa (<60%)

Retorne EXATAMENTE este JSON:
{{
  "e_obra_b2b": true,
  "justificativa_b2b": "1-2 frases explicando",
  "capex_real_M": 0,
  "capex_observacao": "diferenca entre valor noticia vs capex contratavel se houver",
  "decisor_nome": null,
  "cnpj_inferido": null,
  "tier_recomendado": "OURO",
  "confianca": "alta",
  "confianca_pct": 0,
  "resumo_para_admin": "1 paragrafo direto com a recomendacao e razao principal"
}}"""


def analisar_via_llm(client: anthropic.Anthropic, dados: dict) -> dict | None:
    """Pure LLM call - sem touch DB. Retorna dict ou None se erro."""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(dados)}],
        )
        raw = response.content[0].text.strip()
        # Strip ```json fences (Haiku e Sonnet podem retornar com markdown)
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"JSON malformado de Sonnet: {e}\nRaw: {raw[:500] if 'raw' in dir() else '?'}")
        return None
    except Exception as e:
        log.error(f"Erro Sonnet: {type(e).__name__}: {e}")
        return None


def persistir_analise(cur, noticia_id: int, analysis: dict):
    """psycopg2 cursor — usado pelo captador. Commit fica com chamador."""
    cur.execute(
        """
        UPDATE noticias_backlog_manual SET
            sonnet_analysis         = %s::jsonb,
            sonnet_confidence       = %s,
            sonnet_tier_recomendado = %s
        WHERE id = %s
        """,
        (
            json.dumps(analysis),
            analysis.get("confianca", "baixa"),
            analysis.get("tier_recomendado", "PIPELINE"),
            noticia_id,
        ),
    )


def analisar_e_persistir(noticia_id: int, cur, anthropic_client: anthropic.Anthropic) -> dict | None:
    """Helper completo: fetch noticia, chama Sonnet, persiste. Usavel pelo captador.

    Retorna o dict de analysis ou None em caso de erro.
    Commit fica com o chamador (captador faz commit ao fim do batch).
    """
    cur.execute(
        "SELECT id, titulo, url, descricao FROM noticias_backlog_manual WHERE id = %s",
        (noticia_id,),
    )
    row = cur.fetchone()
    if not row:
        log.error(f"Noticia {noticia_id} nao encontrada")
        return None

    # row pode ser tuple (default cursor) ou dict (RealDictCursor)
    if isinstance(row, dict):
        titulo, url, descricao = row.get("titulo"), row.get("url"), row.get("descricao")
    else:
        titulo, url, descricao = row[1], row[2], row[3]

    try:
        dados = json.loads(descricao or "{}")
    except Exception:
        dados = {}

    dados["titulo"] = titulo or ""
    dados["url"] = url or ""

    log.info(f"Analisando noticia {noticia_id}: {(dados.get('titulo') or '')[:60]}")
    analysis = analisar_via_llm(anthropic_client, dados)
    if not analysis:
        return None

    persistir_analise(cur, noticia_id, analysis)
    tier = analysis.get("tier_recomendado", "?")
    confianca = analysis.get("confianca", "?")
    pct = analysis.get("confianca_pct", "?")
    log.info(f"  -> {tier} | confianca {confianca} ({pct}%) | B2B={analysis.get('e_obra_b2b')}")
    return analysis


def processar_todas_pendentes(conn, client):
    """Pega todas pending_url sem analise e processa em loop."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM noticias_backlog_manual
            WHERE sonnet_analysis IS NULL AND status = 'pending_url'
            ORDER BY criado_em DESC
            """
        )
        ids = [r[0] for r in cur.fetchall()]

    log.info(f"Pendentes sem analise Sonnet: {len(ids)}")
    sucesso = 0
    for nid in ids:
        with conn.cursor() as cur:
            result = analisar_e_persistir(nid, cur, client)
            if result:
                conn.commit()
                sucesso += 1
            else:
                conn.rollback()
    log.info(f"Processadas: {sucesso}/{len(ids)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, help="Analisar noticia especifica")
    ap.add_argument("--todas", action="store_true", help="Processar todas pendentes")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY ausente")
        return

    client = anthropic.Anthropic()
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        if args.id:
            with conn.cursor() as cur:
                result = analisar_e_persistir(args.id, cur, client)
                if result:
                    conn.commit()
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                else:
                    conn.rollback()
        elif args.todas:
            processar_todas_pendentes(conn, client)
        else:
            print("Use --id N ou --todas")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
