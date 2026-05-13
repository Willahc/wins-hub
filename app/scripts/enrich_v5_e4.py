#!/usr/bin/env python3
"""V5 E4 — Verifier puro nos OURO/PRATA com email mas smtp_verified IS NULL.

Lacuna deixada por E1 (filtrou =false, ignorou NULL). Custo: ~47 verifications.
"""
from __future__ import annotations
import atexit, json, logging, os, sys, time
sys.path.insert(0, "/app")
import psycopg2, requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("e4")

DB = dict(host=os.getenv("DB_HOST","db"), port=int(os.getenv("DB_PORT","5432")),
          dbname=os.getenv("DB_NAME","wins_hub"), user=os.getenv("DB_USER","postgres"),
          password=os.getenv("DB_PASSWORD",""))
HUNTER_KEY = os.getenv("HUNTER_API_KEY","")
VERIFY_FLOOR = 1000

STATS = {"candidatos":0, "tentados":0, "validos":0, "invalidos":0, "webmail":0, "skip":0}

@atexit.register
def _emit():
    print(f"STATS_JSON: {json.dumps(STATS)}", flush=True)

def saldo(tipo="verifications"):
    try:
        r = requests.get(f"https://api.hunter.io/v2/account?api_key={HUNTER_KEY}", timeout=10)
        b = r.json().get("data",{}).get("requests",{}).get(tipo,{})
        return int(b.get("available",0)) - int(b.get("used",0))
    except Exception:
        return -1

def verifier(email):
    try:
        r = requests.get("https://api.hunter.io/v2/email-verifier",
                         params={"email":email,"api_key":HUNTER_KEY}, timeout=20)
        if r.status_code in (200,222):
            return r.json().get("data") or None
    except Exception as e:
        log.warning(f"  verifier erro {email}: {e}")
    return None

def main():
    s = saldo()
    log.info(f"E4 verifications saldo: {s}")
    if s < VERIFY_FLOOR:
        log.error(f"saldo<{VERIFY_FLOOR} — abort"); return 2
    conn = psycopg2.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, nivel1_email, classificacao_computed
            FROM obras
            WHERE classificacao_computed IN ('OURO','PRATA')
              AND nivel1_nome IS NOT NULL AND nivel1_nome != ''
              AND nivel1_email IS NOT NULL
              AND nivel1_email_smtp_verified IS NULL
              AND nivel1_email NOT LIKE '%%@gmail.%%'
              AND nivel1_email NOT LIKE '%%@hotmail.%%'
              AND nivel1_email NOT LIKE '%%@outlook.%%'
              AND nivel1_email NOT LIKE '%%@yahoo.%%'
              AND nivel1_email NOT LIKE '%%@uol.%%'
            ORDER BY CASE classificacao_computed WHEN 'OURO' THEN 1 ELSE 2 END,
                     valor_estimado DESC NULLS LAST
        """)
        rows = cur.fetchall()
    STATS["candidatos"] = len(rows)
    log.info(f"E4: {len(rows)} candidatos NULL")

    for i, (obra_id, email, classif) in enumerate(rows, 1):
        if i % 20 == 0:
            s2 = saldo()
            log.info(f"  [E4 {i}/{len(rows)}] verifications={s2} stats={STATS}")
            if s2 < VERIFY_FLOOR:
                log.warning("  abaixo floor — parando"); return 0
        STATS["tentados"] += 1
        v = verifier(email)
        if not v:
            STATS["skip"] += 1
            time.sleep(0.5); continue
        status = (v.get("status") or v.get("result") or "unknown").lower()
        score = int(v.get("score") or 0)
        is_valid = status in ("valid","accept_all") and score >= 60
        if status == "webmail":
            STATS["webmail"] += 1
        elif is_valid:
            STATS["validos"] += 1
        else:
            STATS["invalidos"] += 1
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE obras SET
                        nivel1_email_smtp_verified=%s,
                        nivel1_email_status=%s,
                        nivel1_email_score=%s,
                        nivel1_email_verified_at=NOW(),
                        nivel1_origem_enrichment=COALESCE(nivel1_origem_enrichment,'')||'+E4_verifier'
                    WHERE id=%s
                """, (is_valid, status, score, obra_id))
            conn.commit()
            if is_valid:
                log.info(f"  ✓ E4 {classif} id={obra_id} {email} score={score}")
        except Exception as e:
            log.warning(f"  update {obra_id}: {e}"); conn.rollback()
        time.sleep(0.5)
    conn.close()
    return 0

if __name__ == "__main__":
    try: rc = main()
    except BaseException as e:
        import traceback; log.error(f"UNCAUGHT: {e}"); log.error(traceback.format_exc()); rc = 1
    os._exit(rc or 0)
