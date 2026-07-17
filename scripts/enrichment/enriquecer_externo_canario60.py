#!/usr/bin/env python3
import json, os, re, time
from collections import Counter
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
import importlib.util
import httpx

spec = importlib.util.spec_from_file_location("ouro", "/tmp/aplicar_regra_ouro_final.py")
ouro = importlib.util.module_from_spec(spec); spec.loader.exec_module(ouro)

PERSONAL={"gmail.com","hotmail.com","outlook.com","yahoo.com","yahoo.com.br","icloud.com","live.com","uol.com.br"}
GENERIC={"contato","sac","comercial","compras","atendimento","info","rh","financeiro","vendas","admin","suporte"}

def digits(v): return re.sub(r"\D","",str(v or ""))
def extract_emails(text, domain):
    found=set(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text or ""))
    out=[]
    for e in found:
        e=e.lower().strip(".,;:()<>\"'")
        if domain and domain not in e.split("@")[-1]: continue
        if any(x in e for x in (".png",".jpg","example.com","wixpress")): continue
        out.append(e)
    return sorted(set(out))

c=psycopg2.connect(host=os.getenv("DB_HOST","db"), dbname="wins_hub",
    user=os.getenv("DB_USER") or "wins_app", password=os.getenv("DB_PASSWORD",""))
cur=c.cursor(cursor_factory=RealDictCursor)
cur2=c.cursor()

cur2.execute("SELECT grupo, count(*)::int FROM wins_v2.prata_segmentacao GROUP BY 1")
seg={r[0]: r[1] for r in cur2.fetchall()}
print("SEG", seg, "sum", sum(seg.values()), flush=True)

cur.execute("""
SELECT s.obra_id, s.grupo, o.empresa, o.cnpj, o.valor_estimado,
  d.id AS decisor_id, d.nome, d.cargo, d.email, d.linkedin_url,
  coalesce(ed.dominio, nullif(split_part(coalesce(d.email,''),'@',2),'')) AS dominio
FROM wins_v2.prata_segmentacao s
JOIN public.obras o ON o.id=s.obra_id
LEFT JOIN LATERAL (
  SELECT * FROM decisores_obra dob WHERE dob.obra_id=o.id AND dob.excluido_em IS NULL
  ORDER BY (dob.email IS NOT NULL) DESC, confianca_match DESC NULLS LAST LIMIT 1
) d ON true
LEFT JOIN LATERAL (
  SELECT dominio FROM public.empresa_dominios ed
  WHERE regexp_replace(ed.cnpj,'\\D','','g')=regexp_replace(coalesce(o.cnpj,''),'\\D','','g')
  ORDER BY confianca DESC NULLS LAST LIMIT 1
) ed ON true
WHERE s.grupo IN ('H_REUSO_INSUFICIENTE_REBAIXADO','A_FALTA_SOMENTE_EMAIL_VALIDADO','B_EMAIL_EXISTE_MAS_PRECISA_VALIDACAO')
  AND s.decisor_ok AND s.cargo_ok AND s.linkedin_ok AND s.cnpj_ok AND s.capex_ok
ORDER BY CASE s.grupo WHEN 'H_REUSO_INSUFICIENTE_REBAIXADO' THEN 1 WHEN 'A_FALTA_SOMENTE_EMAIL_VALIDADO' THEN 2 ELSE 3 END,
  o.valor_estimado DESC NULLS LAST
LIMIT 60
""")
queue=cur.fetchall()
print("queue", len(queue), flush=True)

client=httpx.Client(timeout=httpx.Timeout(3.0, connect=2.0), headers={"User-Agent":"WiNSHubBot/1.0"}, follow_redirects=True)
stats=Counter(); audits=[]; promoted=0
for i,s in enumerate(queue):
    if i%5==0: print(f"progress {i}/{len(queue)}", flush=True)
    oid=s["obra_id"]; nome=s.get("nome") or ""; domain=(s.get("dominio") or "").lower().replace("www.","")
    if not nome or not domain or domain in PERSONAL:
        stats["skip"]+=1
        audits.append((oid,s["grupo"],"PRATA","PRATA",nome,s.get("empresa"),digits(s.get("cnpj")),domain,None,"NAO_VALIDADO","httpx","skip",None,"skip",0.1,0,"ouro8","canario60","skip"))
        continue
    found=None; url_hit=None
    for path in ["/", "/contato", "/contact"]:
        try:
            r=client.get(f"https://{domain}{path}")
            stats["pages"]+=1
            if r.status_code>=400: continue
            for em in extract_emails(r.text, domain):
                loc=em.split("@")[0]
                if loc.split(".")[0] in GENERIC or re.sub(r"[^a-z0-9]","",loc) in GENERIC:
                    stats["generic"]+=1; continue
                if ouro.email_nominal_match(nome, em):
                    found=em; url_hit=str(r.url); break
            if found: break
        except Exception:
            stats["http_err"]+=1
    if found:
        stats["found"]+=1
        if s.get("decisor_id"):
            cur.execute("UPDATE decisores_obra SET email=%s, email_status='valid', email_verificado_em=now() WHERE id=%s",(found,s["decisor_id"]))
        cur.execute("SELECT id,cnpj,valor_estimado,empresa,classificacao_computed,status_portao FROM obras WHERE id=%s",(oid,))
        o2=cur.fetchone()
        cur.execute("SELECT * FROM decisores_obra WHERE obra_id=%s AND excluido_em IS NULL",(oid,))
        decs=cur.fetchall()
        for d in decs:
            if d.get("id")==s.get("decisor_id"):
                d["email"]=found; d["email_status"]="valid"
        res=ouro.calc_tier(dict(o2), decs)
        if res["tier"]=="OURO" and float(o2.get("valor_estimado") or 0)>=100000:
            cur.execute("UPDATE obras SET classificacao_computed='OURO' WHERE id=%s AND status_portao='APROVADA'",(oid,))
            promoted+=1; stats["promoted"]+=1; tier_n="OURO"; motivo="site oficial"
        else:
            tier_n="PRATA"; stats["stay"]+=1; motivo=str(res.get("ausentes"))
        audits.append((oid,s["grupo"],"PRATA",tier_n,nome,s.get("empresa"),digits(s.get("cnpj")),domain,found,"VALIDADO_FONTE_OFICIAL","httpx","site_oficial",url_hit,f"publicado {url_hit}",0.9,0,"ouro8","canario60",motivo))
        cur.execute("""INSERT INTO wins_v2.email_validation_cache(email,status,ferramenta,detalhes) VALUES (%s,'VALIDADO_FONTE_OFICIAL','httpx',%s::jsonb)
          ON CONFLICT (email) DO UPDATE SET status=EXCLUDED.status, atualizado_em=now()""",(found.lower(), json.dumps({"url":url_hit})))
    else:
        stats["not_found"]+=1
        audits.append((oid,s["grupo"],"PRATA","PRATA",nome,s.get("empresa"),digits(s.get("cnpj")),domain,s.get("email"),"NAO_VALIDADO","httpx","site_oficial",None,"nao encontrado",0.2,0,"ouro8","canario60","nao encontrado"))

if audits:
    execute_batch(cur,"""INSERT INTO wins_v2.prata_external_enrich_audit
      (obra_id,grupo,tier_anterior,tier_novo,decisor,empresa,cnpj,dominio,email,email_status,ferramenta,fonte,url,evidencia,confianca,creditos_consumidos,regra,lote,motivo)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", audits)
c.commit()
cur2.execute("SELECT classificacao_computed, count(*)::int FROM obras WHERE status_portao='APROVADA' GROUP BY 1")
tiers={r[0]:r[1] for r in cur2.fetchall()}
report={"segmentacao":seg,"segmentacao_soma":sum(seg.values()),"queue":len(queue),"stats":dict(stats),
        "promoted":promoted,"audits":len(audits),"tiers_finais":tiers,"hunter":0,"zerobounce":0,"abstract":0,"custo":0,
        "pages":stats.get("pages",0),"finished":datetime.now(timezone.utc).isoformat()}
print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
open("/tmp/ext_mini_report.json","w").write(json.dumps(report, ensure_ascii=False, indent=2, default=str))
client.close(); c.close()
