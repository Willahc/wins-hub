"""
Hunter batch — wave 02/06/2026 (Google Alerts + Soprano + Nestlé).

Domínios alvo (3): nestle.com.br, soprano.com.br, neusoft.com.br
  (Petrobras: padrão nome.sobrenome@petrobras.com.br já conhecido — sem quota)

Obras alvo: fonte='manual_google_alerts_20260602' + UFN-III (87bc0a32-...).

USO:
    # dry-run (mostra saldo + plano sem queimar quota):
    docker exec wins_hub_v2-api-1 python /app/scripts/hunter_wave_google_alerts_20260602.py

    # commit (gasta quota — só rodar pós reset 11/06):
    docker exec wins_hub_v2-api-1 python /app/scripts/hunter_wave_google_alerts_20260602.py --commit

Esperado: 3 domain-searches (~3 quota) + emails inseridos por obra alvo.
Saldo atual: 59/2000 (per user 02/06). Reset 11/06/2026 15:20 UTC.
"""
import os, sys, time
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

HUNTER_KEY = os.getenv("HUNTER_API_KEY", "").strip()
COMMIT = "--commit" in sys.argv

DOMAINS = [
    ("nestle.com.br",  "nestle_wave_20260602"),
    ("soprano.com.br", "soprano_wave_20260602"),
    ("neusoft.com.br", "neusoft_wave_20260602"),
]

PETROBRAS_TARGETS = [
    ("Magda Chambriard", "CEO Petrobras", "87bc0a32-2d0f-4e40-ba19-a864328142d9"),
]


def check_quota():
    r = requests.get("https://api.hunter.io/v2/account",
                     params={"api_key": HUNTER_KEY}, timeout=15)
    d = (r.json() or {}).get("data") or {}
    req = d.get("requests") or {}
    s = req.get("searches") or {}
    used, avail = s.get("used", 0), s.get("available", 2000)
    reset = d.get("reset_date") or "?"
    print(f"[QUOTA] usado={used} / disponivel={avail} | restante={avail-used} | reset={reset}")
    return avail - used


def domain_search(dominio):
    r = requests.get("https://api.hunter.io/v2/domain-search",
                     params={"domain": dominio, "api_key": HUNTER_KEY,
                             "limit": 30,
                             "seniority": "executive,senior",
                             "department": "executive,management,operations,it,engineering"},
                     timeout=20)
    return (r.json() or {}).get("data") or {}


def conn_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "wins_hub-db-1"),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "wins_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def obras_alvo(cur):
    cur.execute("""
        SELECT id, nome, cnpj, empresa, municipio
        FROM obras
        WHERE fonte = 'manual_google_alerts_20260602'
           OR id = '87bc0a32-2d0f-4e40-ba19-a864328142d9'
        ORDER BY valor_estimado DESC NULLS LAST;
    """)
    return cur.fetchall()


def _norm(s):
    import unicodedata
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def map_dominio_to_obras(obras, dominio):
    raiz = dominio.split(".")[0].lower()
    uniq, seen = [], set()
    for o in obras:
        emp = _norm(o["empresa"])
        nm = _norm(o["nome"])
        if (raiz in emp or raiz in nm) and o["id"] not in seen:
            uniq.append(o); seen.add(o["id"])
    return uniq


def main():
    if not HUNTER_KEY:
        print("ERROR: HUNTER_API_KEY ausente.")
        sys.exit(1)

    print(f"=== Hunter Wave Google Alerts 02/06 — COMMIT={COMMIT} ===\n")
    saldo = check_quota()
    if COMMIT and saldo < 10:
        print(f"ABORT: saldo {saldo} < 10 (margem). Aguardar reset 11/06.")
        sys.exit(1)

    with conn_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            obras = obras_alvo(cur)
            print(f"\n[OBRAS] {len(obras)} alvo:")
            for o in obras:
                print(f"  - {o['nome'][:60]:60} | {o['municipio']:20} | empresa={o['empresa']}")

            for dominio, marker in DOMAINS:
                alvo = map_dominio_to_obras(obras, dominio)
                print(f"\n>>> {dominio} (alvo: {len(alvo)} obras) <<<")
                for o in alvo:
                    print(f"    ↳ {o['nome'][:55]}")
                if not COMMIT:
                    print("    (dry-run)")
                    continue

                data = domain_search(dominio)
                emails = data.get("emails") or []
                pattern = data.get("pattern") or "?"
                print(f"    pattern={pattern} | emails_retornados={len(emails)}")
                for e in emails[:15]:
                    fn = (e.get("first_name") or "").strip()
                    ln = (e.get("last_name") or "").strip()
                    pos = e.get("position") or ""
                    em  = e.get("value") or ""
                    conf = e.get("confidence") or 0
                    nome = f"{fn} {ln}".strip()
                    if not nome or not em:
                        continue
                    print(f"      {nome} | {pos[:40]} | {em} ({conf})")
                    for o in alvo:
                        cur.execute("""
                            INSERT INTO decisores_obra
                              (obra_id, nome, cargo, email, fonte, registrado_por, observacoes)
                            VALUES
                              (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO NOTHING;
                        """, (o["id"], nome, pos[:200], em,
                              "hunter_domain_search", marker,
                              f"Hunter domain-search {dominio} conf={conf}"))
                time.sleep(1.2)

            if COMMIT:
                conn.commit()
                print("\n[OK] commit feito.")
            else:
                print("\n[DRY-RUN] use --commit pra disparar (apos reset 11/06).")

            print("\n[PETROBRAS] padrao nome.sobrenome@petrobras.com.br (sem quota):")
            for nm, cargo, obra_id in PETROBRAS_TARGETS:
                parts = nm.lower().replace("ç", "c").replace("ã", "a").replace("é", "e").split()
                em = ".".join(parts) + "@petrobras.com.br"
                print(f"  - {nm} | {cargo} | {em} | obra_id={obra_id}")
            print("  (insert manual pos confirmar Magda Chambriard ainda CEO em 06/2026)")

            saldo2 = check_quota()
            print(f"\n[FINAL] quota gasta nesta run: {saldo - saldo2} | saldo: {saldo2}")


if __name__ == "__main__":
    main()
