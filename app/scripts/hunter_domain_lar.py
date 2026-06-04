"""Hunter domain-search para larcooperativa.com.br — achar decisores Lar."""
import os, sys, requests

HUNTER_KEY = os.getenv("HUNTER_API_KEY", "").strip()

r = requests.get("https://api.hunter.io/v2/domain-search",
                 params={"domain": "larcooperativa.com.br", "api_key": HUNTER_KEY,
                         "limit": 30, "seniority": "executive,senior"},
                 timeout=20)
d = r.json().get("data") or {}
emails = d.get("emails") or []
print(f"Total emails: {len(emails)}\n")
for e in emails:
    fn = (e.get("first_name") or "").strip()
    ln = (e.get("last_name") or "").strip()
    pos = e.get("position") or ""
    dept = e.get("department") or ""
    seniority = e.get("seniority") or ""
    em = e.get("value") or ""
    conf = e.get("confidence") or 0
    print(f"  {fn} {ln:25} | {pos[:45]:45} | dept={dept:15} | sen={seniority:10} | {em} ({conf})")
