"""
crm_followup_creditos.py
Roda 07:00 seg-sex. Duas tarefas:
1. Enviar email de follow-up pra contatos com proximo_followup_em = hoje
2. Avisar prestadores com créditos expirando em 30 dias
"""
import os, json
from datetime import datetime, timezone, timedelta
import urllib.request
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "db"),
    "dbname": os.environ.get("DB_NAME", "wins_hub"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "WiNS HUB <contato@winshubcomercial.com.br>")
SITE_URL = "https://winshubcomercial.com.br"

def enviar(to_email, subject, html):
    if not RESEND_API_KEY:
        print(f"  [DRY] sem RESEND_API_KEY — não envio pra {to_email}")
        return False
    payload = {"from": RESEND_FROM, "to": [to_email], "subject": subject, "html": html}
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                 "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"  ❌ {e}")
        return False

def main():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    hoje = datetime.now(timezone.utc).date()
    enviados = 0

    cur.execute("""
        SELECT cl.id, cl.obra_id, cl.notas, cl.followup_count,
               o.nome AS obra_nome, o.empresa,
               p.email, p.nome_empresa
        FROM contatos_log cl
        JOIN obras o ON o.id = cl.obra_id
        JOIN prestadores p ON p.id = cl.prestador_id
        WHERE DATE(cl.proximo_followup_em) = %s
          AND p.email IS NOT NULL
    """, (hoje,))
    followups = cur.fetchall()

    for f in followups:
        nome = f["nome_empresa"] or "Fornecedor"
        notas_html = f'<br><em style="color:#6B7893;font-size:11px;">{f["notas"][:100]}</em>' if f.get("notas") else ''
        html = f"""
        <div style="font-family:Arial;max-width:580px;margin:0 auto;
                    background:#0A1628;padding:20px;border-radius:12px;">
          <h1 style="color:#F5A623;font-size:18px;">WiNS Hub</h1>
          <h2 style="color:#fff;font-size:15px;">📞 Lembrete de follow-up</h2>
          <p style="color:#8DA0B3;font-size:13px;">
            Olá {nome.split()[0]}, hoje é o dia de retornar o contato com:
          </p>
          <div style="background:#1A2E42;border-radius:8px;padding:14px;margin:12px 0;">
            <strong style="color:#fff;">{f['obra_nome']}</strong><br>
            <span style="color:#8DA0B3;font-size:12px;">{f['empresa'] or ''}</span>
            {notas_html}
          </div>
          <a href="{SITE_URL}/perfil"
             style="display:block;text-align:center;background:#F5A623;
                    color:#0D1B2A;padding:10px;border-radius:24px;
                    font-weight:700;text-decoration:none;font-size:13px;">
            Registrar resultado do contato →
          </a>
        </div>"""
        if enviar(f["email"], f"📞 Follow-up: {f['obra_nome'][:40]} — WiNS Hub", html):
            enviados += 1
            print(f"  ✓ Follow-up: {f['email']} → {f['obra_nome'][:40]}")

    expira_em = hoje + timedelta(days=30)
    cur.execute("""
        SELECT email, nome_empresa,
               creditos_ganhos - creditos_consumidos AS saldo_centavos,
               creditos_expiram_em
        FROM prestadores
        WHERE DATE(creditos_expiram_em) = %s
          AND creditos_ganhos - creditos_consumidos > 0
          AND email IS NOT NULL
    """, (expira_em,))
    expirando = cur.fetchall()

    for e in expirando:
        saldo = f"R${e['saldo_centavos']//100}"
        nome = e["nome_empresa"] or "Fornecedor"
        html = f"""
        <div style="font-family:Arial;max-width:580px;margin:0 auto;
                    background:#0A1628;padding:20px;border-radius:12px;">
          <h1 style="color:#F5A623;font-size:18px;">WiNS Hub</h1>
          <h2 style="color:#E74C3C;font-size:15px;">⚠️ Créditos expirando em 30 dias</h2>
          <p style="color:#8DA0B3;font-size:13px;">
            Olá {nome.split()[0]}, você tem <strong style="color:#F5A623;">{saldo}</strong>
            em créditos WiNS que expiram em 30 dias.
          </p>
          <p style="color:#8DA0B3;font-size:12px;">
            Use seus créditos para desbloquear obras e acessar decisores antes que expirem.
          </p>
          <a href="{SITE_URL}/projetos"
             style="display:block;text-align:center;background:#F5A623;
                    color:#0D1B2A;padding:10px;border-radius:24px;
                    font-weight:700;text-decoration:none;font-size:13px;">
            Usar créditos agora →
          </a>
          <p style="color:#374151;font-size:10px;text-align:center;margin-top:12px;">
            Créditos válidos exclusivamente na plataforma WiNS Hub.
          </p>
        </div>"""
        if enviar(e["email"], f"⚠️ {saldo} em créditos expirando em 30 dias — WiNS Hub", html):
            enviados += 1
            print(f"  ✓ Expiração: {e['email']} → {saldo}")

    print(f"\nTotal enviados: {enviados} | Follow-ups: {len(followups)} | Expirando: {len(expirando)}")
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
