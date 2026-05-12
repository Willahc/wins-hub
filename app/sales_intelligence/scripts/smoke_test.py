#!/usr/bin/env python3
"""Smoke test integrado da Onda 1 (Camadas 1 + 2).

Pipeline real (BrasilAPI + DDG + crt.sh + WHOIS + coleta emails + pattern).
NAO usa cache (migration nao aplicada). Tempo: ~1-3 min por CNPJ.

Uso: python3 smoke_test.py [cnpj1 cnpj2 ...] | sem args usa 3 default.
"""
import json
import logging
import sys
import time
from pathlib import Path

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("smoke")

from sales_intelligence.camada1_identificacao import orquestrador
from sales_intelligence.camada2_pattern_detection import (
    coletar_emails_site, coletar_emails_github, coletar_emails_whois,
    detectar_padrao, gerar_email,
)

# CNPJs default - validados em A3 manha/tarde
DEFAULT_CNPJS = [
    ("32161500000100", "CCR ViaSul (VIA SUL)", "Marcello Guidotti"),
    ("44067725000172", "Via Brasil BR-163 (VIABRASIL)", "Marcello Guidotti"),
    ("29884545000190", "EcoRioMinas (ECO RIOMINAS)", "Marcello Guidotti"),
]


def run_one(cnpj: str, label: str, nome_teste: str):
    print("\n" + "=" * 78)
    print(f"  CNPJ: {cnpj}  |  Esperado: {label}")
    print("=" * 78)
    t0 = time.time()

    # --- CAMADA 1 ---
    try:
        dossier = orquestrador.identificar_empresa(cnpj, force_refresh=True)
    except Exception as e:
        print(f"  ❌ orquestrador falhou: {e}")
        return
    print(f"\n  [Camada 1] dossier coletado em {time.time()-t0:.1f}s")
    print(f"    razao:       {dossier.razao_social}")
    print(f"    fantasia:    {dossier.nome_fantasia}")
    print(f"    natureza:    {dossier.natureza_juridica}")
    print(f"    cnae:        {dossier.cnae_fiscal} - {dossier.cnae_descricao}")
    print(f"    situacao:    {dossier.situacao_cadastral}")
    print(f"    UF/Mun:      {dossier.uf} / {dossier.municipio}")
    print(f"    matriz:      {dossier.matriz}")
    print(f"    classif:     tipo={dossier.tipo_organizacao} spv={dossier.spv_ou_matriz} "
          f"conf={dossier.confianca_classificacao}")
    if dossier.dominio_oficial:
        do = dossier.dominio_oficial
        print(f"    DOMINIO:     {do.dominio}  ({do.confianca}, score={do.score_fuzzy}, HEAD={do.head_status})")
    else:
        print(f"    DOMINIO:     <nenhum candidato confiavel>")
    print(f"    subdominios: {len(dossier.subdominios)} achados via crt.sh")
    for s in dossier.subdominios[:8]:
        print(f"      - {s}")
    if dossier.whois:
        w = dossier.whois
        print(f"    WHOIS ({w.fonte_metodo}): email={w.email_administrativo} "
              f"tel={w.telefone} reg={w.data_registro}")
    print(f"    confianca_geral: {dossier.confianca_geral}  "
          f"(fontes: {', '.join(dossier.fontes_utilizadas)})")

    # --- CAMADA 2 ---
    if not dossier.dominio_oficial:
        print(f"\n  [Camada 2] pulada (sem dominio)")
        return
    dominio = dossier.dominio_oficial.dominio
    print(f"\n  [Camada 2] coletando emails de {dominio}")
    t1 = time.time()

    emails_site = coletar_emails_site.coletar_emails_do_site(dominio)
    print(f"    site:   {len(emails_site)} emails ({time.time()-t1:.1f}s)")
    for e in emails_site[:5]:
        print(f"      - {e}")

    emails_gh = coletar_emails_github.coletar_emails_github(dominio)
    print(f"    github: {len(emails_gh)} emails")
    for e in emails_gh[:5]:
        print(f"      - {e}")

    emails_whois = coletar_emails_whois.coletar_emails_whois(dossier.whois)
    print(f"    whois:  {len(emails_whois)} emails")
    for e in emails_whois:
        print(f"      - {e}")

    all_emails = list(set(emails_site + emails_gh + emails_whois))
    print(f"    total unico: {len(all_emails)}")

    # detectar pattern
    pat = detectar_padrao.detectar_padrao(all_emails)
    if pat is None:
        print(f"\n  [Pattern] inconclusivo (amostra muito pequena ou inconsistente)")
        return
    print(f"\n  [Pattern] detectado:")
    print(f"    padrao:       {pat.padrao}")
    print(f"    confianca:    {pat.confianca}")
    print(f"    amostra:      {pat.amostra_total}")
    print(f"    exemplos:     {pat.exemplos[:3]}")

    # gerar email pro nome de teste
    email_gerado = gerar_email.gerar_email(nome_teste, pat)
    print(f"\n  [Generate] '{nome_teste}' + padrao -> {email_gerado!r}")

    print(f"\n  TEMPO TOTAL: {time.time()-t0:.1f}s")


def main():
    args = sys.argv[1:]
    if args:
        casos = [(c, c, "Marcello Guidotti") for c in args]
    else:
        casos = DEFAULT_CNPJS

    for cnpj, label, nome in casos:
        try:
            run_one(cnpj, label, nome)
        except KeyboardInterrupt:
            print("\nInterrompido.")
            return 1
        except Exception as e:
            log.exception(f"run_one falhou cnpj={cnpj}: {e}")
        # delay entre CNPJs (anti-rate-limit DDG)
        if len(casos) > 1:
            time.sleep(15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
