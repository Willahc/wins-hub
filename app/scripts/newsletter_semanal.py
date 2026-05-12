"""
newsletter_semanal.py
Envia newsletter semanal pra subscribers confirmados+ativos:
  - 3 obras top da semana (valor + fase relevante)
  - 1 insight automatico de mercado (total movimentado, top setor/UF)
DRY-RUN por padrao. Use --commit pra enviar de verdade.

Cron: 0 9 * * 1 (segunda 09:00 BRT, depois do alerta_semanal das 08:00)
"""
import argparse, os, sys
from datetime import datetime, timezone, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
import urllib.request, json as _json

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "db"),
    "dbname": os.environ.get("DB_NAME", "wins_hub"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM    = os.environ.get("RESEND_FROM", "WiNS HUB <contato@winshubcomercial.com.br>")
SITE_URL       = os.environ.get("APP_URL", "https://winshubcomercial.com.br")

FASES_RELEVANTES = (
    'PLANEJAMENTO', 'EM_EXECUCAO',
    'LICENCA_INSTALACAO', 'LICENCA_PREVIA',
    'PROJETO', 'LICITACAO_ABERTA',
)

JANELA_DIAS = 7


def fmt_brl(v):
    if v is None:
        return "—"
    try:
        v = float(v)
    except Exception:
        return "—"
    if v >= 1e9:
        return f"R$ {v/1e9:.2f} bi".replace(".", ",")
    if v >= 1e6:
        return f"R$ {v/1e6:.1f} mi".replace(".", ",")
    return f"R$ {v:,.0f}".replace(",", ".")


def buscar_top_obras(cur, since, n=3):
    cur.execute("""
        SELECT id, nome, empresa, uf, setor, fase, valor_estimado, lead_score
        FROM obras
        WHERE visivel = true
          AND criado_em >= %s
          AND fase = ANY(%s)
          AND valor_estimado IS NOT NULL
        ORDER BY valor_estimado DESC NULLS LAST, lead_score DESC NULLS LAST
        LIMIT %s
    """, (since, list(FASES_RELEVANTES), n))
    return cur.fetchall()


def calcular_insight(cur, since):
    cur.execute("""
        SELECT
            COUNT(*) AS num_obras,
            SUM(valor_estimado) AS total_valor,
            (SELECT setor FROM obras
               WHERE visivel=true AND criado_em >= %s AND setor IS NOT NULL
               GROUP BY setor ORDER BY COUNT(*) DESC, SUM(COALESCE(valor_estimado,0)) DESC
               LIMIT 1) AS top_setor,
            (SELECT uf FROM obras
               WHERE visivel=true AND criado_em >= %s AND uf IS NOT NULL
               GROUP BY uf ORDER BY COUNT(*) DESC, SUM(COALESCE(valor_estimado,0)) DESC
               LIMIT 1) AS top_uf
        FROM obras
        WHERE visivel = true AND criado_em >= %s
    """, (since, since, since))
    return cur.fetchone()


def buscar_subscribers(cur):
    cur.execute("""
        SELECT email, nome, token_confirmacao
        FROM newsletter_subscribers
        WHERE ativo = TRUE AND confirmado_em IS NOT NULL
        ORDER BY criado_em
    """)
    return cur.fetchall()


def render_html(nome, obras, insight, token):
    saudacao = f"Ola{(' ' + nome) if nome else ''},"
    cancelar = f"{SITE_URL}/api/newsletter/cancelar?token={token}"
    obras_html = ""
    for o in obras:
        meta = " · ".join(filter(None, [o.get("empresa") or None, o.get("uf"), o.get("setor")]))
        obras_html += f"""
        <tr><td style="padding:14px 18px;border:1px solid #1f2942;border-radius:8px;background:#0f1525;">
          <p style="margin:0 0 4px;font-size:14px;font-weight:700;color:#e5e7eb;">{o['nome'] or '—'}</p>
          <p style="margin:0 0 8px;font-size:11px;color:#9ca3af;letter-spacing:.3px;">{meta}</p>
          <p style="margin:0;font-size:13px;color:#fbbf24;font-weight:700;">{fmt_brl(o.get('valor_estimado'))}</p>
        </td></tr>
        <tr><td style="height:10px;"></td></tr>
        """

    num_obras = insight["num_obras"] or 0
    total_valor = fmt_brl(insight["total_valor"])
    top_setor = insight["top_setor"] or "—"
    top_uf = insight["top_uf"] or "—"
    insight_html = (
        f"<strong style='color:#fbbf24;'>{num_obras}</strong> obras novas, "
        f"<strong style='color:#fbbf24;'>{total_valor}</strong> em valor estimado. "
        f"Setor lider: <strong style='color:#e5e7eb;'>{top_setor}</strong>. "
        f"UF lider: <strong style='color:#e5e7eb;'>{top_uf}</strong>."
    )

    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0a0e1a;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#e8ecf4;">
  <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#0a0e1a;">
    <tr><td align="center" style="padding:32px 16px;">
      <table cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;">
        <tr><td style="padding:0 8px 24px;">
          <span style="font-size:22px;font-weight:800;letter-spacing:1.5px;color:#f5b800;">WiNS HUB</span>
          <span style="font-size:11px;color:#6b7693;text-transform:uppercase;letter-spacing:1.2px;margin-left:8px;">Newsletter Semanal</span>
        </td></tr>
        <tr><td style="padding:0 8px 16px;color:#a8b1c9;font-size:14px;line-height:1.6;">
          <p style="margin:0 0 8px;">{saudacao}</p>
          <p style="margin:0;">As 3 maiores obras de infraestrutura adicionadas a base WiNS HUB nos ultimos 7 dias:</p>
        </td></tr>
        <tr><td style="padding:8px 0 24px;">
          <table cellpadding="0" cellspacing="0" border="0" width="100%">{obras_html}</table>
        </td></tr>
        <tr><td style="padding:16px 18px;background:#0f1525;border:1px solid #1f2942;border-left:3px solid #fbbf24;border-radius:6px;color:#a8b1c9;font-size:13px;line-height:1.6;">
          <p style="margin:0 0 4px;font-size:11px;color:#fbbf24;text-transform:uppercase;letter-spacing:.5px;font-weight:700;">Insight da semana</p>
          <p style="margin:0;">{insight_html}</p>
        </td></tr>
        <tr><td align="center" style="padding:28px 0 16px;">
          <a href="{SITE_URL}" style="display:inline-block;background:#f5b800;color:#0a0e1a;text-decoration:none;padding:11px 26px;border-radius:6px;font-weight:700;letter-spacing:.4px;font-size:13px;">Ver todas as obras</a>
        </td></tr>
        <tr><td style="padding:24px 8px 0;border-top:1px solid #1f2942;color:#6b7693;font-size:11px;line-height:1.6;text-align:center;letter-spacing:.3px;">
          WiNS HUB &middot; Inteligencia Comercial<br>
          <a href="{SITE_URL}" style="color:#6b7693;text-decoration:none;">winshubcomercial.com.br</a><br><br>
          <a href="{cancelar}" style="color:#6b7693;text-decoration:underline;">Cancelar inscricao</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def enviar_email(to, subject, html):
    if not RESEND_API_KEY:
        print(f"  [DRY] sem RESEND_API_KEY — nao envio pra {to}")
        return False
    payload = _json.dumps({"from": RESEND_FROM, "to": [to], "subject": subject, "html": html}).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            print(f"  OK {to} — {body[:120]}")
            return True
    except Exception as exc:
        print(f"  ERR {to} — {exc}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="Envia emails de verdade")
    ap.add_argument("--limit", type=int, default=None, help="Limita N subscribers (debug)")
    args = ap.parse_args()

    modo = "COMMIT" if args.commit else "DRY-RUN"
    print(f"=== Newsletter Semanal — {modo} ===")
    print(f"Iniciado em {datetime.now(timezone.utc).isoformat()}")

    since = datetime.now(timezone.utc) - timedelta(days=JANELA_DIAS)
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            obras = buscar_top_obras(cur, since, n=3)
            insight = calcular_insight(cur, since)
            subs = buscar_subscribers(cur)
    finally:
        conn.close()

    print(f"  obras top: {len(obras)}")
    print(f"  insight: {insight['num_obras'] or 0} obras / {fmt_brl(insight['total_valor'])} / setor={insight['top_setor']} / uf={insight['top_uf']}")
    print(f"  subscribers ativos+confirmados: {len(subs)}")

    if not obras:
        print("Sem obras na janela. Abortando.")
        return 0
    if not subs:
        print("Sem subscribers. Nada a enviar.")
        return 0

    if args.limit:
        subs = subs[: args.limit]
        print(f"  limit aplicado: {len(subs)} subscribers")

    enviados, falhas = 0, 0
    subject = f"WiNS HUB · {len(obras)} obras top + insight da semana"
    for s in subs:
        html = render_html(s["nome"], obras, insight, s["token_confirmacao"])
        if not args.commit:
            print(f"  [DRY] would send -> {s['email']}")
            continue
        if enviar_email(s["email"], subject, html):
            enviados += 1
        else:
            falhas += 1

    print(f"=== FIM === enviados={enviados} falhas={falhas} subscribers={len(subs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
