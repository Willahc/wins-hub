#!/usr/bin/env python3
"""V6 T2 — Hunter Email Finder seletivo nos 30 dominios manual_chat_v6.

Critério briefing original (`nivel1_email IS NULL`) retorna 0 candidatos pois
todos 30 CNPJs já têm email. Expansão pragmática:
- (a) smtp_verified IS NOT TRUE → vale re-Finder com dominio validado
- (b) dominio_email ≠ dominio_v6 → tentar email canonico no dominio oficial

Mantém filtro de cargo gerencial/operacional. Marca origem '+V6_manual_dominio'.
Só substitui o email atual se Finder retornar diferente E (score>=80) E Verifier=valid.
"""
from __future__ import annotations
import atexit, json, logging, os, sys, time
sys.path.insert(0, "/app")
import psycopg2, requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("v6_finder")

DB = dict(host=os.getenv("DB_HOST","db"), port=int(os.getenv("DB_PORT","5432")),
          dbname=os.getenv("DB_NAME","wins_hub"), user=os.getenv("DB_USER","postgres"),
          password=os.getenv("DB_PASSWORD",""))
HUNTER = os.getenv("HUNTER_API_KEY","")

STATS = {"candidatos":0, "tentados":0, "iguais":0, "substituidos":0,
         "novos":0, "score_baixo":0, "verify_falhou":0,
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
        log.warning(f"  finder erro {domain}/{name}: {e}")
    return None

def verifier(email):
    try:
        r = requests.get("https://api.hunter.io/v2/email-verifier",
                         params={"email":email, "api_key":HUNTER}, timeout=20)
        if r.status_code in (200,222): return r.json().get("data") or None
    except Exception as e:
        log.warning(f"  verifier erro {email}: {e}")
    return None

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
              AND o.nivel1_nome IS NOT NULL AND LENGTH(o.nivel1_nome) > 5
              AND (LOWER(o.nivel1_cargo) LIKE '%%gerente%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%coordenador%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%compra%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%suprimento%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%engenh%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%manuten%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%industrial%%' OR
                   LOWER(o.nivel1_cargo) LIKE '%%planejamento%%')
              AND NOT LOWER(o.nivel1_cargo) LIKE '%%diretor-presidente%%'
              AND NOT LOWER(o.nivel1_cargo) LIKE '%%vice-presidente%%'
              AND NOT LOWER(o.nivel1_cargo) LIKE '%% ceo%%'
              AND (
                o.nivel1_email IS NULL OR
                o.nivel1_email_smtp_verified IS NOT TRUE OR
                LOWER(SPLIT_PART(o.nivel1_email,'@',2)) != LOWER(ed.dominio)
              )
            ORDER BY o.valor_estimado DESC NULLS LAST
        """)
        rows = cur.fetchall()
    STATS["candidatos"] = len(rows)
    log.info(f"V6 T2: {len(rows)} candidatos (smtp false/null OR dominio mismatch)")

    seen_pair = set()  # dedup (cnpj, nome) → 1 Finder call
    cache_email = {}   # (cnpj, nome) → (email_novo, score, valido, status)

    for i, (obra_id, cnpj, empresa, nome, cargo, email_old, verified,
            dominio_v6, classif) in enumerate(rows, 1):
        if i % 10 == 0:
            s = saldo("searches")
            log.info(f"  [{i}/{len(rows)}] searches={s} stats={STATS}")
            if s < 500:
                log.warning("searches < 500 — parando"); return 0
        STATS["tentados"] += 1
        key = (cnpj, nome.lower())
        if key not in seen_pair:
            seen_pair.add(key)
            STATS["hunter_search"] += 1
            d = finder(dominio_v6, nome)
            if not d or not d.get("email"):
                cache_email[key] = None
                time.sleep(0.6); continue
            em_new = d["email"].lower().strip()
            sc_new = int(d.get("score") or 0)
            if sc_new < 50:
                STATS["score_baixo"] += 1
                cache_email[key] = None
                time.sleep(0.6); continue
            # Verifier no email novo
            v_saldo = saldo("verifications")
            if v_saldo < 1000:
                log.warning("verifications<1000 — Finder sem Verifier")
                cache_email[key] = (em_new, sc_new, False, "unverified")
            else:
                STATS["hunter_verify"] += 1
                v = verifier(em_new)
                v_status = "unknown"; v_score = sc_new
                if v:
                    v_status = (v.get("status") or v.get("result") or "unknown").lower()
                    v_score = int(v.get("score") or sc_new)
                valido = v_status in ("valid","accept_all") and v_score >= 60
                if not valido:
                    STATS["verify_falhou"] += 1
                cache_email[key] = (em_new, v_score, valido, v_status)
            time.sleep(0.6)

        result = cache_email.get(key)
        if not result:
            continue
        em_new, sc, valido, v_status = result
        em_old_norm = (email_old or "").lower().strip()

        # decisão de update
        is_email_igual = em_new == em_old_norm
        is_email_novo = email_old is None
        # substituir email atual só se novo for diferente E valido E score alto
        is_substituir = (not is_email_igual and not is_email_novo and valido and sc >= 80)

        if is_email_igual:
            # mesmo email, só atualizar verifier status se valido
            if valido:
                try:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE obras SET
                                nivel1_email_smtp_verified=true,
                                nivel1_email_status=%s,
                                nivel1_email_score=%s,
                                nivel1_email_verified_at=NOW(),
                                nivel1_origem_enrichment=COALESCE(nivel1_origem_enrichment,'')||'+V6_manual_dominio'
                            WHERE id=%s
                        """, (v_status, sc, obra_id))
                    conn.commit()
                    STATS["iguais"] += 1
                except Exception as e:
                    log.warning(f"  update igual erro: {e}"); conn.rollback()
        elif is_email_novo or is_substituir:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE obras SET
                            nivel1_email=%s, nivel1_email_score=%s,
                            nivel1_email_smtp_verified=%s,
                            nivel1_email_status=%s,
                            nivel1_email_verified_at=NOW(),
                            nivel1_origem_enrichment=COALESCE(nivel1_origem_enrichment,'')||'+V6_manual_dominio',
                            nivel1_enrichment_data=NOW()
                        WHERE id=%s
                    """, (em_new, sc, valido, v_status, obra_id))
                conn.commit()
                if is_email_novo:
                    STATS["novos"] += 1
                    log.info(f"  ✓ NOVO {classif} {empresa[:35]}: ∅ → {em_new} score={sc}")
                else:
                    STATS["substituidos"] += 1
                    log.info(f"  ✓ SWAP {classif} {empresa[:35]}: {em_old_norm} → {em_new} score={sc}")
            except Exception as e:
                log.warning(f"  update novo erro: {e}"); conn.rollback()
        # else: email novo é diferente mas não valido/score baixo → não substitui
    conn.close()
    return 0

if __name__ == "__main__":
    try: rc = main()
    except BaseException as e:
        import traceback; log.error(f"UNCAUGHT: {e}"); log.error(traceback.format_exc()); rc = 1
    os._exit(rc or 0)
