"""
Importação COMPLETA dos CNPJs CLIENTES (donos de obra).
Processa 3 fontes da Receita Federal:
  - Estabelecimentos*.zip (10 arquivos): endereço, telefone, email, CNAE, situação
  - Empresas*.zip (10 arquivos):         razão social, capital, porte, natureza
  - Socios*.zip (10 arquivos):           quadro societário (QSA)

Filtra POR CNPJ (lista) e insere em empresas_clientes.
"""
import os
import sys
import csv
import io
import json
import zipfile
import logging
import argparse
from pathlib import Path
from datetime import datetime

import requests
import psycopg2
from psycopg2.extras import execute_values

log = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

MIRROR_BASE = "https://dados-abertos-rf-cnpj.casadosdados.com.br/arquivos"
PASTA_DEFAULT = "2026-04-12"
WORK_DIR = Path("/tmp/receita_clientes")
BATCH_SIZE = 500


# ==================== HELPERS ====================

def carregar_cnpjs(arquivo):
    """Carrega lista de CNPJs (14 dígitos) e extrai CNPJs base (8 dígitos)."""
    cnpjs_completos = set()
    cnpjs_base = set()
    with open(arquivo, 'r') as f:
        for linha in f:
            cnpj = linha.strip()
            if len(cnpj) == 14 and cnpj.isdigit():
                cnpjs_completos.add(cnpj)
                cnpjs_base.add(cnpj[:8])
    return cnpjs_completos, cnpjs_base


def baixar_arquivo(url, destino, chunk_size=8 * 1024 * 1024):
    log.info(f"Baixando {url}")
    inicio = datetime.now()
    r = requests.get(url, stream=True, timeout=300)
    r.raise_for_status()
    total = 0
    with open(destino, 'wb') as f:
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                f.write(chunk)
                total += len(chunk)
    duracao = (datetime.now() - inicio).total_seconds()
    log.info(f"  {total/1024/1024:.1f} MB em {duracao:.1f}s ({total/1024/1024/max(duracao,0.1):.1f} MB/s)")


def parse_data(data_str):
    if not data_str or len(data_str) != 8:
        return None
    try:
        return datetime.strptime(data_str, "%Y%m%d").date()
    except ValueError:
        return None


def parse_capital(s):
    """Capital social vem como '1234567,89'"""
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def situacao_codigo(c):
    return {"01": "NULA", "02": "ATIVA", "03": "SUSPENSA", "04": "INAPTA", "08": "BAIXADA"}.get(c, c or "DESCONHECIDA")


def porte_codigo(c):
    return {"00": "NAO_INFORMADO", "01": "MICRO", "03": "PEQUENA", "05": "DEMAIS"}.get(c, c or "DESCONHECIDO")


def qualificacao_codigo(c):
    """Códigos de qualificação dos sócios (Receita Federal)"""
    return {
        "05": "ADMINISTRADOR",
        "08": "CONSELHEIRO_ADMINISTRACAO",
        "10": "DIRETOR",
        "16": "PRESIDENTE",
        "17": "PROCURADOR",
        "20": "SOCIO",
        "22": "SOCIO_COTISTA",
        "23": "SOCIO_INCAPAZ_OU_RELATIVAMENTE_INCAPAZ",
        "29": "RECUPERADOR_JUDICIAL",
        "37": "SOCIO_PESSOA_JURIDICA_DOMICILIADO_NO_EXTERIOR",
        "49": "SOCIO_ADMINISTRADOR",
        "54": "FUNDADOR",
        "65": "TITULAR_PESSOA_FISICA_RESIDENTE_OU_DOMICILIADO_NO_BRASIL",
    }.get(c, c or "DESCONHECIDO")


# ==================== ESTABELECIMENTOS ====================

def processar_estabelecimentos(zip_path, cnpjs_alvo, conn, max_linhas=None):
    """
    Layout Estabelecimentos:
    0:cnpj_base 1:cnpj_ordem 2:cnpj_dv 3:matriz_filial 4:nome_fantasia 5:situacao
    6:data_situacao 7:motivo 8:cidade_ext 9:pais 10:data_inicio 11:cnae_principal
    12:cnaes_sec 13:tipo_log 14:logradouro 15:numero 16:complemento 17:bairro
    18:cep 19:uf 20:municipio 21:ddd1 22:tel1 23:ddd2 24:tel2
    25:ddd_fax 26:fax 27:email 28:sit_especial 29:data_sit_especial
    """
    stats = {"total_linhas": 0, "matched": 0, "inseridas": 0}
    batch = []

    with zipfile.ZipFile(zip_path, 'r') as zf:
        nomes = zf.namelist()
        if not nomes:
            return stats
        log.info(f"  CSV: {nomes[0]}")

        with zf.open(nomes[0]) as f:
            text = io.TextIOWrapper(f, encoding="latin-1", errors="replace")
            reader = csv.reader(text, delimiter=';', quotechar='"')

            for row in reader:
                stats["total_linhas"] += 1
                if max_linhas and stats["total_linhas"] >= max_linhas:
                    break
                if len(row) < 28:
                    continue

                cnpj = (row[0] + row[1] + row[2]).strip()
                if cnpj not in cnpjs_alvo:
                    continue

                stats["matched"] += 1

                ddd1, tel1 = row[21].strip(), row[22].strip()
                tel_1 = f"({ddd1}) {tel1}" if ddd1 and tel1 else (tel1 or None)
                ddd2, tel2 = row[23].strip(), row[24].strip()
                tel_2 = f"({ddd2}) {tel2}" if ddd2 and tel2 else (tel2 or None)

                tipo_log = row[13].strip()
                log_nome = row[14].strip()
                logradouro = f"{tipo_log} {log_nome}".strip() if log_nome else None

                batch.append((
                    cnpj,
                    None,  # razao_social - vem de Empresas
                    row[4].strip() or None,   # nome_fantasia
                    row[11].strip() or None,  # cnae_principal
                    None,  # cnae_descricao
                    logradouro,
                    row[15].strip() or None,  # numero
                    row[16].strip() or None,  # complemento
                    row[17].strip() or None,  # bairro
                    row[18].strip() or None,  # cep
                    None,  # municipio_nome
                    row[19].strip() or None,  # uf
                    tel_1,
                    tel_2,
                    row[27].strip().lower() or None,  # email
                    situacao_codigo(row[5].strip()),
                    None,  # porte - vem de Empresas
                    None,  # capital - vem de Empresas
                    parse_data(row[10].strip()),  # data_abertura
                    None,  # natureza - vem de Empresas
                    None,  # qsa - vem de Sócios
                    'receita_federal',
                ))

                if len(batch) >= BATCH_SIZE:
                    inseridas = inserir_estabelecimentos(conn, batch)
                    stats["inseridas"] += inseridas
                    batch = []

    if batch:
        stats["inseridas"] += inserir_estabelecimentos(conn, batch)

    return stats


def inserir_estabelecimentos(conn, batch):
    sql = """
        INSERT INTO empresas_clientes (
            cnpj, razao_social, nome_fantasia, cnae_principal, cnae_descricao,
            logradouro, numero, complemento, bairro, cep,
            municipio_nome, uf, telefone_1, telefone_2, email,
            situacao, porte, capital_social, data_abertura, natureza_juridica,
            qsa, fonte
        ) VALUES %s
        ON CONFLICT (cnpj) DO UPDATE SET
            nome_fantasia = COALESCE(EXCLUDED.nome_fantasia, empresas_clientes.nome_fantasia),
            cnae_principal = COALESCE(EXCLUDED.cnae_principal, empresas_clientes.cnae_principal),
            logradouro = COALESCE(EXCLUDED.logradouro, empresas_clientes.logradouro),
            numero = COALESCE(EXCLUDED.numero, empresas_clientes.numero),
            complemento = COALESCE(EXCLUDED.complemento, empresas_clientes.complemento),
            bairro = COALESCE(EXCLUDED.bairro, empresas_clientes.bairro),
            cep = COALESCE(EXCLUDED.cep, empresas_clientes.cep),
            uf = COALESCE(EXCLUDED.uf, empresas_clientes.uf),
            telefone_1 = COALESCE(EXCLUDED.telefone_1, empresas_clientes.telefone_1),
            telefone_2 = COALESCE(EXCLUDED.telefone_2, empresas_clientes.telefone_2),
            email = COALESCE(EXCLUDED.email, empresas_clientes.email),
            situacao = EXCLUDED.situacao,
            data_abertura = COALESCE(EXCLUDED.data_abertura, empresas_clientes.data_abertura),
            atualizado_em = now()
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, batch, page_size=BATCH_SIZE)
    conn.commit()
    return len(batch)


# ==================== EMPRESAS (matriz) ====================

def processar_empresas(zip_path, cnpjs_base_alvo, conn, max_linhas=None):
    """
    Layout Empresas:
    0:cnpj_base 1:razao_social 2:natureza_juridica_codigo 3:qualif_responsavel
    4:capital_social 5:porte 6:ente_federativo
    """
    stats = {"total_linhas": 0, "matched": 0, "atualizadas": 0}
    batch = []

    with zipfile.ZipFile(zip_path, 'r') as zf:
        nomes = zf.namelist()
        if not nomes:
            return stats
        log.info(f"  CSV: {nomes[0]}")

        with zf.open(nomes[0]) as f:
            text = io.TextIOWrapper(f, encoding="latin-1", errors="replace")
            reader = csv.reader(text, delimiter=';', quotechar='"')

            for row in reader:
                stats["total_linhas"] += 1
                if max_linhas and stats["total_linhas"] >= max_linhas:
                    break
                if len(row) < 6:
                    continue

                cnpj_base = row[0].strip()
                if cnpj_base not in cnpjs_base_alvo:
                    continue

                stats["matched"] += 1

                batch.append((
                    cnpj_base,
                    row[1].strip() or None,           # razao_social
                    row[2].strip() or None,           # natureza_juridica_codigo
                    parse_capital(row[4].strip()),    # capital_social
                    porte_codigo(row[5].strip()),     # porte
                ))

                if len(batch) >= BATCH_SIZE:
                    stats["atualizadas"] += atualizar_empresas(conn, batch)
                    batch = []

    if batch:
        stats["atualizadas"] += atualizar_empresas(conn, batch)

    return stats


def atualizar_empresas(conn, batch):
    """UPDATE em massa por CNPJ base (8 dígitos = matriz + filiais)"""
    sql = """
        UPDATE empresas_clientes ec SET
            razao_social = data.razao_social,
            natureza_juridica = data.natureza_juridica,
            capital_social = data.capital_social,
            porte = data.porte,
            atualizado_em = now()
        FROM (VALUES %s) AS data(cnpj_base, razao_social, natureza_juridica, capital_social, porte)
        WHERE substring(ec.cnpj, 1, 8) = data.cnpj_base
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, batch, page_size=BATCH_SIZE)
        afetadas = cur.rowcount
    conn.commit()
    return afetadas


# ==================== SÓCIOS ====================

def processar_socios(zip_path, cnpjs_base_alvo, conn, max_linhas=None):
    """
    Layout Sócios:
    0:cnpj_base 1:identif_socio (1=PJ 2=PF 3=Estrangeiro) 2:nome_socio
    3:cpf_cnpj_socio 4:qualificacao 5:data_entrada 6:pais
    7:cpf_repr_legal 8:nome_repr_legal 9:qualif_repr_legal 10:faixa_etaria
    """
    stats = {"total_linhas": 0, "matched": 0}
    socios_por_cnpj_base = {}  # cnpj_base -> list of socios

    with zipfile.ZipFile(zip_path, 'r') as zf:
        nomes = zf.namelist()
        if not nomes:
            return stats, {}
        log.info(f"  CSV: {nomes[0]}")

        with zf.open(nomes[0]) as f:
            text = io.TextIOWrapper(f, encoding="latin-1", errors="replace")
            reader = csv.reader(text, delimiter=';', quotechar='"')

            for row in reader:
                stats["total_linhas"] += 1
                if max_linhas and stats["total_linhas"] >= max_linhas:
                    break
                if len(row) < 11:
                    continue

                cnpj_base = row[0].strip()
                if cnpj_base not in cnpjs_base_alvo:
                    continue

                stats["matched"] += 1

                socio = {
                    "tipo": {"1": "PJ", "2": "PF", "3": "ESTRANGEIRO"}.get(row[1].strip(), "?"),
                    "nome": row[2].strip() or None,
                    "cpf_cnpj": row[3].strip() or None,
                    "qualificacao": qualificacao_codigo(row[4].strip()),
                    "data_entrada": row[5].strip() or None,
                }
                socios_por_cnpj_base.setdefault(cnpj_base, []).append(socio)

    return stats, socios_por_cnpj_base


def aplicar_socios(conn, socios_por_cnpj_base):
    """Aplica QSA na tabela empresas_clientes"""
    if not socios_por_cnpj_base:
        return 0

    batch = [
        (cnpj_base, json.dumps(socios, ensure_ascii=False))
        for cnpj_base, socios in socios_por_cnpj_base.items()
    ]

    sql = """
        UPDATE empresas_clientes ec SET
            qsa = data.qsa::jsonb,
            atualizado_em = now()
        FROM (VALUES %s) AS data(cnpj_base, qsa)
        WHERE substring(ec.cnpj, 1, 8) = data.cnpj_base
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, batch, page_size=BATCH_SIZE)
        afetadas = cur.rowcount
    conn.commit()
    return afetadas


# ==================== MAIN ====================

def baixar_e_processar(grupo, qtd, pasta, cnpjs_alvo, cnpjs_base_alvo, conn, max_linhas):
    """Baixa e processa N arquivos de um grupo (Estabelecimentos/Empresas/Socios)"""
    total = {"total_linhas": 0, "matched": 0, "inseridas": 0, "atualizadas": 0}
    socios_acumulados = {}

    for i in range(qtd):
        nome = f"{grupo}{i}.zip"
        url = f"{MIRROR_BASE}/{pasta}/{nome}"
        destino = WORK_DIR / nome

        log.info("-" * 60)
        log.info(f"{grupo} {i+1}/{qtd}: {nome}")

        try:
            baixar_arquivo(url, destino)

            if grupo == "Estabelecimentos":
                stats = processar_estabelecimentos(destino, cnpjs_alvo, conn, max_linhas)
            elif grupo == "Empresas":
                stats = processar_empresas(destino, cnpjs_base_alvo, conn, max_linhas)
            elif grupo == "Socios":
                stats, socios_arquivo = processar_socios(destino, cnpjs_base_alvo, conn, max_linhas)
                # Merge sócios em estrutura acumulada
                for cnpj_base, socios in socios_arquivo.items():
                    socios_acumulados.setdefault(cnpj_base, []).extend(socios)
            else:
                stats = {}

            log.info(f"  RESULTADO: {stats}")
            for k, v in stats.items():
                total[k] = total.get(k, 0) + v

        except Exception as e:
            log.exception(f"Erro processando {nome}: {e}")
        finally:
            if destino.exists():
                destino.unlink()

    # Aplica sócios acumulados (1 update grande)
    if grupo == "Socios" and socios_acumulados:
        log.info(f"  Aplicando QSA em {len(socios_acumulados)} CNPJs base...")
        afetadas = aplicar_socios(conn, socios_acumulados)
        log.info(f"  Linhas afetadas: {afetadas}")
        total["atualizadas"] = afetadas

    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cnpjs", required=True)
    parser.add_argument("--pasta", default=PASTA_DEFAULT)
    parser.add_argument("--apenas", type=int)
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--skip", choices=["estab", "emp", "soc"], action="append", default=[])
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    cnpjs_alvo, cnpjs_base_alvo = carregar_cnpjs(args.cnpjs)
    log.info(f"CNPJs alvo (14 dígitos): {len(cnpjs_alvo)}")
    log.info(f"CNPJs base (8 dígitos):  {len(cnpjs_base_alvo)}")

    if not cnpjs_alvo:
        log.error("Nenhum CNPJ válido")
        sys.exit(1)

    conn = psycopg2.connect(**DB_CONFIG)
    qtd = 1 if args.test else (args.apenas or 10)
    max_linhas = 100000 if args.test else None
    inicio = datetime.now()

    # FASE 1: Estabelecimentos (cria registros base)
    if "estab" not in args.skip:
        log.info("=" * 60)
        log.info("FASE 1/3: ESTABELECIMENTOS")
        log.info("=" * 60)
        r1 = baixar_e_processar("Estabelecimentos", qtd, args.pasta, cnpjs_alvo, cnpjs_base_alvo, conn, max_linhas)
        log.info(f"FASE 1 TOTAL: {r1}")

    # FASE 2: Empresas (preenche razão social, capital, porte, natureza)
    if "emp" not in args.skip:
        log.info("=" * 60)
        log.info("FASE 2/3: EMPRESAS")
        log.info("=" * 60)
        r2 = baixar_e_processar("Empresas", qtd, args.pasta, cnpjs_alvo, cnpjs_base_alvo, conn, max_linhas)
        log.info(f"FASE 2 TOTAL: {r2}")

    # FASE 3: Sócios (preenche QSA)
    if "soc" not in args.skip:
        log.info("=" * 60)
        log.info("FASE 3/3: SÓCIOS")
        log.info("=" * 60)
        r3 = baixar_e_processar("Socios", qtd, args.pasta, cnpjs_alvo, cnpjs_base_alvo, conn, max_linhas)
        log.info(f"FASE 3 TOTAL: {r3}")

    duracao = (datetime.now() - inicio).total_seconds()
    log.info("=" * 60)
    log.info(f"FIM GERAL: duracao {duracao/60:.1f} minutos")
    log.info("=" * 60)

    # Resumo final do banco
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(razao_social) as com_razao,
                COUNT(capital_social) as com_capital,
                COUNT(qsa) as com_qsa,
                COUNT(email) as com_email,
                COUNT(telefone_1) as com_tel
            FROM empresas_clientes
        """)
        r = cur.fetchone()
        log.info(f"empresas_clientes: total={r[0]} razao={r[1]} capital={r[2]} qsa={r[3]} email={r[4]} tel={r[5]}")

    conn.close()


if __name__ == "__main__":
    main()
