#!/usr/bin/env python3
"""
sonnet_research_obras_detalhes.py — Pesquisa na internet sobre a obra para extrair Capex,
descrição rica (escopo, insumos, equipamentos) e status/fase.
"""
import os, sys, json, re, argparse, logging, time
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime

# Shims do Anthropic / LLM
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")
from services.llm_haiku_compat import _haiku_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sonnet_research_obras_detalhes")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

SERPER_API_KEY = os.getenv("SERPER_API_KEY")

SYSTEM_PROMPT = """Você é um especialista em inteligência de mercado de obras industriais, comerciais e infraestrutura no Brasil.
Sua tarefa é analisar os resultados de busca sobre um projeto/obra e extrair:
1. O Capex estimado real em reais absolutos (float, ex: 200000000 para R$ 200 milhões).
2. Uma descrição rica e detalhada do projeto, focando em: o que está sendo construído, escopo técnico, materiais/insumos demandados (ex: concreto, aço, tubulações, fiação, terraplenagem), equipamentos (ex: turbinas, geradores, bombas, pontes rolantes) e a fase atual do projeto.
3. O nome correto da empresa responsável pelo investimento.
4. O status de licenciamento/execução física da obra (Planejamento, Em Obras, Inaugurada, Cancelada).

Retorne APENAS um JSON válido no formato especificado, sem explicações em markdown, sem blocos de código e sem texto adicional."""

USER_TEMPLATE = """Analise o projeto/obra abaixo e os resultados da pesquisa do Google sobre ele:

Projeto Original:
- Título/Nome: {nome}
- Empresa: {empresa}
- UF: {uf}
- Fonte original: {fonte}
- Descrição atual: {descricao_atual}

Resultados da Pesquisa Google:
{search_results}

Sua tarefa é extrair e detalhar as informações em JSON:
- "empresa": Nome corrigido/detalhado da empresa investidora ou manter o original se correto.
- "capex": Valor estimado em reais absolutos (float, ex: 200000000.0 para R$ 200 milhões). Se não houver menção de valor, use 0.
- "descricao_rica": Uma descrição rica de 2-4 parágrafos focando em escopo físico da obra, materiais e insumos necessários, fornecedores de tecnologia/equipamentos e fase de andamento da obra.
- "fase": Retorne "PLANEJAMENTO" ou "EM_EXECUCAO".
- "status_licenca": Status resumido da obra/licença (ex: "Licença Prévia obtida", "Em obras", "Licença de Instalação", "Adjudicada").
- "confianca": "alta" se encontrou dados exatos do projeto, "media" se estimou com base em dados correlatos, "baixa" se não encontrou nada relevante.

Retorne EXATAMENTE este formato JSON:
{{
  "empresa": "Nome da Empresa",
  "capex": 0.0,
  "descricao_rica": "...",
  "fase": "PLANEJAMENTO|EM_EXECUCAO",
  "status_licenca": "...",
  "confianca": "alta|media|baixa"
}}"""

def serper_search(query: str) -> list[dict]:
    # 1. Tenta usar o Serper se a chave estiver configurada
    if SERPER_API_KEY and SERPER_API_KEY.strip():
        url = "https://google.serper.dev/search"
        headers = {
            "X-API-KEY": SERPER_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "q": query,
            "gl": "br",
            "hl": "pt-br",
            "num": 5
        }
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=20)
            if r.status_code != 200:
                log.warning(f"Serper error {r.status_code}: {r.text}")
            r.raise_for_status()
            res = r.json()
            return res.get("organic", []) or []
        except Exception as e:
            log.warning(f"Erro Serper search, usando DDG fallback: {e}")

    # 2. Fallback: DDG Keyless Search com Proxy Tor (100% gratuito e estável)
    try:
        from ddgs import DDGS
        tor_proxy = os.getenv("TOR_PROXY_URL", "http://tor-privoxy:8118")
        with DDGS(proxy=tor_proxy) as ddgs:
            res_list = list(ddgs.text(query, max_results=5))
        mapped = []
        for r in res_list:
            mapped.append({
                "title": r.get("title", ""),
                "link": r.get("href", ""),
                "snippet": r.get("body", "")
            })
        if mapped:
            log.info(f"  ✓ DuckDuckGo (via Tor): {len(mapped)} resultados obtidos para '{query[:40]}...'")
            return mapped
    except Exception as e:
        log.warning(f"Erro DuckDuckGo search fallback, usando SearXNG fallback: {e}")

    # 3. Fallback: SearXNG local (100% gratuito e ilimitado)
    try:
        import urllib.parse
        url = f"http://searxng:8080/search?q={urllib.parse.quote(query)}&format=json"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        res = r.json()
        mapped = []
        for item in res.get("results", []):
            mapped.append({
                "title": item.get("title", ""),
                "link": item.get("url", ""),
                "snippet": item.get("content", "")
            })
        if mapped:
            log.info(f"  ✓ SearXNG: {len(mapped)} resultados obtidos para '{query[:40]}...'")
            return mapped
    except Exception as e:
        log.error(f"Erro SearXNG fallback para query '{query}': {e}")
        
    return []

def formatar_valor(valor: float) -> str | None:
    if not valor or valor <= 0:
        return None
    if valor >= 1e9:
        return f"R$ {valor/1e9:.1f} bi"
    elif valor >= 1e6:
        return f"R$ {valor/1e6:.0f} mi"
    elif valor >= 1e3:
        return f"R$ {valor/1e3:.0f} k"
    return f"R$ {valor:,.0f}"

def main():
    parser = argparse.ArgumentParser(description="Pesquisa web e enriquecimento profundo de detalhes de obras")
    parser.add_argument("--commit", action="store_true", help="Salva alterações no banco de dados. Sem esta flag, roda em dry-run.")
    parser.add_argument("--limit", type=int, default=5, help="Limite de obras a processar (default: 5)")
    parser.add_argument("--obra-id", type=str, help="UUID de uma obra específica para pesquisar")
    args = parser.parse_args()

    commit = args.commit
    log.info(f"=== INICIO PESQUISA OBRAS {'(COMMIT)' if commit else '(DRY-RUN)'} ===")

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Seleciona obras que não têm capex ou têm descrição muito curta
    if args.obra_id:
        cur.execute("""
            SELECT id::text, nome, empresa, cnpj, setor, uf, valor_estimado, descricao, fonte, url_fonte, classificacao_computed
            FROM obras
            WHERE id = %s::uuid
        """, (args.obra_id,))
    else:
        cur.execute("""
            SELECT id::text, nome, empresa, cnpj, setor, uf, valor_estimado, descricao, fonte, url_fonte, classificacao_computed
            FROM obras
            WHERE visivel = true
              AND (valor_estimado IS NULL OR valor_estimado = 0 OR LENGTH(COALESCE(descricao, '')) < 150)
            ORDER BY criado_em DESC
            LIMIT %s
        """, (args.limit,))

    obras = cur.fetchall()
    log.info(f"Obras candidatas para pesquisa: {len(obras)}")

    if not obras:
        log.info("Nenhuma obra encontrada para atualizar.")
        conn.close()
        return

    client = _haiku_client()

    for idx, obra in enumerate(obras, 1):
        log.info(f"[{idx}/{len(obras)}] Processando Obra ID: {obra['id']} | Nome: {obra['nome'][:80]}")
        
        # 1. Formula query e realiza busca
        query_parts = []
        if obra['empresa'] and obra['empresa'].strip():
            query_parts.append(obra['empresa'].strip())
        query_parts.append(obra['nome'].strip())
        if obra['uf']:
            query_parts.append(obra['uf'])
        query_parts.append("investimento capex obras")
        
        query = " ".join(query_parts)
        log.info(f"  Query Google: {query}")
        
        organic_results = serper_search(query)
        
        if not organic_results:
            log.warning("  Nenhum resultado orgânico retornado do Google.")
            continue
            
        search_text = []
        for o in organic_results:
            title = o.get("title", "")
            snippet = o.get("snippet", "")
            link = o.get("link", "")
            search_text.append(f"- Título: {title}\n  Snippet: {snippet}\n  Link: {link}")
            
        search_results_str = "\n".join(search_text)
        
        # 2. Monta Prompt para Claude
        prompt = USER_TEMPLATE.format(
            nome=obra['nome'] or '',
            empresa=obra['empresa'] or '',
            uf=obra['uf'] or '',
            fonte=obra['fonte'] or '',
            descricao_atual=obra['descricao'] or '',
            search_results=search_results_str
        )
        
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )
            raw_text = resp.content[0].text.strip()
            
            # Limpa código e marcações
            raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.MULTILINE).strip()
            
            m_json = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if not m_json:
                log.error("  Não foi possível encontrar um JSON válido na resposta do modelo.")
                continue
                
            data = json.loads(m_json.group(0))
            
            confianca = data.get("confianca", "baixa").lower()
            empresa_ext = data.get("empresa") or obra['empresa']
            capex = float(data.get("capex", 0.0))
            descricao_rica = data.get("descricao_rica", "")
            fase = data.get("fase", "PLANEJAMENTO")
            status_lic = data.get("status_licenca", "Não definido")
            
            log.info(f"  Extracao LLM -> Confiança: {confianca} | Capex: {capex} ({formatar_valor(capex)}) | Fase: {fase} | Status: {status_lic}")
            
            if confianca in ("alta", "media") and len(descricao_rica) > 100:
                valor_fmt = formatar_valor(capex)
                if commit:
                    cur.execute("""
                        UPDATE obras SET
                            empresa = COALESCE(NULLIF(%s, ''), empresa),
                            valor_estimado = CASE WHEN %s > 0.0 THEN %s ELSE valor_estimado END,
                            valor_formatado = CASE WHEN %s > 0.0 THEN %s ELSE valor_formatado END,
                            descricao = %s,
                            fase = %s,
                            status_licenca = %s,
                            observacoes_validacao = COALESCE(observacoes_validacao, '') || ' | sonnet_research_obras_detalhes_20260709'
                        WHERE id = %s::uuid
                    """, (
                        empresa_ext.strip()[:300] if empresa_ext else None,
                        capex, capex,
                        capex, valor_fmt,
                        descricao_rica,
                        fase,
                        status_lic[:200],
                        obra['id']
                    ))
                    log.info(f"  ✓ Obra ATUALIZADA no banco de dados!")
                else:
                    log.info(f"  [DRY-RUN] Atualizaria Obra: {obra['id']} | Capex={capex} | Status={status_lic}")
                    log.info(f"  [DRY-RUN] Descrição rica:\n{descricao_rica[:300]}...")
            else:
                log.warning(f"  ✗ Ignorado por baixa confiança ou descrição insuficiente.")
                
        except Exception as e:
            log.error(f"  Erro ao processar obra {obra['id']}: {e}")
            continue

    if commit:
        conn.commit()
        log.info("Modificações persistidas com sucesso.")
    else:
        log.info("Modificações descartadas (modo dry-run).")
        
    conn.close()
    log.info("=== FIM PESQUISA OBRAS ===")

if __name__ == "__main__":
    main()
