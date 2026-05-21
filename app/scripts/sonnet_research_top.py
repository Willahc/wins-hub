"""
sonnet_research_top.py — Pesquisa profunda Sonnet 4.6 com web_search nos top decisores.

Custo ~$0.10-0.20/call. Para top 5 obras alto valor = ~$0.50-1.00 total.
"""
import json, os, re, sys
sys.path.insert(0, "/app")
import anthropic
import phonenumbers
import psycopg2

DB = {"host":os.getenv("DB_HOST","db"),"port":int(os.getenv("DB_PORT","5432")),
      "dbname":os.getenv("DB_NAME","wins_hub"),"user":os.getenv("DB_USER","postgres"),
      "password":os.getenv("DB_PASSWORD","")}

TOP = [
    {"obra_id":"b1b5c79d-558e-4c7e-8f12-9e732b249d32","decisor":"Daniel Novaes","empresa":"Vale S.A.","gap":"email + telefone direto"},
    {"obra_id":"71b91446-d91c-4dab-8d20-97b436ce06ea","decisor":"Alexandre Roloff","empresa":"Sabesp","gap":"telefone direto"},
    {"obra_id":"dc26d46e-c1ea-40d1-a536-848f351b410c","decisor":"Jorge Braga","empresa":"Petrobras","gap":"telefone direto"},
    {"obra_id":"feaaa9a0-7318-4b44-9a69-1f89c572ba40","decisor":"Jorge Bastos","empresa":"BAHER Fertilizantes","gap":"LinkedIn + telefone"},
    {"obra_id":"73aced45-363f-4b44-b48b-122de3541b8b","decisor":"Njabulo Xhakaza","empresa":"Omnia Group","gap":"email"},
]

client = anthropic.Anthropic()

PROMPT = """Pesquise o contato profissional público de {decisor}, executivo da empresa {empresa}.
Foco no que está faltando: {gap}.

Use web search. Retorne APENAS um JSON:
{{"email": "...", "telefone": "...", "linkedin": "...", "fonte_url": "...", "confianca": "alta|media|baixa"}}

Apenas dados publicamente disponíveis (press releases, sites RI, perfis profissionais). Sem inferência. Se não encontrar, retorne campos vazios."""

for case in TOP:
    print(f"\n=== {case['decisor']} @ {case['empresa']} (gap: {case['gap']}) ===")
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[{"type":"web_search_20250305","name":"web_search","max_uses":4}],
            messages=[{"role":"user","content":PROMPT.format(**case)}],
        )
        # Pega último text block
        last_text = ""
        for b in msg.content:
            if getattr(b, "type", "") == "text":
                last_text = b.text
        # Strip fences
        raw = re.sub(r"^```(?:json)?\s*","", last_text.strip())
        raw = re.sub(r"\s*```$","", raw)
        # Encontrar JSON
        m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                print(f"  Email: {data.get('email','')}")
                print(f"  Tel:   {data.get('telefone','')}")
                print(f"  LK:    {data.get('linkedin','')}")
                print(f"  Conf:  {data.get('confianca','')} | Fonte: {data.get('fonte_url','')[:80]}")
            except:
                print(f"  Parse err. Raw: {raw[:300]}")
        else:
            print(f"  No JSON. Raw: {last_text[:200]}")
    except Exception as e:
        print(f"  ERR: {e!r}")
