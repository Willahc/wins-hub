"""
alerta_semanal_cnae.py
Envia email semanal de obras novas por CNAE pra cada prestador.
Roda toda segunda 08:00 via cron.
DRY-RUN por padrao. Use --commit pra enviar de verdade.

Schema correto:
- matches_obra_prestador tem cnpj (nao prestador_id) -> JOIN com prestador_empresas
- score column = "score" (nao "score_match")
- prestadores.nome_empresa (nao nome_contato)
"""
import argparse, json, os, sys
from datetime import datetime, timezone, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
import urllib.request

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "db"),
    "dbname": os.environ.get("DB_NAME", "wins_hub"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM    = os.environ.get("RESEND_FROM", "WiNS HUB <contato@winshubcomercial.com.br>")
SITE_URL       = os.environ.get("SITE_URL", "https://winshubcomercial.com.br")
JANELA_DIAS    = 7
SCORE_MIN_DEFAULT = 50

FASES_RELEVANTES = (
    'PLANEJAMENTO', 'EM_EXECUCAO',
    'LICENCA_INSTALACAO', 'LICENCA_PREVIA',
    'PROJETO', 'LICITACAO_ABERTA',
)


def buscar_obras_novas(cur, prestador_id, since, score_min):
    cur.execute("""
        SELECT DISTINCT ON (mo.obra_id)
               mo.obra_id, o.nome, mo.score, o.fase, o.uf,
               o.valor_estimado, o.setor
        FROM matches_obra_prestador mo
        JOIN prestador_empresas pe
            ON pe.cnpj = mo.cnpj
           AND pe.prestador_id = %s
           AND pe.ativo = true
        JOIN obras o ON o.id = mo.obra_id
        LEFT JOIN alertas_enviados ae
            ON ae.prestador_id = pe.prestador_id
           AND mo.obra_id = ANY(ae.obras_ids)
           AND ae.canal = 'email'
        WHERE mo.gerado_em > %s
          AND mo.score >= %s
          AND o.fase = ANY(%s)
          AND (o.cnpj_status IS NULL OR o.cnpj_status = 'ok')
          AND ae.id IS NULL
        ORDER BY mo.obra_id, mo.score DESC
        LIMIT 10
    """, (prestador_id, since, score_min, list(FASES_RELEVANTES)))
    rows = cur.fetchall()
    rows.sort(key=lambda r: r['score'] or 0, reverse=True)
    return rows


def formatar_valor(v):
    if not v:
        return "Valor nao divulgado"
    v = float(v)
    if v >= 1e9: return f"R$ {v/1e9:.1f} bi"
    if v >= 1e6: return f"R$ {v/1e6:.0f} mi"
    return f"R$ {v:,.0f}"


def montar_email(nome_empresa, obras):
    items_html = ""
    for o in obras:
        score = int(o['score'] or 0)
        score_color = "#27AE60" if score >= 75 else "#F39C12"
        nome_obra = (o['nome'] or 'Obra sem nome')[:60]
        items_html += f"""
        <div style="border:1px solid #1E3A52;border-radius:8px;padding:14px;margin-bottom:10px;background:#0D1B2A;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                <strong style="color:#FFFFFF;font-size:14px;">{nome_obra}</strong>
                <span style="background:{score_color}20;color:{score_color};padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;white-space:nowrap;margin-left:8px;">{score}/100</span>
            </div>
            <div style="margin-top:6px;font-size:12px;color:#8DA0B3;">
                {o['uf'] or '?'} &middot; {o['fase']} &middot; {formatar_valor(o['valor_estimado'])} &middot; Setor: {o['setor'] or '?'}
            </div>
            <a href="{SITE_URL}/projetos" style="display:inline-block;margin-top:8px;background:#F5A623;color:#0D1B2A;padding:4px 12px;border-radius:16px;font-size:11px;font-weight:700;text-decoration:none;">Ver obra &rarr;</a>
        </div>"""

    primeiro_nome = (nome_empresa or "").split()[0] if nome_empresa else "prestador"

    return f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#0A1628;padding:24px;border-radius:12px;">
    <div style="text-align:center;margin-bottom:20px;">
        <h1 style="color:#F5A623;font-size:22px;margin:0;">WiNS Hub</h1>
        <p style="color:#8DA0B3;font-size:12px;margin:4px 0 0;">Inteligencia Comercial para Megaobras</p>
    </div>
    <h2 style="color:#FFFFFF;font-size:16px;margin:0 0 6px;">
        Suas obras da semana, {primeiro_nome}
    </h2>
    <p style="color:#8DA0B3;font-size:13px;margin:0 0 16px;">
        Encontramos <strong style="color:#F5A623;">{len(obras)} obras compativeis</strong> com o perfil da sua empresa esta semana.
    </p>
    {items_html}
    <div style="margin-top:20px;padding:14px;background:#1A2E42;border-radius:8px;text-align:center;">
        <a href="{SITE_URL}/projetos" style="background:#F5A623;color:#0D1B2A;padding:10px 24px;border-radius:24px;font-weight:700;text-decoration:none;font-size:14px;">
            Ver todas as obras compativeis &rarr;
        </a>
    </div>
    <p style="color:#374151;font-size:10px;text-align:center;margin-top:16px;">
        WiNS Hub &middot; winshubcomercial.com.br &middot; Voce recebe porque e assinante.
    </p>
</div>"""


def enviar_email(to_email, to_name, obras, commit=False):
    html = montar_email(to_name, obras)
    payload = {
        "from": RESEND_FROM,
        "to": [to_email],
        "subject": f"{len(obras)} obras compativeis esta semana - WiNS Hub",
        "html": html,
    }
    if not commit:
        print(f"  [DRY-RUN] {to_email}: {len(obras)} obras (subject={payload['subject']!r})")
        return True

    if not RESEND_API_KEY:
        print(f"  ERRO: RESEND_API_KEY nao configurada no ambiente")
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
        print(f"  ERRO Resend: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="Realmente envia. Default: dry-run")
    parser.add_argument("--limit", type=int, default=50, help="Max prestadores nesta rodada")
    parser.add_argument("--prestador", help="UUID de um prestador especifico (debug)")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    since = datetime.now(timezone.utc) - timedelta(days=JANELA_DIAS)

    if args.prestador:
        cur.execute("""
            SELECT p.id, p.nome_empresa, p.email,
                   COALESCE(ap.score_minimo, %s) AS score_minimo
            FROM prestadores p
            LEFT JOIN alertas_preferencias ap ON ap.prestador_id = p.id
            WHERE p.id = %s
        """, (SCORE_MIN_DEFAULT, args.prestador))
    else:
        cur.execute("""
            SELECT p.id, p.nome_empresa, p.email,
                   COALESCE(ap.score_minimo, %s) AS score_minimo
            FROM prestadores p
            LEFT JOIN alertas_preferencias ap ON ap.prestador_id = p.id
            WHERE p.email IS NOT NULL AND p.email <> ''
              AND p.ativo = true
              AND COALESCE(ap.ativo, true) = true
              AND COALESCE(ap.frequencia, 'semanal') <> 'nunca'
            ORDER BY p.criado_em ASC
            LIMIT %s
        """, (SCORE_MIN_DEFAULT, args.limit))

    prestadores = cur.fetchall()

    print(f"\n{'='*55}")
    print(f"MODO:        {'COMMIT' if args.commit else 'DRY-RUN'}")
    print(f"Prestadores: {len(prestadores)}")
    print(f"Janela:      ultimos {JANELA_DIAS} dias")
    print(f"{'='*55}\n")

    enviados = skips = erros = 0
    for p in prestadores:
        obras = buscar_obras_novas(cur, p['id'], since, p['score_minimo'])
        if not obras:
            print(f"  -- {(p['nome_empresa'] or '?')[:30]:30s} | {p['email']:30s} | sem matches novos")
            skips += 1
            continue

        print(f"  -> {(p['nome_empresa'] or '?')[:30]:30s} | {p['email']:30s} | {len(obras)} obras")
        ok = enviar_email(p['email'], p['nome_empresa'], obras, commit=args.commit)

        if ok and args.commit:
            cur.execute("""
                INSERT INTO alertas_enviados (prestador_id, canal, obras_ids, obras_count)
                VALUES (%s, 'email', %s, %s)
            """, (p['id'], [o['obra_id'] for o in obras], len(obras)))
            conn.commit()
            enviados += 1
        elif ok:
            enviados += 1
        else:
            erros += 1

    print(f"\n{'='*55}")
    print(f"Enviados: {enviados} | Sem matches: {skips} | Erros: {erros}")
    print(f"Modo: {'COMMIT' if args.commit else 'DRY-RUN -- nada enviado'}")
    print(f"{'='*55}\n")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
