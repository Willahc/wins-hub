"""
check_quota_hunter.py
Snapshot diário do saldo Hunter.io. Roda via cron.
INSERT em quota_snapshots + email Resend se pct_restante < THRESHOLD.

DRY-RUN por padrão (não envia email). Use --commit pra enviar.

Schema quota_snapshots: id, ts, service, used, available, pct_restante, reset_at, raw
"""
import argparse, json, os, sys
from datetime import datetime, date
import psycopg2
import urllib.request
import urllib.error

DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "db"),
    "dbname":   os.environ.get("DB_NAME", "wins_hub"),
    "user":     os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

HUNTER_API_KEY  = os.environ.get("HUNTER_API_KEY", "").strip()
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_FROM     = os.environ.get("RESEND_FROM", "WiNS HUB <contato@winshubcomercial.com.br>")
ALERT_TO        = os.environ.get("QUOTA_ALERT_TO", "williamvnvn@gmail.com")

THRESHOLD_PCT = 20.0


def fetch_hunter_account():
    """GET https://api.hunter.io/v2/account?api_key=K  → dict."""
    if not HUNTER_API_KEY:
        raise RuntimeError("HUNTER_API_KEY ausente no ambiente.")
    url = f"https://api.hunter.io/v2/account?api_key={HUNTER_API_KEY}"
    req = urllib.request.Request(url, headers={"User-Agent": "wins-hub-quota-check"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def insert_snapshot(conn, service, used, available, pct, reset_at, raw):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO quota_snapshots (service, used, available, pct_restante, reset_at, raw) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (service, used, available, pct, reset_at, json.dumps(raw)),
        )
        return cur.fetchone()[0]


def send_alert_email(used, available, pct, reset_at):
    if not RESEND_API_KEY:
        print("RESEND_API_KEY ausente — skipping email", file=sys.stderr)
        return False
    subject = f"⚠️ WiNS HUB — saldo Hunter baixo: {pct:.1f}%"
    body_html = (
        f"<p>Saldo Hunter abaixo de {THRESHOLD_PCT:.0f}%.</p>"
        f"<ul>"
        f"<li>Usado: <b>{used}</b></li>"
        f"<li>Disponível: <b>{available}</b></li>"
        f"<li>Restante: <b>{pct:.1f}%</b></li>"
        f"<li>Reset: <b>{reset_at}</b></li>"
        f"</ul>"
        f"<p>Topup ou aguarde reset; pipeline pode parar.</p>"
    )
    payload = json.dumps({
        "from":    RESEND_FROM,
        "to":      [ALERT_TO],
        "subject": subject,
        "html":    body_html,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload,
        headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                 "Content-Type":  "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"Email Resend OK → {ALERT_TO}: {r.status}")
            return True
    except urllib.error.HTTPError as e:
        print(f"Resend HTTPError {e.code}: {e.read()[:200]}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Envia email se saldo < threshold. Sem flag = dry-run.")
    args = ap.parse_args()

    raw = fetch_hunter_account()
    data = raw.get("data", {})
    requests_obj = data.get("requests", {}).get("searches", {})
    used = int(requests_obj.get("used", 0))
    available = int(requests_obj.get("available", 0))
    restante = max(0, available - used)
    pct = (restante / available * 100.0) if available > 0 else 0.0
    reset_str = data.get("reset_date") or None  # YYYY-MM-DD or None
    reset_at = None
    if reset_str:
        try:
            reset_at = date.fromisoformat(reset_str)
        except (TypeError, ValueError):
            reset_at = None

    print(f"[Hunter] used={used} available={available} restante={restante} pct={pct:.1f}% reset={reset_at}")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        snap_id = insert_snapshot(conn, "hunter", used, available, pct, reset_at, data)
        conn.commit()
        print(f"snapshot id={snap_id}")
    finally:
        conn.close()

    if pct < THRESHOLD_PCT:
        if args.commit:
            send_alert_email(used, available, pct, reset_at)
        else:
            print(f"DRY-RUN: pct {pct:.1f}% < {THRESHOLD_PCT}% — alert pendente. Use --commit pra enviar.")
    else:
        print(f"OK: pct {pct:.1f}% >= threshold {THRESHOLD_PCT}%.")


if __name__ == "__main__":
    main()
