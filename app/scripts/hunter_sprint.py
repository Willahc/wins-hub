"""Hunter email-finder pros decisores da sprint 28/05/2026 sem email."""
import os
import sys
import time

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras
import requests

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}
HUNTER_KEY = os.getenv("HUNTER_API_KEY", "").strip()
HUNTER_FIND_URL = "https://api.hunter.io/v2/email-finder"
HUNTER_VERIFY_URL = "https://api.hunter.io/v2/email-verifier"

# (obra_id, nome, domínio) — decisores sem email/LK pra tentar email-finder
ALVOS = [
    # Âmbar — promover PRATA→OURO
    ("f672d9c8-d25e-492d-aca2-74c3151d7dcb", "Eduardo", "Antonello", "ambarenergia.com.br"),
    # Lar — promover PRATA→OURO
    ("e887e5fe-56cc-4282-8931-e9040ca8ee21", "Irineo", "Rodrigues", "larcooperativa.com.br"),
    # Petrobras/Transpetro
    ("7b872e7d-2342-49f0-9c4f-53303c20a027", "Sérgio", "Bacci", "transpetro.com.br"),
    ("7b872e7d-2342-49f0-9c4f-53303c20a027", "Magda", "Chambriard", "petrobras.com.br"),
    # CMPC — Antonio Lacerda sem LK confirmado
    ("5c674385-9dd6-4bc3-b136-1ad2b1fe4922", "Antonio", "Lacerda", "cmpc.com.br"),
    # Ascenty — Fábio Matos sem LK
    ("29321cc1-4be2-4096-9189-dc77302a377b", "Fábio", "Matos", "ascenty.com"),
]


def hunter_find(first_name, last_name, domain):
    try:
        r = requests.get(HUNTER_FIND_URL,
                         params={"domain": domain, "first_name": first_name,
                                 "last_name": last_name, "api_key": HUNTER_KEY},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("data") or {}
        print(f"  Hunter HTTP {r.status_code}: {r.text[:120]}")
        return {}
    except Exception as e:
        print(f"  Hunter ERR: {e!r}")
        return {}


def hunter_verify(email):
    try:
        r = requests.get(HUNTER_VERIFY_URL,
                         params={"email": email, "api_key": HUNTER_KEY},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get("data") or {}
        return {}
    except Exception as e:
        print(f"  verify ERR: {e!r}")
        return {}


def main():
    if not HUNTER_KEY:
        print("HUNTER_API_KEY ausente"); sys.exit(1)

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Quota
    try:
        r = requests.get("https://api.hunter.io/v2/account",
                         params={"api_key": HUNTER_KEY}, timeout=10)
        if r.status_code == 200:
            d = r.json().get("data") or {}
            req = d.get("requests") or {}
            sea = req.get("searches") or {}
            print(f"Hunter quota: available={sea.get('available')} used={sea.get('used')}\n")
    except Exception:
        pass

    found = 0
    for obra_id, fn, ln, dom in ALVOS:
        print(f"\n[{fn} {ln} @{dom}]")
        d = hunter_find(fn, ln, dom)
        email = d.get("email")
        score = d.get("score")
        ver = d.get("verification") or {}
        print(f"  email={email} score={score} verification={ver.get('result')}")
        if not email or (score is not None and score < 50):
            print("  -> skip (sem email ou score baixo)")
            continue

        # SMTP verify pra confirmar
        v = hunter_verify(email)
        status = v.get("status")
        smtp_ok = v.get("status") == "valid"
        print(f"  verify status={status} smtp={v.get('smtp_check')} mx={v.get('mx_records')}")

        # UPDATE no decisor — busca pelo first_name no obra
        # Edge case: nome no DB pode ter mais tokens. Match por ILIKE first+last.
        cur.execute("""
            UPDATE decisores_obra
               SET email=%s,
                   observacoes = COALESCE(observacoes,'') || E'\nHunter score=' || %s::text || ' status=' || COALESCE(%s,'?')
             WHERE obra_id=%s
               AND excluido_em IS NULL
               AND nome ILIKE %s
               AND nome ILIKE %s
               AND (email IS NULL OR email='')
            RETURNING id, nome
        """, (email, score or 0, status, obra_id, f"%{fn}%", f"%{ln}%"))
        row = cur.fetchone()
        if row:
            print(f"  ✓ UPDATE decisor {row['nome']}: email={email}")
            found += 1
            # Atualizar nivel1_* na obra se ainda vazio
            cur.execute("""
                UPDATE obras
                   SET nivel1_nome=%s, nivel1_email=%s,
                       nivel1_email_score=%s, nivel1_email_status=%s,
                       nivel1_email_smtp_verified=%s,
                       nivel1_email_verified_at=NOW(),
                       nivel1_origem_enrichment='hunter_email_finder_sprint_20260528',
                       nivel1_enrichment_data=NOW()
                 WHERE id=%s
                   AND (nivel1_email IS NULL OR nivel1_email='')
            """, (f"{fn} {ln}", email, score or 0, status or 'unknown', smtp_ok, obra_id))
        else:
            print("  ! decisor não encontrado pra UPDATE (já tem email?)")
        time.sleep(0.5)

    conn.commit()
    print(f"\n== {found} emails encontrados e atualizados ==")


if __name__ == "__main__":
    main()
