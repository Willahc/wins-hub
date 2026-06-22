#!/usr/bin/env python3
"""
etl_rfb_insumos_PC.py — RODA NO SEU PC (não no VPS). Sem dependências (só stdlib).

Filtra a base CNPJ aberta da Receita (CNAE 22 borracha/plástico, 23 cimento/min.não-met.,
24 metalurgia/aço) e gera um CSV pequeno para subir ao banco.

PASSO A PASSO:
  1) Baixe os arquivos da RFB para uma pasta (ex.: C:\\rfb):
       - Estabelecimentos0.zip ... Estabelecimentos9.zip
       - Empresas0.zip ... Empresas9.zip
     Fonte (jan/2026 mudou p/ NextCloud): https://dados.gov.br/dados/conjuntos-dados/cadastro-nacional-da-pessoa-juridica---cnpj
     Mirror scriptável (CDN): https://dados-abertos-rf-cnpj.casadosdados.com.br/
  2) Rode:  python etl_rfb_insumos_PC.py C:\\rfb fornecedores_insumos.csv
  3) Suba o CSV gerado e avise — eu importo no banco.

Pico de memória: ~poucas centenas de MB (só os filtrados ficam em RAM; o resto é streaming).
"""
import sys, os, csv, io, zipfile, glob

DIVISOES = {"22", "23", "24"}
# posições RFB (CSV ;-delimitado, latin-1, sem header) — layout oficial
EST_BASICO, EST_ORDEM, EST_DV, EST_SIT, EST_CNAE, EST_UF, EST_MUN = 0, 1, 2, 5, 11, 19, 20
EMP_BASICO, EMP_RAZAO, EMP_CAPITAL = 0, 1, 4


def _rows(zpath):
    with zipfile.ZipFile(zpath) as z:
        for nome in z.namelist():
            with z.open(nome) as fh:
                yield from csv.reader(io.TextIOWrapper(fh, encoding="latin-1", errors="replace"), delimiter=";")


def main():
    if len(sys.argv) < 2:
        print("uso: python etl_rfb_insumos_PC.py <pasta_dos_zips> [saida.csv]"); return
    pasta = sys.argv[1]
    saida = sys.argv[2] if len(sys.argv) > 2 else "fornecedores_insumos.csv"

    estab = sorted(glob.glob(os.path.join(pasta, "Estabelecimentos*.zip")))
    emps = sorted(glob.glob(os.path.join(pasta, "Empresas*.zip")))
    if not estab:
        print(f"ERRO: nenhum Estabelecimentos*.zip em {pasta}"); return

    print(f"PASSADA 1/2 — filtrando {len(estab)} arquivos de Estabelecimentos (CNAE 22/23/24 ativos)...")
    achados = {}  # basico -> [cnpj14, cnae, uf, mun]
    for i, zf in enumerate(estab, 1):
        n0 = len(achados)
        for r in _rows(zf):
            if len(r) <= EST_MUN or r[EST_SIT] != "02":
                continue
            cnae = (r[EST_CNAE] or "").strip()
            if cnae[:2] not in DIVISOES:
                continue
            achados[r[EST_BASICO]] = [r[EST_BASICO] + r[EST_ORDEM] + r[EST_DV], cnae, r[EST_UF], r[EST_MUN]]
        print(f"  [{i}/{len(estab)}] {os.path.basename(zf)} (+{len(achados)-n0}, total {len(achados)})")

    print(f"PASSADA 2/2 — razão social + capital de {len(emps)} arquivos de Empresas...")
    for i, zf in enumerate(emps, 1):
        for r in _rows(zf):
            if r and r[EMP_BASICO] in achados:
                rec = achados[r[EMP_BASICO]]
                rec.append(r[EMP_RAZAO] if len(r) > EMP_RAZAO else "")
                rec.append((r[EMP_CAPITAL] if len(r) > EMP_CAPITAL else "0").replace(",", "."))
        print(f"  [{i}/{len(emps)}] {os.path.basename(zf)}")

    print(f"Escrevendo {saida}...")
    n = 0
    with open(saida, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["cnpj", "divisao_cnae", "cnae_principal", "uf", "municipio", "razao_social", "capital_social"])
        for bas, rec in achados.items():
            cnpj14, cnae, uf, mun = rec[0], rec[1], rec[2], rec[3]
            razao = rec[4] if len(rec) > 4 else ""
            cap = rec[5] if len(rec) > 5 else "0"
            w.writerow([cnpj14, cnae[:2], cnae, uf, mun, razao, cap]); n += 1
    print(f"PRONTO: {n} fornecedores de insumo -> {saida}")
    print("Suba esse CSV e avise para importar no banco (status='importado_sc').")


if __name__ == "__main__":
    main()
