#!/usr/bin/env python3
"""ETL VPS disk-safe: baixa 1 arquivo por vez do mirror, filtra CNAE 22/23/24 ativos,
APAGA antes do próximo (pico ~2GB), gera CSV. stdlib pura."""
import os, sys, csv, io, zipfile, urllib.request, time

BASE = "https://dados-abertos-rf-cnpj.casadosdados.com.br/arquivos/2026-01-11"
TMP = "/home/william/_rfb_tmp"
OUT = "/home/william/fornecedores_insumos.csv"
DIVISOES = {"22", "23", "24"}
EST_BASICO, EST_ORDEM, EST_DV, EST_SIT, EST_CNAE, EST_UF, EST_MUN = 0, 1, 2, 5, 11, 19, 20
EMP_BASICO, EMP_RAZAO, EMP_CAPITAL = 0, 1, 4
os.makedirs(TMP, exist_ok=True)
_op = urllib.request.build_opener()
_op.addheaders = [('User-Agent', 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36')]
urllib.request.install_opener(_op)


def log(m): print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def baixa(fn):
    dst = os.path.join(TMP, fn)
    urllib.request.urlretrieve(f"{BASE}/{fn}", dst)
    return dst


def rows(zpath):
    with zipfile.ZipFile(zpath) as z:
        for nome in z.namelist():
            with z.open(nome) as fh:
                yield from csv.reader(io.TextIOWrapper(fh, encoding="latin-1", errors="replace"), delimiter=";")


def main():
    achados = {}
    log("=== PASSADA 1: Estabelecimentos (10) ===")
    for i in range(10):
        fn = f"Estabelecimentos{i}.zip"
        try:
            p = baixa(fn); n0 = len(achados)
            for r in rows(p):
                if len(r) <= EST_MUN or r[EST_SIT] != "02":
                    continue
                cnae = (r[EST_CNAE] or "").strip()
                if cnae[:2] in DIVISOES:
                    achados[r[EST_BASICO]] = [r[EST_BASICO]+r[EST_ORDEM]+r[EST_DV], cnae, r[EST_UF], r[EST_MUN]]
            os.remove(p)
            log(f"  {fn}: +{len(achados)-n0} (total {len(achados)})")
        except Exception as e:
            log(f"  ERRO {fn}: {e!r}")
    log("=== PASSADA 2: Empresas (10) — razão+capital ===")
    for i in range(10):
        fn = f"Empresas{i}.zip"
        try:
            p = baixa(fn)
            for r in rows(p):
                if r and r[EMP_BASICO] in achados:
                    rec = achados[r[EMP_BASICO]]
                    if len(rec) == 4:
                        rec.append(r[EMP_RAZAO] if len(r) > EMP_RAZAO else "")
                        rec.append((r[EMP_CAPITAL] if len(r) > EMP_CAPITAL else "0").replace(",", "."))
            os.remove(p)
            log(f"  {fn} ok")
        except Exception as e:
            log(f"  ERRO {fn}: {e!r}")
    log(f"Escrevendo {OUT} ({len(achados)} fornecedores)...")
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["cnpj", "divisao_cnae", "cnae_principal", "uf", "municipio", "razao_social", "capital_social"])
        for bas, rec in achados.items():
            w.writerow([rec[0], rec[1][:2], rec[1], rec[2], rec[3],
                        rec[4] if len(rec) > 4 else "", rec[5] if len(rec) > 5 else "0"])
    log(f"PRONTO: {OUT}")


if __name__ == "__main__":
    main()
