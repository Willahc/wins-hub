#!/usr/bin/env python3
"""
etl_rfb_insumos.py — ESBOÇO (NÃO EXECUTAR sem --confirm) — aquisição de inventário
de fornecedores de INSUMO da base CNPJ aberta da Receita Federal.

Objetivo: importar p/ `fornecedores` os CNPJs ATIVOS de manufatura pesada que hoje
faltam (CNAE divisões 22 borracha/plástico, 23 cimento/min.não-met., 24 metalurgia/aço),
marcados status='importado_sc' — sem enriquecer (só inventário).

Fonte (pública, gratuita):
  https://arquivos.receitafederal.gov.br/dados/cnpj/dados_abertos_cnpj/<AAAA-MM>/
  - Estabelecimentos0..9.zip  (CNAE principal, situação, UF, município)  ~ 10 arquivos
  - Empresas0..9.zip          (razão social, capital_social, porte)       ~ 10 arquivos
  - Cnaes.zip                 (dicionário CNAE)                            ~ tiny
  CSV ;-delimitado, latin-1, SEM cabeçalho.

Pipeline (2 passadas, streaming — nunca carrega tudo em RAM):
  1) Stream Estabelecimentos*: filtra cnae_fiscal_principal[:2] IN {22,23,24}
     E situacao_cadastral='02' (ativa). Guarda (cnpj_basico, cnpj14, cnae, uf, municipio).
     -> resultado pequeno (dezenas de milhares), cabe em memória.
  2) Stream Empresas*: para os cnpj_basico coletados, pega razao_social + capital_social + porte.
  3) Monta cnpj14 = basico(8)+ordem(4)+dv(2); INSERT em fornecedores (ON CONFLICT cnpj DO NOTHING)
     com status='importado_sc', divisao_cnae=cnae[:2], situacao_cadastral='02'.

ESTIMATIVAS (ordem de grandeza, base CNPJ ~64M):
  - Download: Estabelecimentos ~10×350MB ≈ 3,5GB zip (~25-30GB descompactado);
    Empresas ~10×80MB ≈ 800MB. Total download ~4,3GB → ~30-60 min na banda da VPS.
  - Processamento (1vCPU, streaming): ~1-2h (I/O-bound; sem carregar tudo).
  - CNPJs ATIVOS esperados nas 3 divisões (estimativa):
      22 borracha/plástico ........ ~60-90k  (muitos pequenos transformadores de plástico)
      23 cimento/min.não-met. ..... ~30-50k  (artefatos de cimento, cerâmica, vidro)
      24 metalurgia/aço ........... ~8-15k   (siderurgia + não-ferrosos + fundição)
      TOTAL ....................... ~100-155k ativos
    OBS: divisão-nível inclui muita oficina pequena. Para "indústria relevante" (médias+),
    filtrar capital_social/porte -> ~ poucos milhares por divisão (o alvo comercial real).
  - Impacto no banco: fornecedores 3,97M -> +~100-155k (+3-4%); cuidado com VACUUM pós-carga.

⚠️ Rodar OFF-PEAK (não na janela 05:00-07:00 do orchestrator) e idealmente fora da VPS
1vCPU (processar num box separado e só fazer o INSERT final). feedback_vps_overload.
"""
import os, sys, csv, io, zipfile, glob, urllib.request

BASE_URL = os.getenv("RFB_BASE_URL", "https://arquivos.receitafederal.gov.br/dados/cnpj/dados_abertos_cnpj/")
DIVISOES_ALVO = {"22", "23", "24"}
DEST_DIR = os.getenv("RFB_DOWNLOAD_DIR", "/tmp/rfb")

# --- colunas RFB (posicionais, sem header) ---
EST_CNPJ_BASICO, EST_ORDEM, EST_DV = 0, 1, 2
EST_SITUACAO = 5            # '02' = ATIVA
EST_CNAE_PRINC = 11        # cnae_fiscal_principal
EST_UF, EST_MUN = 19, 20
EMP_CNPJ_BASICO, EMP_RAZAO, EMP_CAPITAL, EMP_PORTE = 0, 1, 4, 5


def baixar(periodo):
    os.makedirs(DEST_DIR, exist_ok=True)
    alvos = [f"Estabelecimentos{i}.zip" for i in range(10)] + \
            [f"Empresas{i}.zip" for i in range(10)] + ["Cnaes.zip"]
    for fn in alvos:
        url = f"{BASE_URL}{periodo}/{fn}"
        dst = os.path.join(DEST_DIR, fn)
        if os.path.exists(dst):
            continue
        print(f"baixando {url}")
        urllib.request.urlretrieve(url, dst)


def passada1_estabelecimentos():
    """Stream dos Estabelecimentos*; devolve dict cnpj_basico -> (cnpj14, cnae, uf, mun)."""
    achados = {}
    for zf in sorted(glob.glob(os.path.join(DEST_DIR, "Estabelecimentos*.zip"))):
        with zipfile.ZipFile(zf) as z:
            for nome in z.namelist():
                with z.open(nome) as fh:
                    rdr = csv.reader(io.TextIOWrapper(fh, encoding="latin-1"), delimiter=";")
                    for r in rdr:
                        if len(r) <= EST_CNAE_PRINC:
                            continue
                        if r[EST_SITUACAO] != "02":
                            continue
                        cnae = (r[EST_CNAE_PRINC] or "").strip()
                        if cnae[:2] not in DIVISOES_ALVO:
                            continue
                        bas = r[EST_CNPJ_BASICO]
                        cnpj14 = bas + r[EST_ORDEM] + r[EST_DV]
                        achados[bas] = (cnpj14, cnae, r[EST_UF], r[EST_MUN])
    return achados


def passada2_empresas(basicos):
    """Stream das Empresas*; razao_social + capital_social só p/ os básicos achados."""
    info = {}
    for zf in sorted(glob.glob(os.path.join(DEST_DIR, "Empresas*.zip"))):
        with zipfile.ZipFile(zf) as z:
            for nome in z.namelist():
                with z.open(nome) as fh:
                    rdr = csv.reader(io.TextIOWrapper(fh, encoding="latin-1"), delimiter=";")
                    for r in rdr:
                        if r and r[EMP_CNPJ_BASICO] in basicos:
                            cap = (r[EMP_CAPITAL] or "0").replace(",", ".")
                            info[r[EMP_CNPJ_BASICO]] = (r[EMP_RAZAO], cap, r[EMP_PORTE])
    return info


def carregar(achados, info, conn):
    sql = """INSERT INTO fornecedores
      (cnpj, razao_social, divisao_cnae, cnae_principal, uf, capital_social,
       situacao_cadastral, status)
      VALUES (%s,%s,%s,%s,%s,%s,'02','importado_sc')
      ON CONFLICT (cnpj) DO NOTHING"""
    cur = conn.cursor(); n = 0
    for bas, (cnpj14, cnae, uf, mun) in achados.items():
        razao, cap, _porte = info.get(bas, (None, "0", None))
        cur.execute(sql, (cnpj14, razao, cnae[:2], cnae, uf, float(cap or 0)))
        n += 1
    conn.commit(); return n


def main():
    if "--confirm" not in sys.argv:
        print("DRY: esboço. Rode com --confirm --periodo AAAA-MM para executar (pesado).")
        return
    periodo = sys.argv[sys.argv.index("--periodo") + 1]
    baixar(periodo)
    achados = passada1_estabelecimentos()
    print(f"estabelecimentos ativos 22/23/24: {len(achados)}")
    info = passada2_empresas(set(achados))
    import psycopg2
    conn = psycopg2.connect(host=os.getenv("DB_HOST", "db"), dbname=os.getenv("DB_NAME", "wins_hub"),
                            user=os.getenv("DB_USER", "postgres"), password=os.getenv("DB_PASSWORD", ""))
    print(f"inseridos: {carregar(achados, info, conn)}")


if __name__ == "__main__":
    main()
