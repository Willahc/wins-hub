"""Tentativas alternativas pra Lar."""
import os, requests, sys, time

HUNTER_KEY = os.getenv("HUNTER_API_KEY", "").strip()

# Domain-search sem filtros
r = requests.get("https://api.hunter.io/v2/domain-search",
                 params={"domain": "larcooperativa.com.br", "api_key": HUNTER_KEY, "limit": 50},
                 timeout=20)
d = r.json().get("data") or {}
emails = d.get("emails") or []
print(f"Domain-search sem filtro: {len(emails)} emails")
for e in emails[:30]:
    print(f"  {e.get('first_name','')} {e.get('last_name','')} | {e.get('position','')} | {e.get('value','')} ({e.get('confidence',0)})")

# Email-finder variações Irineo
print("\nVariações Irineo:")
for fn, ln in [("Irineo", "Rodrigues"), ("Irineo", "da Costa"), ("Irineo", "Costa Rodrigues"), ("Irineo Costa", "Rodrigues"), ("Irineu", "Rodrigues")]:
    r = requests.get("https://api.hunter.io/v2/email-finder",
                     params={"domain": "larcooperativa.com.br", "first_name": fn,
                             "last_name": ln, "api_key": HUNTER_KEY}, timeout=15)
    d = r.json().get("data") or {}
    print(f"  {fn} {ln}: email={d.get('email')} score={d.get('score')}")
    time.sleep(0.3)

# Verificar variações comuns inferindo do pattern
print("\nGuess + verify (pode falhar SMTP):")
for em in ["irineo.rodrigues@larcooperativa.com.br","irineo@larcooperativa.com.br","i.rodrigues@larcooperativa.com.br","presidencia@larcooperativa.com.br","diretoria@larcooperativa.com.br"]:
    r = requests.get("https://api.hunter.io/v2/email-verifier",
                     params={"email": em, "api_key": HUNTER_KEY}, timeout=15)
    d = r.json().get("data") or {}
    print(f"  {em} -> status={d.get('status')} score={d.get('score')} smtp={d.get('smtp_check')}")
    time.sleep(0.3)
