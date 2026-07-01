"""Discover CNPJ dinâmico via Serper + Sonnet/Gemini pra obras NULL com empresa sem cnpj válido.

Pipeline:
1. SELECT obras NULL tier sem cnpj válido com empresa
2. Pra cada: Serper "{empresa} CNPJ Brasil"
3. Sonnet extrai CNPJ dos snippets + answer box
4. Valida via cnpj_valido() postgres
5. UPDATE obras SET cnpj=X (trigger trg_zerar_cnpj_invalido protege caso passe inválido)

Uso (dentro do container wins_hub-api-1):
  python /app/scripts/discover_cnpj_dynamic.py --limit 2 --dry-run   # smoke test
  python /app/scripts/discover_cnpj_dynamic.py --commit              # full run
"""
import argparse, os, re, sys, time, json
import requests, psycopg2

sys.path.insert(0, "/app")
from services.llm_haiku_compat import _haiku_client

SONNET_MODEL = "claude-haiku-4-5-20251001"
SERPER_URL = "https://google.serper.dev/search"

def serper(q, key):
    r = requests.post(SERPER_URL,
        headers={'X-API-KEY': key, 'Content-Type': 'application/json'},
        json={'q': q, 'gl': 'br', 'hl': 'pt', 'num': 5},
        timeout=20)
    r.raise_for_status()
    j = r.json()
    parts = []
    if j.get('answerBox'):
        parts.append(f"ANSWER: {j['answerBox']}")
    for it in (j.get('organic') or [])[:5]:
        parts.append(f"- {it.get('title','')} | {it.get('snippet','')}")
    if j.get('knowledgeGraph'):
        parts.append(f"KG: {j['knowledgeGraph']}")
    return "\n".join(parts)[:3000]

PROMPT = """Você extrai CNPJ. RESPOSTA = APENAS UM OBJETO JSON. NÃO escreva análise, preâmbulo, ou texto fora do JSON.

Empresa: {empresa}
Contexto: {nome}

Serper:
{serper}

Output OBRIGATORIO neste formato exato (uma linha, sem fences, sem prosa):
{{"cnpj":"DDDDDDDDDDDDDD","confianca":N,"motivo":"breve"}}

cnpj: 14 dígitos OU null se não certeza.
confianca: 0-100. Use null no cnpj se <75.
Aceitar CNPJ governo/estado/municipio quando empresa for ente publico.
"""

def extract_cnpj(client, empresa, nome, serper_text):
    prompt = PROMPT.format(empresa=empresa, nome=(nome or "")[:200], serper=serper_text)
    resp = client.messages.create(model=SONNET_MODEL, max_tokens=200,
        messages=[{"role":"user","content":prompt}])
    txt = resp.content[0].text.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt)
        txt = txt.strip()
    idx = txt.find('{')
    if idx < 0:
        return None, 0, f"no_json:{txt[:80]}"
    try:
        d, _ = json.JSONDecoder().raw_decode(txt[idx:])
        cnpj = (d.get("cnpj") or "").strip() if d.get("cnpj") else None
        if cnpj:
            cnpj = re.sub(r"\D", "", cnpj)
            if len(cnpj) != 14:
                cnpj = None
        return cnpj, int(d.get("confianca") or 0), (d.get("motivo") or "")[:200]
    except Exception as e:
        return None, 0, f"parse_err:{e}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confianca-min", type=int, default=75)
    args = ap.parse_args()

    if not args.commit and not args.dry_run:
        print("ERRO: use --commit ou --dry-run", file=sys.stderr); sys.exit(2)

    serper_key = os.environ["SERPER_API_KEY"]
    client = _haiku_client()
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST","db"), port=int(os.getenv("DB_PORT","5432")),
        dbname=os.getenv("DB_NAME","wins_hub"), user=os.getenv("DB_USER","postgres"),
        password=os.getenv("DB_PASSWORD",""))
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("""
        SELECT id, empresa, nome FROM obras
        WHERE motivo_invisivel IS NULL
          AND classificacao_computed IS NULL
          AND empresa IS NOT NULL AND empresa <> ''
          AND (cnpj IS NULL OR cnpj = '' OR NOT cnpj_valido(cnpj))
        ORDER BY criado_em ASC
    """ + (f" LIMIT {args.limit}" if args.limit else ""))
    rows = cur.fetchall()
    print(f"[{len(rows)} obras pra processar dry_run={args.dry_run}]", flush=True)

    ok = skip = err = 0
    for obra_id, empresa, nome in rows:
        try:
            sr = serper(f"{empresa} CNPJ Brasil", serper_key)
            cnpj, conf, motivo = extract_cnpj(client, empresa, nome, sr)
            if not cnpj or conf < args.confianca_min:
                skip += 1
                print(f"SKIP {obra_id} {empresa[:35]!r} conf={conf} motivo={motivo[:60]}", flush=True)
                continue
            # valida DV no banco
            cur.execute("SELECT cnpj_valido(%s)", (cnpj,))
            if not cur.fetchone()[0]:
                skip += 1
                print(f"INVALID_DV {obra_id} cnpj={cnpj}", flush=True)
                continue
            if args.commit:
                cur.execute(
                    "UPDATE obras SET cnpj=%s, "
                    "observacoes_validacao=COALESCE(observacoes_validacao,'')||' | cnpj_discover_sonnet_30052026' "
                    "WHERE id=%s",
                    (cnpj, obra_id))
                conn.commit()
            ok += 1
            print(f"OK {obra_id} {empresa[:35]!r} -> {cnpj} conf={conf}", flush=True)
        except Exception as e:
            err += 1
            print(f"ERR {obra_id} {empresa[:35]!r}: {e}", flush=True)
            conn.rollback()
        time.sleep(0.3)  # rate limit

    print(f"\n=== TOTAL: ok={ok} skip={skip} err={err} (commit={args.commit}) ===", flush=True)
    conn.close()

if __name__ == "__main__":
    main()
