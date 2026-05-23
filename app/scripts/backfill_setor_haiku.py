"""
Backfill setor via Haiku 4.5 para obras visíveis com setor vazio/null.

Universo: obras WHERE motivo_invisivel IS NULL AND (setor IS NULL OR setor = '')
Modelo:   claude-haiku-4-5-20251001
Marker:   observacoes_validacao recebe sufixo " | auto:haiku_setor_v1"
          validacao_metodo = 'auto:haiku_setor_v1' (rollback fácil)
"""
import os, sys, json, time
import psycopg2
from anthropic import Anthropic

MODEL = "claude-haiku-4-5-20251001"
MARKER = "auto:haiku_setor_v1"

SETORES = [
    "AGRO", "AGROINDUSTRIAL", "ALIMENTOS_E_BEBIDAS", "AUTOMOTIVO_E_AUTOPECAS",
    "ENERGIA", "INDUSTRIAL", "INFRAESTRUTURA", "LATICINIOS", "LOGISTICO",
    "MINERACAO", "PAPEL_E_CELULOSE", "PETROLEO_GAS", "PORTUARIO", "QUIMICA",
    "SANEAMENTO", "SUCROENERGETICO", "TECNOLOGIA", "OUTRO",
]
SETOR_SET = set(SETORES)

SYSTEM = (
    "Você classifica obras/empreendimentos brasileiros em UM setor canônico. "
    "Responda EXCLUSIVAMENTE com JSON: {\"setor\": \"<UM_DOS_SETORES>\"}. "
    "Sem prosa, sem markdown, sem code fences. "
    "Setores válidos (escolha o mais específico que se aplica): "
    + ", ".join(SETORES) + ". "
    "Heurísticas: data center → TECNOLOGIA; refinaria/upstream/midstream/downstream/petróleo/gás → PETROLEO_GAS; "
    "mineração/minério/extração mineral → MINERACAO; rede elétrica/transmissão/geração elétrica → ENERGIA; "
    "saneamento/água/esgoto → SANEAMENTO; siderurgia/aço → INDUSTRIAL; celulose/papel → PAPEL_E_CELULOSE; "
    "porto/terminal portuário → PORTUARIO; terminal multimodal/ferrovia/logística → LOGISTICO; "
    "rodovia/aeroporto/mobilidade urbana → INFRAESTRUTURA; automotivo/autopeças → AUTOMOTIVO_E_AUTOPECAS; "
    "açúcar/etanol/usina sucroenergética → SUCROENERGETICO; agroindústria/processamento agrícola → AGROINDUSTRIAL; "
    "agropecuária pura → AGRO; alimentos/bebidas/laticínios → ALIMENTOS_E_BEBIDAS (laticínios → LATICINIOS); "
    "química/petroquímica → QUIMICA. Se nada se aplicar, use OUTRO."
)


def classify(client: Anthropic, nome: str, descricao: str) -> str:
    user = f"Nome: {nome}\nDescrição: {(descricao or '')[:600]}\n\nResponda apenas o JSON."
    r = client.messages.create(
        model=MODEL,
        max_tokens=40,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    txt = r.content[0].text.strip()
    if txt.startswith("```"):
        txt = txt.strip("`").lstrip("json").strip()
    try:
        data = json.loads(txt)
        s = (data.get("setor") or "").strip().upper()
    except Exception:
        return "OUTRO"
    return s if s in SETOR_SET else "OUTRO"


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    conn.autocommit = False
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, nome, COALESCE(descricao,'') AS descricao,
                   classificacao_computed, fonte
            FROM obras
            WHERE motivo_invisivel IS NULL
              AND (setor IS NULL OR setor = '')
            ORDER BY classificacao_computed, valor_estimado DESC NULLS LAST
        """)
        rows = cur.fetchall()

    print(f"[backfill_setor_haiku] {len(rows)} obras candidatas", flush=True)
    counts = {}
    updates = []
    for i, (oid, nome, desc, classe, fonte) in enumerate(rows, 1):
        try:
            setor = classify(client, nome, desc)
        except Exception as e:
            print(f"  [{i}/{len(rows)}] ERRO {oid}: {e}", flush=True)
            continue
        counts[setor] = counts.get(setor, 0) + 1
        updates.append((setor, oid))
        print(f"  [{i}/{len(rows)}] {classe:8s} {fonte:16s} -> {setor:24s} | {nome[:70]}", flush=True)

    if not updates:
        print("[backfill_setor_haiku] nada a atualizar.", flush=True)
        conn.close()
        return

    with conn.cursor() as cur:
        cur.executemany("""
            UPDATE obras
               SET setor = %s,
                   observacoes_validacao = COALESCE(observacoes_validacao || ' | ', '') || %s,
                   validacao_metodo = %s,
                   validacao_data = CURRENT_DATE
             WHERE id = %s
        """, [(s, MARKER, MARKER, oid) for (s, oid) in updates])
    conn.commit()

    print("\n[backfill_setor_haiku] distribuição final:", flush=True)
    for s, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {s:24s} {n}", flush=True)
    print(f"\n[backfill_setor_haiku] {len(updates)} obras atualizadas. marker={MARKER}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
