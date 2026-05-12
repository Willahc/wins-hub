from typing import List, Optional


def coletar_emails_whois(whois_data) -> List[str]:
    """Extrai emails do WHOIS/RDAP. Aceita dict ou WhoisInfo."""
    if not whois_data:
        return []
    if hasattr(whois_data, "model_dump"):
        whois_data = whois_data.model_dump()
    out: set = set()
    em = whois_data.get("email_administrativo")
    if em:
        out.add(em.lower())
    return sorted(out)
