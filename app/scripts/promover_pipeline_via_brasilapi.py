"""
promover_pipeline_via_brasilapi.py
Popula decisores de obras PIPELINE via BrasilAPI QSA (gratuito).

Fluxo por obra:
  1. CNPJ da obra
  2. GET BrasilAPI /api/cnpj/v1/{cnpj}
  3. Extrai QSA (sócios/administradores)
  4. Mapeia qualificacao → tipo_cargo
  5. INSERT top-2 em decisores_obra com fonte='brasilapi_qsa'
  6. Marca em enriquecimento_log (fonte='brasilapi_qsa:<status>') p/ skip futuro

Limitação conhecida: QSA não traz email/linkedin → obra NÃO vira OURO automatic.
Anderson curaria contatos depois manualmente. Ganho aqui = nomes + cargos.

Rate limit BrasilAPI free tier: ~3 req/min → sleep RATE_LIMIT_SLEEP entre calls.
DRY-RUN por padrão. Use --commit pra persistir.

Uso:
    docker exec wins_hub-api-1 python /app/scripts/promover_pipeline_via_brasilapi.py --limit 1
    docker exec wins_hub-api-1 python /app/scripts/promover_pipeline_via_brasilapi.py --commit --limit 20
"""
import sys
sys.path.insert(0, "/app")

import argparse
import json
import re
import time
import urllib.error
import urllib.request

import psycopg2
from psycopg2.extras import RealDictCursor

from services.matchmaking import DB_CONFIG


BRASILAPI_URL    = "https://brasilapi.com.br/api/cnpj/v1/{}"
RATE_LIMIT_SLEEP = 22  # segundos entre calls (~3 req/min seguro)


# ── Mapeamento qualificacao QSA → tipo_cargo ────────────────────────────────

QSA_MAP = [
    (r"diretor|director",                        "GERENTE_PROJETOS"),
    (r"presidente|president",                    "GERENTE_PROJETOS"),
    (r"s.cio.administrador|socio.administrador", "GERENTE_PROJETOS"),
    (r"administrador",                           "GERENTE_PROJETOS"),
    (r"gerente",                                 "GERENTE_PROJETOS"),
    (r"procurador",                              "GERENTE_SUPRIMENTOS"),
    # sócio puro (sem 'administrador' antes/depois) → OUTRO (filtrado fora)
    (r"s.cio(?!.administrador)|socio(?!.admin)", "OUTRO"),
]


def mapear_qualificacao(qualificacao: str) -> str:
    if not qualificacao:
        return "OUTRO"
    q = qualificacao.lower()
    for pattern, tipo in QSA_MAP:
        if re.search(pattern, q, re.I):
            return tipo
    return "OUTRO"


# ── BrasilAPI call ──────────────────────────────────────────────────────────

def buscar_qsa(cnpj: str) -> tuple[list, str]:
    """Retorna (qsa_list, status_str). status: 'ok', 'sem_qsa', '404', 'erro'."""
    url = BRASILAPI_URL.format(cnpj)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WiNSHub/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            qsa = data.get("qsa") or []
            return qsa, ("ok" if qsa else "sem_qsa")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [], "404"
        print(f"    ⚠ BrasilAPI HTTP {e.code}: {e.reason}")
        return [], f"erro_http_{e.code}"
    except Exception as e:
        print(f"    ⚠ BrasilAPI erro: {e}")
        return [], "erro"


# ── SQL ─────────────────────────────────────────────────────────────────────

INSERT_SQL = """
    INSERT INTO decisores_obra
        (obra_id, nome, cargo, tipo_cargo, fonte, registrado_por, registrado_em)
    SELECT %s, %s, %s, %s, 'brasilapi_qsa', 'auto:brasilapi_qsa_v1', now()
    WHERE NOT EXISTS (
        SELECT 1 FROM decisores_obra
        WHERE obra_id = %s AND nome = %s AND excluido_em IS NULL
    )
"""

LOG_SQL = """
    INSERT INTO enriquecimento_log (obra_id, campo, valor_novo, fonte, criado_em)
    VALUES (%s, '_processado', %s, 'brasilapi_qsa', now())
"""

# Candidatas: PIPELINE (canonical fragment) com CNPJ ok, sem decisor OURO,
# e sem entrada prévia no enriquecimento_log marcada como brasilapi_qsa.
CANDIDATAS_SQL = """
    SELECT o.id, o.nome, o.empresa, o.cnpj, o.valor_estimado
    FROM obras o
    WHERE o.fase IN ('EM_EXECUCAO','PLANEJAMENTO','LICENCA_INSTALACAO','LICENCA_PREVIA')
      AND o.valor_estimado IS NOT NULL AND o.valor_estimado >= 100000000
      AND COALESCE(o.nivel1_nome,'') = ''
      AND COALESCE(o.fonte_tipo,'OFICIAL') <> 'NOTICIA'
      AND o.cnpj IS NOT NULL AND o.cnpj <> ''
      AND COALESCE(o.cnpj_status,'ok') = 'ok'
      AND cnpj_valido(o.cnpj)
      AND NOT EXISTS (
          SELECT 1 FROM decisores_obra d
          WHERE d.obra_id = o.id AND d.excluido_em IS NULL
            AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo <> 'OUTRO'
      )
      AND NOT EXISTS (
          SELECT 1 FROM enriquecimento_log el
          WHERE el.obra_id = o.id AND el.fonte = 'brasilapi_qsa'
      )
    ORDER BY o.valor_estimado DESC NULLS LAST
    LIMIT %s
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true",
                        help="Persistir inserções e logs (default: dry-run)")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max obras a processar (default 20)")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur  = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(CANDIDATAS_SQL, (args.limit,))
    candidatas = cur.fetchall()

    print(f"\n{'='*70}")
    print(f"  MODO: {'COMMIT' if args.commit else 'DRY-RUN (nada gravado)'}")
    print(f"  Obras candidatas: {len(candidatas)}")
    print(f"  Rate limit: {RATE_LIMIT_SLEEP}s entre calls")
    print(f"  Tempo estimado: ~{max(0, len(candidatas) - 1) * RATE_LIMIT_SLEEP // 60}min")
    print(f"{'='*70}\n")

    total_inseridos     = 0
    total_obras_com_qsa = 0
    total_skip_sem_qsa  = 0
    total_skip_so_socio = 0

    for i, obra in enumerate(candidatas, 1):
        cnpj = re.sub(r"[^0-9]", "", obra["cnpj"] or "")
        print(f"[{i}/{len(candidatas)}] {(obra['nome'] or '')[:55]}  ({obra['empresa']})")

        qsa, status = buscar_qsa(cnpj)

        if status != "ok":
            print(f"    → {status} (CNPJ {cnpj})")
            total_skip_sem_qsa += 1
            if args.commit:
                cur.execute(LOG_SQL, (obra["id"], status))
                conn.commit()
            if i < len(candidatas):
                time.sleep(RATE_LIMIT_SLEEP)
            continue

        candidatos = []
        for socio in qsa:
            nome = socio.get("nome_socio") or ""
            qual = socio.get("qualificacao_socio") or ""
            tipo = mapear_qualificacao(qual)
            if nome and tipo != "OUTRO":
                candidatos.append({
                    "nome":       nome.strip().title(),
                    "cargo":      qual,
                    "tipo_cargo": tipo,
                })

        if not candidatos:
            print(f"    → QSA tem {len(qsa)} socios, todos OUTRO (puros)")
            total_skip_so_socio += 1
            if args.commit:
                cur.execute(LOG_SQL, (obra["id"], "so_socios"))
                conn.commit()
            if i < len(candidatas):
                time.sleep(RATE_LIMIT_SLEEP)
            continue

        total_obras_com_qsa += 1
        selecionados = candidatos[:2]
        for c in selecionados:
            print(f"    → {c['nome']:35} | {c['cargo'][:32]:32} | {c['tipo_cargo']}")
            if args.commit:
                cur.execute(INSERT_SQL, (
                    obra["id"], c["nome"], c["cargo"], c["tipo_cargo"],
                    obra["id"], c["nome"],
                ))
                if cur.rowcount:
                    total_inseridos += 1

        if args.commit:
            cur.execute(LOG_SQL, (obra["id"], f"ok:{len(selecionados)}"))
            conn.commit()

        if i < len(candidatas):
            time.sleep(RATE_LIMIT_SLEEP)

    if not args.commit:
        conn.rollback()

    print(f"\n{'='*70}")
    print(f"  Inserções:                 {total_inseridos}")
    print(f"  Obras com QSA aproveitavel:{total_obras_com_qsa}")
    print(f"  Skip (sem QSA / 404 / err):{total_skip_sem_qsa}")
    print(f"  Skip (so socios puros):    {total_skip_so_socio}")
    print(f"  Modo: {'COMMIT — persistido' if args.commit else 'DRY-RUN — nada gravado'}")
    print(f"  Nota: OURO requer email|linkedin; QSA so traz nome+cargo,")
    print(f"        entao obras nao viram OURO automatic. Curadoria humana depois.")
    print(f"{'='*70}\n")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
