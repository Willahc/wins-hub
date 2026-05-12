"""
alerta_realtime_obras.py
Roda a cada 30min. Detecta obras novas, cruza com matches CNAE,
envia 1 email por prestador com ate 10 obras novas compativeis.

Salvaguardas:
- Skip se >200 obras na janela (backfill detectado)
- Max 4 emails por prestador por dia (anti-fadiga)
- So prestadores com alertas_preferencias.ativo=true
- So obras com score>=50 nas 6 fases prospectaveis

DRY-RUN por padrao. Use --commit pra enviar de verdade.
"""
import argparse, json, os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import urllib.request
import psycopg2
from psycopg2.extras import RealDictCursor

try:
    from services.matchmaking import DB_CONFIG
except ImportError:
    DB_CONFIG = {
        "host": os.environ.get("DB_HOST", "db"),
        "dbname": os.environ.get("DB_NAME", "wins_hub"),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
    }

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM    = os.environ.get("RESEND_FROM", "WiNS HUB <contato@winshubcomercial.com.br>")
SITE_URL       = os.environ.get("SITE_URL", "https://winshubcomercial.com.br")

JANELA_MIN      = 30
BACKFILL_CAP    = 200
MAX_OBRAS_EMAIL = 10
MAX_EMAILS_DIA  = 4
SCORE_MIN       = 50
FASES_OK = ('PLANEJAMENTO', 'EM_EXECUCAO', 'LICENCA_INSTALACAO',
            'LICENCA_PREVIA', 'PROJETO', 'LICITACAO_ABERTA')


def formatar_valor(v):
    if not v:
        return "Valor nao divulgado"
    v = float(v)
    if v >= 1e9: return f"R$ {v/1e9:.1f} bi"
    if v >= 1e6: return f"R$ {v/1e6:.0f} mi"
    return f"R$ {v:,.0f}"


def score_color(s):
    if s >= 75: return "#27AE60"
    if s >= 60: return "#F5A623"
    return "#8DA0B3"


def montar_email(nome, obras):
    items = ""
    for o in obras:
        sc = score_color(int(o['score'] or 0))
        nome_obra = str(o['nome'] or 'Obra sem nome')[:55]
        items += f"""
        <div style="border:1px solid #1E3A52;border-radius:8px;padding:12px;margin-bottom:8px;background:#0D1B2A;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <strong style="color:#fff;font-size:13px;">{nome_obra}</strong>
            <span style="background:{sc}20;color:{sc};padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;white-space:nowrap;margin-left:8px;">{int(o['score'] or 0)}/100</span>
          </div>
          <div style="margin-top:4px;font-size:11px;color:#8DA0B3;">
            {o.get('uf') or '?'} &middot; {o.get('fase') or ''} &middot; {formatar_valor(o.get('valor_estimado'))}
          </div>
          <a href="{SITE_URL}/projetos?apenas_meus_matches=1" style="display:inline-block;margin-top:8px;background:#F5A623;color:#0D1B2A;padding:3px 10px;border-radius:14px;font-size:11px;font-weight:700;text-decoration:none;">Ver obra &rarr;</a>
        </div>"""

    n = len(obras)
    plural = "s" if n > 1 else ""
    plural_is = "is" if n > 1 else ""
    primeiro_nome = (nome or "").split()[0] if nome else "prestador"

    return f"""<div style="font-family:Arial,sans-serif;max-width:580px;margin:0 auto;background:#0A1628;padding:20px;border-radius:12px;">
  <div style="text-align:center;margin-bottom:16px;">
    <h1 style="color:#F5A623;font-size:20px;margin:0;">WiNS Hub</h1>
    <p style="color:#8DA0B3;font-size:11px;margin:2px 0 0;">Inteligencia Comercial para Megaobras</p>
  </div>
  <h2 style="color:#fff;font-size:15px;margin:0 0 4px;">
    {n} nova{plural} obra{plural} compativ{'eis' if n>1 else 'el'}, {primeiro_nome}!
  </h2>
  <p style="color:#8DA0B3;font-size:12px;margin:0 0 14px;">
    Obras que combinam com o CNAE da sua empresa acabaram de entrar no radar.
  </p>
  {items}
  <div style="text-align:center;margin-top:16px;">
    <a href="{SITE_URL}/projetos?apenas_meus_matches=1" style="background:#F5A623;color:#0D1B2A;padding:10px 24px;border-radius:24px;font-weight:700;text-decoration:none;font-size:13px;">
      Ver todas as obras compativeis &rarr;
    </a>
  </div>
  <p style="color:#374151;font-size:10px;text-align:center;margin-top:14px;">
    WiNS Hub &middot; winshubcomercial.com.br<br>
    Voce recebe porque e assinante com alertas ativos.
  </p>
</div>"""


def enviar_resend(to_email, to_name, obras, commit=False):
    n = len(obras)
    plural = "s" if n > 1 else ""
    plural_eis = "is" if n > 1 else "el"
    subj = f"{n} nova{plural} obra{plural} compativ{plural_eis} - WiNS Hub"
    payload = {
        "from": RESEND_FROM,
        "to": [to_email],
        "subject": subj,
        "html": montar_email(to_name, obras),
    }

    if not commit:
        print(f"    [DRY-RUN] -> {to_email}: {n} obras (subject={subj!r})")
        return True

    if not RESEND_API_KEY:
        print(f"    ERRO: RESEND_API_KEY nao configurada")
        return False

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"    ERRO Resend: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    cur  = conn.cursor(cursor_factory=RealDictCursor)

    since = datetime.now(timezone.utc) - timedelta(minutes=JANELA_MIN)

    cur.execute("""
        SELECT COUNT(*) AS n FROM obras
        WHERE criado_em > %s AND notificado_em IS NULL
    """, (since,))
    n_novas = cur.fetchone()["n"]

    print(f"\n{'='*55}")
    print(f"MODO:        {'COMMIT' if args.commit else 'DRY-RUN'}")
    print(f"Janela:      ultimos {JANELA_MIN} min")
    print(f"Obras novas: {n_novas}")

    if n_novas > BACKFILL_CAP:
        print(f"BACKFILL DETECTADO ({n_novas} > {BACKFILL_CAP}) -- SKIP ciclo")
        print(f"{'='*55}\n")
        cur.close(); conn.close(); return

    if n_novas == 0:
        print("Sem obras novas. Nada a fazer.")
        print(f"{'='*55}\n")
        cur.close(); conn.close(); return

    cur.execute("""
        SELECT
            p.id AS prestador_id,
            p.nome_empresa,
            p.email,
            o.id AS obra_id,
            o.nome,
            o.uf,
            o.fase,
            o.valor_estimado,
            o.setor,
            mo.score
        FROM obras o
        JOIN matches_obra_prestador mo ON mo.obra_id = o.id
        JOIN prestador_empresas pe ON pe.cnpj = mo.cnpj AND pe.ativo = true
        JOIN prestadores p ON p.id = pe.prestador_id
        LEFT JOIN alertas_preferencias ap ON ap.prestador_id = p.id
        WHERE o.criado_em > %s
          AND o.notificado_em IS NULL
          AND (o.cnpj_status IS NULL OR o.cnpj_status = 'ok')
          AND (o.visivel IS NULL OR o.visivel = true)
          AND o.fase IN %s
          AND mo.score >= %s
          AND p.email IS NOT NULL AND p.email <> ''
          AND p.ativo = true
          AND COALESCE(ap.ativo, true) = true
          AND COALESCE(ap.frequencia, 'semanal') <> 'nunca'
        ORDER BY p.id, mo.score DESC
    """, (since, FASES_OK, SCORE_MIN))
    rows = cur.fetchall()

    por_prestador = defaultdict(list)
    seen = defaultdict(set)
    for r in rows:
        if r['obra_id'] in seen[r['prestador_id']]:
            continue
        seen[r['prestador_id']].add(r['obra_id'])
        por_prestador[r['prestador_id']].append(r)

    print(f"Prestadores c/ match: {len(por_prestador)}")
    print(f"{'='*55}\n")

    obras_notificadas = set()
    enviados = skips_fadiga = erros = 0

    for pid, obras in por_prestador.items():
        nome  = obras[0]['nome_empresa'] or "Fornecedor"
        email = obras[0]['email']

        cur.execute("""
            SELECT COUNT(*) AS n FROM alertas_enviados
            WHERE prestador_id = %s
              AND canal = 'realtime'
              AND enviado_em > now() - interval '24 hours'
        """, (pid,))
        emails_hoje = cur.fetchone()["n"]

        if emails_hoje >= MAX_EMAILS_DIA:
            print(f"  SKIP fadiga: {nome[:30]} ({emails_hoje} emails ja enviados hoje)")
            skips_fadiga += 1
            continue

        top = obras[:MAX_OBRAS_EMAIL]
        obras_ids = [str(o['obra_id']) for o in top]

        print(f"  -> {nome[:30]:30s} | {email:30s} | {len(top)} obras")
        for o in top:
            print(f"       {(o['nome'] or '?')[:45]:45s} | {int(o['score'] or 0)}/100 | {o['fase']}")

        ok = enviar_resend(email, nome, top, commit=args.commit)

        if ok and args.commit:
            cur.execute("""
                INSERT INTO alertas_enviados (prestador_id, canal, obras_ids, obras_count)
                VALUES (%s, 'realtime', %s, %s)
            """, (pid, obras_ids, len(top)))
            conn.commit()
            enviados += 1
            for oid in obras_ids:
                obras_notificadas.add(oid)
        elif ok:
            enviados += 1
        else:
            erros += 1

    if args.commit and obras_notificadas:
        cur.execute(
            "UPDATE obras SET notificado_em = now() WHERE id = ANY(%s::uuid[])",
            (list(obras_notificadas),)
        )
        conn.commit()
        print(f"\n{len(obras_notificadas)} obras marcadas notificado_em=now()")

    print(f"\n{'='*55}")
    print(f"Enviados: {enviados} | Skip fadiga: {skips_fadiga} | Erros: {erros}")
    print(f"Modo: {'COMMIT' if args.commit else 'DRY-RUN -- nada enviado'}")
    print(f"{'='*55}\n")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
