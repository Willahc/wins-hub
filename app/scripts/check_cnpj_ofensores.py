"""
check_cnpj_ofensores.py
Auto-monitor diário pra detectar CNPJs "guarda-chuva" no banco.
Envia email Resend se >=1 CNPJ aparecer com 5+ empresas distintas.

DRY-RUN por padrão. Use --commit pra enviar email.
"""
import argparse, json, os, sys
from datetime import datetime
import psycopg2
import urllib.request

DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "db"),
    "dbname":   os.environ.get("DB_NAME", "wins_hub"),
    "user":     os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_FROM    = os.environ.get("RESEND_FROM", "WiNS HUB <contato@winshubcomercial.com.br>")
ALERT_TO       = os.environ.get("QUOTA_ALERT_TO", "williamvnvn@gmail.com")

THRESHOLD_EMPRESAS = 5


def query_ofensores(conn):
    """Retorna list de (cnpj, n_empresas, sample_empresas) onde n_empresas >= THRESHOLD."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cnpj, COUNT(DISTINCT empresa) AS n_emp,
                   STRING_AGG(DISTINCT SUBSTRING(empresa,1,40), ' / ' ORDER BY SUBSTRING(empresa,1,40)) AS amostra
            FROM obras
            WHERE cnpj IS NOT NULL AND (visivel IS NULL OR visivel=true)
              AND empresa IS NOT NULL AND empresa <> ''
            GROUP BY cnpj
            HAVING COUNT(DISTINCT empresa) >= %s
            ORDER BY 2 DESC
            """,
            (THRESHOLD_EMPRESAS,),
        )
        return cur.fetchall()


def send_email(subject, body):
    if not RESEND_API_KEY:
        print("[warn] RESEND_API_KEY ausente — pulando envio")
        return
    payload = json.dumps({"from": RESEND_FROM, "to": [ALERT_TO], "subject": subject, "text": body}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f"[ok] Resend status {r.status}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true", help="Send email (default dry-run)")
    args = p.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        ofensores = query_ofensores(conn)
        print(f"[i] {len(ofensores)} CNPJs com >= {THRESHOLD_EMPRESAS} empresas distintas")
        for cnpj, n, amostra in ofensores[:10]:
            print(f"  {cnpj}: {n} empresas — {amostra[:80]}{'...' if len(amostra) > 80 else ''}")

        if not ofensores:
            print("[ok] Nenhum ofensor detectado")
            return 0

        body = "Detectados CNPJs com múltiplas empresas distintas no DB:\n\n"
        for cnpj, n, amostra in ofensores:
            body += f"• CNPJ {cnpj}: {n} empresas\n  {amostra[:200]}\n\n"
        body += f"\nGerado em {datetime.now().isoformat()}"
        subject = f"[WiNS HUB] {len(ofensores)} CNPJs ofensores detectados"

        if args.commit:
            send_email(subject, body)
            print("[ok] Email enviado")
        else:
            print("[DRY-RUN] Use --commit pra enviar email. Subject seria:", subject)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
