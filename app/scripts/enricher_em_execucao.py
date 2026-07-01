"""
enricher_em_execucao.py — Cria decisores para obras EM_EXECUCAO via Claude API + web_search.

WHAT IT DOES:
- Busca obras EM_EXECUCAO sem decisor cadastrado e ainda não processadas pela routine.
- Para cada obra, faz UMA chamada à Claude API com a tool web_search_20250305 habilitada.
- Modelo busca pessoas em até 10 cargos-padrão Anderson na empresa responsável e devolve
  JSON estruturado com {nome, cargo_original, tipo_cargo, linkedin_url}.
- Para cada decisor encontrado: INSERT idempotente em decisores_obra (skip se já existe
  outro decisor com mesmo nome para a mesma obra) + INSERT em enriquecimento_log
  (fonte='WEBSEARCH_ROUTINE:<tipo_cargo>').
- Se 0 decisores válidos: INSERT marker em enriquecimento_log
  (fonte='WEBSEARCH_ROUTINE_VAZIO') — assim a obra não volta na próxima rodada.

REQUIRED ENV VARS (em /root/wins_hub/.env):
- ANTHROPIC_API_KEY    — chave da Anthropic API
- DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

OPTIONAL ENV VARS:
- ENRICHER_LIMIT       — quantas obras processar (default 20)

INSTALL (uma vez, no container):
    docker exec wins_hub-api-1 pip install 'anthropic>=0.40'

RUN:
    docker exec wins_hub-api-1 python /app/scripts/enricher_em_execucao.py

COST ESTIMATE (20 obras, 1 chamada por obra, modelo claude-sonnet-4-6):
- web_search: $10/1k × ~60 (até 3 searches por obra) = ~$0.60
- tokens (output JSON multi-decisor + system cacheado se disparar): ~$3-5
- Total: ~$5-8 por run de 20 obras.
"""

import json
import sys as _scompat
if "/app" not in _scompat.path: _scompat.path.insert(0, "/app")
from services.llm_haiku_compat import _haiku_client, _haiku_async_client  # free-first 25/06
import os
import re
import sys
import time

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, "/app")
from sales_intelligence.decisor_gate import decisor_inserivel

try:
    import anthropic
except ImportError:
    sys.exit(
        "ERRO: anthropic SDK não instalado.\n"
        "Rode: docker exec wins_hub-api-1 pip install 'anthropic>=0.40'"
    )


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    sys.exit("ERRO: ANTHROPIC_API_KEY não configurada no .env")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}
LIMIT = int(os.getenv("ENRICHER_LIMIT", "20"))
MODEL = "claude-sonnet-4-6"

TIPOS_CARGO_VALIDOS = {
    "GERENTE_SUPRIMENTOS",
    "GERENTE_COMPRAS",
    "SUPPLY_CHAIN",
    "ENGENHEIRO_MECANICO_CIVIL",
    "GERENTE_ENGENHARIA",
    "PROJETISTA",
    "COORDENADOR_MANUTENCAO",
    "GERENTE_INDUSTRIAL",
    "COORDENADOR_OBRAS",
    "GERENTE_PROJETOS",
}

CARGOS_ANDERSON = [
    ("Gerente de Compras / Coordenador de Compras", "GERENTE_COMPRAS"),
    ("Gerente de Suprimentos / Coordenador de Suprimentos", "GERENTE_SUPRIMENTOS"),
    ("Supply Chain Manager", "SUPPLY_CHAIN"),
    ("Engenheiro Mecânico ou Engenheiro Civil", "ENGENHEIRO_MECANICO_CIVIL"),
    ("Gerente de Engenharia", "GERENTE_ENGENHARIA"),
    ("Engenheiro de Projetos / Projetista", "PROJETISTA"),
    ("Coordenador de Manutenção", "COORDENADOR_MANUTENCAO"),
    ("Gerente Industrial", "GERENTE_INDUSTRIAL"),
    ("Coordenador de Obras", "COORDENADOR_OBRAS"),
    ("Gerente de Projetos", "GERENTE_PROJETOS"),
]
CARGOS_TEXT = "\n".join(
    f"{i+1:2d}. {desc} → tipo_cargo: {tc}" for i, (desc, tc) in enumerate(CARGOS_ANDERSON)
)

SYSTEM_PROMPT = f"""Você é um especialista em prospecção B2B brasileira para grandes obras de capex (mineração, energia, infraestrutura, indústria pesada). Sua tarefa é encontrar pessoas reais em cargos decisores específicos em uma empresa brasileira.

CARGOS-ALVO (busque cada um, em ordem de prioridade):
{CARGOS_TEXT}

INSTRUÇÕES OPERACIONAIS:
1. Use a tool web_search com queries por cargo, ex:
   "Gerente de Compras" "<empresa>" site:linkedin.com/in
   "Diretor de Suprimentos" "<empresa>" site:linkedin.com
   "<empresa>" engenharia LinkedIn
   Use no máximo 3 queries — escolha as mais promissoras.
2. Para cada cargo da lista que conseguir preencher com confiança, retorne UM decisor com:
   - nome: nome completo real (não inventado)
   - cargo_original: o título do cargo como aparece no LinkedIn (ex: "Gerente de Compras Sr")
   - tipo_cargo: EXATAMENTE um dos valores padronizados acima (caixa alta, undescore)
   - linkedin_url: URL canônica https://www.linkedin.com/in/<slug>
3. URLs LinkedIn aceitáveis: APENAS https://www.linkedin.com/in/<slug>.
   REJEITE: /pub/, /company/, /school/, /jobs/, /posts/, /pulse/, /feed/, /sales/.
4. NÃO invente pessoas, nomes, cargos ou URLs. Se não tiver um match plausível para um cargo, OMITA esse cargo da resposta. Melhor omitir do que chutar.
5. O perfil deve estar atualmente (ou recentemente, último ano) na empresa indicada. Se a pessoa saiu há anos, omita.
6. Não retorne mais de UM decisor por tipo_cargo. Não retorne a mesma pessoa em dois cargos diferentes.
7. Pode haver 0 decisores se a empresa for pequena demais ou não tiver presença no LinkedIn — nesse caso retorne lista vazia.

VALORES VÁLIDOS de tipo_cargo (rígido):
GERENTE_SUPRIMENTOS, GERENTE_COMPRAS, SUPPLY_CHAIN, ENGENHEIRO_MECANICO_CIVIL,
GERENTE_ENGENHARIA, PROJETISTA, COORDENADOR_MANUTENCAO, GERENTE_INDUSTRIAL,
COORDENADOR_OBRAS, GERENTE_PROJETOS.

FORMATO DE RESPOSTA (no último bloco de texto, APENAS o JSON, sem markdown, sem comentários):
{{
  "decisores": [
    {{"nome": "Fulano Silva", "cargo_original": "Gerente de Compras Sr", "tipo_cargo": "GERENTE_COMPRAS", "linkedin_url": "https://www.linkedin.com/in/fulano-silva"}},
    {{"nome": "Sicrano Costa", "cargo_original": "Coordenador de Manutenção Industrial", "tipo_cargo": "COORDENADOR_MANUTENCAO", "linkedin_url": "https://www.linkedin.com/in/sicrano-costa"}}
  ]
}}

ou, se não encontrou ninguém:
{{"decisores": []}}
"""

LINKEDIN_IN_RE = re.compile(
    r"^https://www\.linkedin\.com/in/[A-Za-z0-9\-_%\.]+$"
)
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
BLACKLIST = ("/pub/", "/company/", "/school/", "/jobs/", "/posts/", "/pulse/", "/feed/", "/sales/")


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def fetch_lote(limit):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id::text AS obra_id, nome, empresa, cnpj, uf
                FROM obras
                WHERE fase = 'EM_EXECUCAO'
                  AND (nivel1_nome IS NULL OR nivel1_nome = '')
                  AND COALESCE(fonte_tipo, 'OFICIAL') != 'NOTICIA'
                  AND (visivel IS NULL OR visivel = true)
                  AND id NOT IN (
                      SELECT obra_id FROM enriquecimento_log
                      WHERE fonte LIKE 'WEBSEARCH_ROUTINE%%'
                  )
                ORDER BY lead_score DESC NULLS LAST, urgencia ASC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def normalize_linkedin(url):
    if not url or not isinstance(url, str):
        return None
    url = url.strip().rstrip("/")
    if "?" in url:
        url = url.split("?", 1)[0]
    if "#" in url:
        url = url.split("#", 1)[0]
    lower = url.lower()
    if any(bad in lower for bad in BLACKLIST):
        return None
    url = re.sub(r"^http://", "https://", url)
    url = re.sub(r"^https://(?:[a-z]{2}\.)?linkedin\.com", "https://www.linkedin.com", url)
    if not url.startswith("https://www.linkedin.com/in/"):
        return None
    if not LINKEDIN_IN_RE.match(url):
        return None
    return url


def parse_json_response(text):
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    m = JSON_OBJECT_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def buscar_decisores(client, obra):
    user_msg = (
        f"Empresa: {obra['empresa']}\n"
        f"CNPJ: {obra.get('cnpj') or '—'}\n"
        f"UF: {obra.get('uf') or '—'}\n"
        f"Obra/Projeto: {obra['nome']}\n\n"
        f"Encontre pessoas reais para os 10 cargos-alvo nesta empresa. "
        f"Retorne só os cargos com match plausível, no formato JSON especificado."
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text = block.text
    parsed = parse_json_response(text)
    if not isinstance(parsed, dict):
        return []
    raw_list = parsed.get("decisores") or []
    if not isinstance(raw_list, list):
        return []

    valid = []
    seen_tipos = set()
    seen_nomes = set()
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        nome = (item.get("nome") or "").strip()
        cargo = (item.get("cargo_original") or "").strip()
        tipo = (item.get("tipo_cargo") or "").strip().upper()
        url = normalize_linkedin(item.get("linkedin_url"))
        if not nome or not cargo or tipo not in TIPOS_CARGO_VALIDOS or not url:
            continue
        nome_lower = nome.lower()
        if tipo in seen_tipos or nome_lower in seen_nomes:
            continue
        seen_tipos.add(tipo)
        seen_nomes.add(nome_lower)
        valid.append({"nome": nome, "cargo": cargo, "tipo_cargo": tipo, "linkedin_url": url})
    return valid


def insert_decisor_idempotente(obra_id, decisor):
    """Retorna (decisor_id, criado_bool). Skip se já existe decisor com mesmo nome
    nesta obra (a tabela tem UNIQUE em (obra_id, nome) WHERE excluido_em IS NULL).

    Aplica decisor_gate antes do INSERT — rejeita replicações e cargos não-decisor.
    Ver .claude/skills/decisor-capture/.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text FROM decisores_obra
                WHERE obra_id = %s AND lower(nome) = lower(%s) AND excluido_em IS NULL
                LIMIT 1
                """,
                (obra_id, decisor["nome"]),
            )
            existing = cur.fetchone()
            if existing:
                return existing[0], False

            cur.execute("SELECT empresa FROM obras WHERE id = %s", (obra_id,))
            row = cur.fetchone()
            empresa_obra = (row[0] or "") if row else ""
            permite, motivo = decisor_inserivel(
                cur, decisor["nome"], decisor.get("cargo") or "", empresa_obra
            )
            if not permite:
                print(f"  gate rejeitou: {decisor['nome']!r} obra_id={obra_id} motivo={motivo}")
                return None, False

            cur.execute(
                """
                INSERT INTO decisores_obra
                    (obra_id, nome, cargo, tipo_cargo, linkedin_url, fonte)
                VALUES (%s, %s, %s, %s, %s, 'anderson_csv_websearch')
                RETURNING id::text
                """,
                (
                    obra_id,
                    decisor["nome"],
                    decisor["cargo"],
                    decisor["tipo_cargo"],
                    decisor["linkedin_url"],
                ),
            )
            new_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO enriquecimento_log
                    (obra_id, decisor_id, decisor_nome, campo, valor_anterior, valor_novo, fonte)
                VALUES (%s, %s, %s, 'decisor_criado', NULL, %s, %s)
                """,
                (
                    obra_id,
                    new_id,
                    decisor["nome"],
                    decisor["linkedin_url"],
                    f"WEBSEARCH_ROUTINE:{decisor['tipo_cargo']}",
                ),
            )
        conn.commit()
        return new_id, True
    finally:
        conn.close()


def marcar_esgotada(obra_id):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO enriquecimento_log
                    (obra_id, decisor_id, decisor_nome, campo, valor_anterior, valor_novo, fonte)
                VALUES (%s, NULL, NULL, 'websearch_routine', NULL, 'esgotado', 'WEBSEARCH_ROUTINE_VAZIO')
                """,
                (obra_id,),
            )
        conn.commit()
    finally:
        conn.close()


def main():
    print(f"[enricher_em_execucao] modelo={MODEL} limit={LIMIT}")
    obras = fetch_lote(LIMIT)
    if not obras:
        print("Nada a processar — fila vazia.")
        return
    print(f"Lote: {len(obras)} obras")

    client = _haiku_client(api_key=ANTHROPIC_API_KEY)
    decisores_criados = obras_esgotadas = obras_com_match = erros = 0

    for i, obra in enumerate(obras, 1):
        tag = f"{obra['nome']} ({obra['empresa']})"
        try:
            decisores = buscar_decisores(client, obra)
        except anthropic.APIError as e:
            erros += 1
            print(f"[{i}/{len(obras)}] ERR Claude: {tag} → {e}")
            time.sleep(2.0)
            continue
        except Exception as e:
            erros += 1
            print(f"[{i}/{len(obras)}] ERR: {tag} → {e!r}")
            continue

        if not decisores:
            try:
                marcar_esgotada(obra["obra_id"])
                obras_esgotadas += 1
                print(f"[{i}/{len(obras)}] —   {tag} (esgotada)")
            except Exception as e:
                erros += 1
                print(f"[{i}/{len(obras)}] ERR DB esgotada: {tag} → {e!r}")
            time.sleep(0.5)
            continue

        criados_obra = 0
        for d in decisores:
            try:
                _, criado = insert_decisor_idempotente(obra["obra_id"], d)
                if criado:
                    criados_obra += 1
                    decisores_criados += 1
            except Exception as e:
                erros += 1
                print(f"[{i}/{len(obras)}] ERR insert: {tag} {d['nome']} → {e!r}")
        obras_com_match += 1
        print(
            f"[{i}/{len(obras)}] OK  {tag} → {len(decisores)} encontrados, {criados_obra} novos"
        )
        time.sleep(0.5)

    print()
    print("=" * 60)
    print(f"Obras processadas: {len(obras)}")
    print(f"Obras com match: {obras_com_match}")
    print(f"Obras esgotadas: {obras_esgotadas}")
    print(f"Decisores criados: {decisores_criados}")
    print(f"Erros: {erros}")
    print("=" * 60)


if __name__ == "__main__":
    main()
