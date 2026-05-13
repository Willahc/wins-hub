#!/usr/bin/env python3
"""V6 T2v2 — Finder com nome composto extraido do email atual.

Caso T2v1 falhou pq decisores top-30 maioria só tem primeiro nome → Hunter retornou null.
Aqui: extrair nome+sobrenome do local-part do email atual (julio.pimenta → Julio Pimenta).
Rodar Finder com dominio v6 (canonico). Substituir se valid + score >= 80.
"""
from __future__ import annotations
import atexit, json, logging, os, re, sys, time
sys.path.insert(0, "/app")
import psycopg2, requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("v6_v2")

DB = dict(host=os.getenv("DB_HOST","db"), port=int(os.getenv("DB_PORT","5432")),
          dbname=os.getenv("DB_NAME","wins_hub"), user=os.getenv("DB_USER","postgres"),
          password=os.getenv("DB_PASSWORD",""))
HUNTER = os.getenv("HUNTER_API_KEY","")

STATS = {"candidatos":0, "tentou_finder":0, "sem_full_name":0,
         "email_null":0, "score_baixo":0,
         "iguais_revalidados":0, "swap":0,
         "hunter_search":0, "hunter_verify":0}

@atexit.register
def _emit(): print(f"STATS_JSON: {json.dumps(STATS)}", flush=True)

def saldo(t="searches"):
    try:
        r = requests.get(f"https://api.hunter.io/v2/account?api_key={HUNTER}", timeout=10)
        b = r.json().get("data",{}).get("requests",{}).get(t,{})
        return int(b.get("available",0)) - int(b.get("used",0))
    except: return -1

def finder(domain, name):
    try:
        r = requests.get("https://api.hunter.io/v2/email-finder",
                         params={"domain":domain, "full_name":name, "api_key":HUNTER},
                         timeout=20)
        if r.status_code == 200: return r.json().get("data") or None
    except Exception as e:
        log.warning(f"  finder erro: {e}")
    return None

def verifier(email):
    try:
        r = requests.get("https://api.hunter.io/v2/email-verifier",
                         params={"email":email, "api_key":HUNTER}, timeout=20)
        if r.status_code in (200,222): return r.json().get("data") or None
    except: pass
    return None

def extrair_full_name_de_email(email: str) -> str | None:
    """julio.pimenta@dominio → 'Julio Pimenta'. Retorna None se só primeiro nome."""
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0]
    # remove sufixos numericos
    local = re.sub(r"\d+$", "", local)
    # split por . ou _
    parts = re.split(r"[._]+", local)
    parts = [p for p in parts if p and len(p) >= 2 and p.lower() not in ("tintas","contato","compras","central")]
    if len(parts) < 2:
        return None
    # primeiras 2 ou 3 partes -> capitalizar
    return " ".join(p.capitalize() for p in parts[:3])

def main():
    conn = psycopg2.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT o.id, o.cnpj, o.empresa, o.nivel1_nome, o.nivel1_cargo,
                   o.nivel1_email, o.nivel1_email_smtp_verified,
                   ed.dominio AS dominio_v6,
                   o.classificacao_computed
            FROM obras o
            JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
            WHERE ed.validacao_metodo = 'manual_chat_v6'
              AND o.nivel1_nome IS NOT NULL AND LENGTH(o.nivel1_nome) > 2
              AND (LOWER(o.nivel1_cargo) LIKE '%%gerente%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%coordenador%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%compra%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%suprimento%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%engenh%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%manuten%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%industrial%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%projeto%%')
              AND NOT LOWER(o.nivel1_cargo) LIKE '%%diretor-presidente%%'
              AND NOT LOWER(o.nivel1_cargo) LIKE '%%vice-presidente%%'
              AND NOT LOWER(o.nivel1_cargo) LIKE '%% ceo%%'
              AND o.nivel1_email IS NOT NULL
              AND (
                o.nivel1_email_smtp_verified IS NOT TRUE OR
                LOWER(SPLIT_PART(o.nivel1_email,'@',2)) != LOWER(ed.dominio)
              )
            ORDER BY o.valor_estimado DESC NULLS LAST
        """)
        rows = cur.fetchall()
    STATS["candidatos"] = len(rows)
    log.info(f"V6 T2v2: {len(rows)} candidatos para Finder com full_name extraido")

    seen = set()  # (cnpj, full_name_extraido)
    cache = {}    # (cnpj, full_name) → (email_new, score, valid, status)

    for i, (obra_id, cnpj, empresa, nome_db, cargo, email_old,
            verified, dominio_v6, classif) in enumerate(rows, 1):
        full = extrair_full_name_de_email(email_old)
        if not full:
            STATS["sem_full_name"] += 1
            log.info(f"  [{i}/{len(rows)}] SEM full_name: {nome_db} email={email_old}")
            continue
        key = (cnpj, full.lower(), dominio_v6)
        log.info(f"  [{i}/{len(rows)}] {classif} cnpj={cnpj} '{full}' @{dominio_v6} (atual: {email_old})")
        if key not in seen:
            seen.add(key)
            STATS["hunter_search"] += 1
            STATS["tentou_finder"] += 1
            d = finder(dominio_v6, full)
            if not d or not d.get("email"):
                STATS["email_null"] += 1
                cache[key] = None
                time.sleep(0.6); continue
            em_new = d["email"].lower().strip()
            sc = int(d.get("score") or 0)
            if sc < 50:
                STATS["score_baixo"] += 1
                cache[key] = None
                log.info(f"    score baixo: {em_new} sc={sc}")
                time.sleep(0.6); continue
            STATS["hunter_verify"] += 1
            v = verifier(em_new)
            v_status = "unknown"; v_score = sc
            if v:
                v_status = (v.get("status") or v.get("result") or "unknown").lower()
                v_score = int(v.get("score") or sc)
            valido = v_status in ("valid","accept_all") and v_score >= 60
            cache[key] = (em_new, v_score, valido, v_status)
            log.info(f"    Finder→ {em_new} sc={v_score} status={v_status} valido={valido}")
            time.sleep(0.6)

        r = cache.get(key)
        if not r: continue
        em_new, sc, valido, status = r
        em_old = (email_old or "").lower().strip()

        if not valido:
            continue

        if em_new == em_old:
            # mesmo email → só revalida
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE obras SET
                            nivel1_email_smtp_verified=true,
                            nivel1_email_status=%s, nivel1_email_score=%s,
                            nivel1_email_verified_at=NOW(),
                            nivel1_origem_enrichment=COALESCE(nivel1_origem_enrichment,'')||'+V6_revalidado'
                        WHERE id=%s
                    """, (status, sc, obra_id))
                conn.commit()
                STATS["iguais_revalidados"] += 1
            except Exception as e:
                log.warning(f"  revalidar erro: {e}"); conn.rollback()
        elif sc >= 80:
            # swap (só se score alto)
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE obras SET
                            nivel1_email=%s, nivel1_email_score=%s,
                            nivel1_email_smtp_verified=true,
                            nivel1_email_status=%s,
                            nivel1_email_verified_at=NOW(),
                            nivel1_origem_enrichment=COALESCE(nivel1_origem_enrichment,'')||'+V6_manual_dominio',
                            nivel1_enrichment_data=NOW()
                        WHERE id=%s
                    """, (em_new, sc, status, obra_id))
                conn.commit()
                STATS["swap"] += 1
                log.info(f"  ✓ SWAP {classif} {empresa[:30]}: {em_old} → {em_new}")
            except Exception as e:
                log.warning(f"  swap erro: {e}"); conn.rollback()
    conn.close()
    return 0

if __name__ == "__main__":
    try: rc = main()
    except BaseException as e:
        import traceback; log.error(f"UNCAUGHT: {e}"); log.error(traceback.format_exc()); rc = 1
    os._exit(rc or 0)
