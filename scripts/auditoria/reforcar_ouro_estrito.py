#!/usr/bin/env python3
"""Segundo passe: mantém OURO só com e-mail status valid/smtp_ok OU telefone validado de baixa frequência com fonte direta."""
import json, os, re
from collections import Counter
import psycopg2
from psycopg2.extras import RealDictCursor

def connect():
    return psycopg2.connect(host=os.getenv("DB_HOST","db"), dbname=os.getenv("DB_NAME","wins_hub"),
                            user=os.getenv("DB_USER","wins_app"), password=os.getenv("DB_PASSWORD",""))

def digits(v): return re.sub(r"\D","",str(v or ""))

def email_gen(em):
    return (em or "").split("@")[0].lower() in {
        "contato","sac","ouvidoria","info","informacoes","noreply","no-reply","admin","suporte",
        "financeiro","rh","atendimento","comercial","vendas","secretaria","protocolo"}

def main():
    conn=connect(); cur=conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
      SELECT regexp_replace(telefone,'\\D','','g') tel, COUNT(DISTINCT obra_id) n
      FROM decisores_obra WHERE excluido_em IS NULL AND NULLIF(telefone,'') IS NOT NULL GROUP BY 1""")
    tf={r["tel"]:int(r["n"]) for r in cur.fetchall() if r["tel"]}
    cur.execute("""
      SELECT o.id::text id FROM public.obras o
      WHERE o.status_portao='APROVADA' AND o.classificacao_computed='OURO'""")
    ouros=[r["id"] for r in cur.fetchall()]
    stats=Counter(); rebaixados=[]
    for oid in ouros:
        cur.execute("""
          SELECT nome,cargo,email,telefone,telefone_fonte,linkedin_url,email_status,email_smtp_status,whatsapp_status,fonte
          FROM decisores_obra WHERE obra_id=%s AND excluido_em IS NULL
            AND (hipotese_replicacao IS NULL OR hipotese_replicacao <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO')
        """,(oid,))
        decs=cur.fetchall()
        ok=False; canal="SEM_CANAL"; meta={}; dec=None
        for d in decs:
            em=(d.get("email") or "").strip()
            est=(d.get("email_status") or "").lower()
            smtp=(d.get("email_smtp_status") or "").lower()
            tel=digits(d.get("telefone"))
            wa=(d.get("whatsapp_status") or "").lower()
            tfonte=(d.get("telefone_fonte") or "").lower()
            if em and "@" in em and not email_gen(em) and (est in ("valido","valid","ok","smtp_ok") or smtp in ("valid","valido","ok")):
                ok=True; canal="EMAIL_NOMINAL_VALIDADO"; meta={"email":em}; dec=d; break
            if tel and len(tel)>=10 and wa in ("confirmado","confirmed","ok","validado","sim"):
                ok=True; canal="WHATSAPP_CONFIRMADO"; meta={"telefone":tel}; dec=d; break
            if tel and len(tel)>=10 and tf.get(tel,0)<10 and any(x in tfonte for x in ("direto","ramal","departamento","setor")):
                ok=True; canal="DIRETO_VALIDADO" if "departamento" not in tfonte else "DEPARTAMENTO_VALIDADO"
                meta={"telefone":tel,"telefone_fonte":tfonte}; dec=d; break
        if ok:
            stats["mantidos"]+=1
            stats[canal]+=1
            continue
        # classify main weak canal for audit
        for d in decs:
            tel=digits(d.get("telefone")); em=(d.get("email") or "").strip()
            if tel and tf.get(tel,0)>=10: canal,meta="GERAL_EMPRESA",{"telefone":tel,"freq":tf.get(tel)}; dec=d; break
            if tel: canal,meta="NAO_VALIDADO",{"telefone":tel}; dec=d; break
            if em: canal,meta="INFERIDO",{"email":em}; dec=d; break
            if d.get("linkedin_url"): canal,meta="LINKEDIN_SOMENTE",{"linkedin":d.get("linkedin_url")}; dec=d; break
        motivo=f"OURO estrito: sem canal acionável validado; canal={canal}"
        cur.execute("""UPDATE public.obras SET classificacao_computed='PRATA', observacoes_enrichment=left(%s,500)
                       WHERE id=%s AND classificacao_computed='OURO'""", (f"tier_coerencia_v2:{motivo}", oid))
        cur.execute("""INSERT INTO wins_v2.tier_coerencia_audit
            (obra_id,tier_anterior,tier_novo,motivo,telefone,tipo_telefone,fonte,confianca,decisor,cargo,canal_meta,regra_aplicada)
            VALUES (%s,'OURO','PRATA',%s,%s,%s,%s,0.7,%s,%s,%s::jsonb,'TIER_COERENCIA_ESTRITO_V2')""",
            (oid, motivo[:500], meta.get("telefone") or digits((dec or {}).get("telefone")), canal,
             (dec or {}).get("fonte"), (dec or {}).get("nome"), (dec or {}).get("cargo"),
             json.dumps({"canal":canal,"meta":meta},ensure_ascii=False,default=str)))
        conn.commit()
        stats["rebaixados"]+=1; stats[f"rebaixado_{canal}"]+=1
        rebaixados.append({"obra_id":oid,"canal":canal})
    cur.execute("SELECT COUNT(*) n FROM obras WHERE status_portao='APROVADA' AND classificacao_computed='OURO'")
    ouro=cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) n FROM obras WHERE status_portao='APROVADA' AND classificacao_computed='PRATA'")
    prata=cur.fetchone()["n"]
    cur.execute("SELECT classificacao_computed FROM obras WHERE id='b72d3db9-875b-4ec4-8678-4f574acecb93'")
    pilot=cur.fetchone()["classificacao_computed"]
    out={"stats":dict(stats),"ouro_final":ouro,"prata_final":prata,"piloto":pilot,"rebaixados_extra":stats["rebaixados"]}
    print(json.dumps(out,ensure_ascii=False,indent=2))
    open("/tmp/tier_estrito.json","w").write(json.dumps(out,indent=2))
    conn.close(); return 0
if __name__=="__main__":
    raise SystemExit(main())
