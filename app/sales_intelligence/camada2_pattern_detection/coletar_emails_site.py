import re
import logging
from typing import List
# playwright importado lazy dentro de coletar_emails_do_site (pode nao estar instalado)

log = logging.getLogger("sales_intel.coletar_site")

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
EXCLUDOS_LOCAL = {"test", "example", "foo", "bar", "noreply", "no-reply", "no_reply",
                  "mailer-daemon", "postmaster", "abuse", "spam", "support",
                  "sample", "your", "youremail", "exemplo", "seu", "suporte"}
PATHS = ("/", "/contato", "/sobre", "/equipe", "/institucional", "/imprensa",
         "/investidores", "/trabalhe-conosco", "/fale-conosco", "/quem-somos")

_CACHE: dict = {}


def _filtrar(emails: set, dominio_alvo: str) -> List[str]:
    out = []
    seen = set()
    for e in emails:
        e = e.lower().strip(".,;:'\"<>()")
        if e in seen:
            continue
        local, _, host = e.partition("@")
        if not host or "." not in host:
            continue
        # dominio bate
        host_lower = host.lower()
        if not (host_lower == dominio_alvo or host_lower.endswith("." + dominio_alvo)):
            continue
        # excluir local-parts genericos
        local_clean = re.sub(r"[._-]", "", local)
        if local_clean in EXCLUDOS_LOCAL or local in EXCLUDOS_LOCAL:
            continue
        seen.add(e)
        out.append(e)
    return out


def coletar_emails_do_site(dominio: str, max_pages: int = 10) -> List[str]:
    if not dominio:
        return []
    if dominio in _CACHE:
        return _CACHE[dominio]

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        log.warning(f"playwright indisponivel ({e}); coleta site pulada p/ {dominio}")
        return []

    encontrados: set = set()
    paths = list(PATHS)[:max_pages]
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/chromium-browser", headless=True,
                              args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        ctx = b.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/147.0.0.0")
        for path in paths:
            page = ctx.new_page()
            for proto in ("https", "http"):
                try:
                    r = page.goto(f"{proto}://{dominio}{path}", timeout=15000,
                                  wait_until="domcontentloaded")
                    if r and 200 <= r.status < 400:
                        page.wait_for_timeout(800)
                        body = page.content()
                        for m in EMAIL_RE.findall(body):
                            encontrados.add(m)
                        break
                except Exception as e:
                    log.debug(f"GET {proto}://{dominio}{path} falhou: {e}")
            page.close()
        b.close()

    out = _filtrar(encontrados, dominio)
    _CACHE[dominio] = out
    log.info(f"site={dominio} emails_encontrados={len(out)}")
    return out
