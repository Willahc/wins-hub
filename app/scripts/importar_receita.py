"""
Importacao filtrada do dump da Receita Federal (estabelecimentos).
Baixa do mirror Casa dos Dados, descomprime em streaming, filtra por CNAE de interesse
+ situacao ATIVA, e insere em fornecedores.

Uso:
    python importar_receita.py --test           # baixa 1 arquivo, processa 10k linhas
    python importar_receita.py                  # importacao completa (todos os 10 arquivos)
    python importar_receita.py --pasta 2026-04-12  # especifica versao do dump
"""
import os
import sys
import csv
import io
import zipfile
import logging
import argparse
import shutil
from pathlib import Path
from datetime import datetime, date

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
PASTA_DEFAULT = "2026-05-10"
WORK_DIR = Path("/tmp/receita_dump")
BATCH_SIZE = 1000
SITUACAO_ATIVA = "02"
TEST_MAX_LINHAS = 10000


def _parse_rfb_date(s):
    s = (s or "").strip()
    if len(s) != 8 or s == "00000000":
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def carregar_cnaes_interesse(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT cnae FROM cnaes_interesse")
        return {row[0] for row in cur.fetchall()}


def baixar_arquivo(url, destino, chunk_size=8 * 1024 * 1024):
    log.info(f"Baixando {url}")
    log.info(f"  destino: {destino}")
    inicio = datetime.now()
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        baixado = 0
        with open(destino, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    baixado += len(chunk)
    duracao = (datetime.now() - inicio).total_seconds()
    mb = baixado / (1024 * 1024)
    log.info(f"  {mb:.1f} MB em {duracao:.1f}s ({mb/duracao:.1f} MB/s)")
    return baixado


def processar_zip(zip_path, cnaes_interesse, conn, pasta, max_linhas=None):
    total_linhas = 0
    inseridas = 0
    pulou_situacao = 0
    pulou_cnae = 0
    batch = []

    with zipfile.ZipFile(zip_path) as zf:
        nome_csv = zf.namelist()[0]
        log.info(f"  CSV interno: {nome_csv}")

        with zf.open(nome_csv) as raw:
            text = io.TextIOWrapper(raw, encoding="latin-1", newline="")
            reader = csv.reader(text, delimiter=";", quotechar='"')

            for row in reader:
                total_linhas += 1
                if max_linhas and total_linhas > max_linhas:
                    break

                if len(row) < 28:
                    continue

                situacao = row[5]
                if situacao != SITUACAO_ATIVA:
                    pulou_situacao += 1
                    continue

                tipo_estabelecimento = row[3].strip() or None
                data_situacao_cadastral = _parse_rfb_date(row[6])

                cnae_principal = row[11].strip()
                if cnae_principal not in cnaes_interesse:
                    pulou_cnae += 1
                    continue

                cnpj = (row[0] + row[1] + row[2]).zfill(14)
                cnae_secundarios = [c.strip() for c in row[12].split(",") if c.strip()]
                logradouro = f"{row[13]} {row[14]}".strip() if row[13] or row[14] else None
                cep = row[18].strip() or None
                uf = row[19].strip() or None
                municipio_rfb = row[20].strip() or None
                ddd1 = row[21].strip()
                tel1 = row[22].strip()
                tel_full_1 = f"({ddd1}) {tel1}" if ddd1 and tel1 else None
                ddd2 = row[23].strip()
                tel2 = row[24].strip()
                tel_full_2 = f"({ddd2}) {tel2}" if ddd2 and tel2 else None
                email = row[27].strip().lower() or None
                nome_fantasia = row[4].strip() or None

                batch.append((
                    cnpj,
                    nome_fantasia,
                    nome_fantasia,
                    cnae_principal,
                    cnae_secundarios or None,
                    logradouro,
                    row[15].strip() or None,
                    row[16].strip() or None,
                    row[17].strip() or None,
                    cep,
                    None,
                    municipio_rfb,
                    None,
                    uf,
                    tel_full_1,
                    tel_full_2,
                    email,
                    "ATIVA",
                    None,
                    None,
                    None,
                    situacao,
                    data_situacao_cadastral,
                    tipo_estabelecimento,
                    pasta,
                ))

                if len(batch) >= BATCH_SIZE:
                    inseridas += flush_batch(conn, batch)
                    batch = []
                    if total_linhas % 100000 == 0:
                        log.info(f"  processadas {total_linhas:,} | inseridas {inseridas:,}")

            if batch:
                inseridas += flush_batch(conn, batch)

    return {
        "total_linhas": total_linhas,
        "inseridas": inseridas,
        "pulou_situacao": pulou_situacao,
        "pulou_cnae": pulou_cnae,
    }


def flush_batch(conn, batch):
    sql = """
        INSERT INTO fornecedores (
            cnpj, razao_social, nome_fantasia,
            cnae_principal, cnae_secundarios,
            logradouro, numero, complemento, bairro, cep,
            municipio_ibge, municipio_rfb, municipio_nome, uf,
            telefone_1, telefone_2, email,
            situacao, porte, capital_social, data_abertura,
            situacao_cadastral, data_situacao_cadastral, tipo_estabelecimento, fonte_dump_rfb
        ) VALUES %s
        ON CONFLICT (cnpj) DO UPDATE SET
            nome_fantasia = COALESCE(EXCLUDED.nome_fantasia, fornecedores.nome_fantasia),
            cnae_principal = EXCLUDED.cnae_principal,
            cnae_secundarios = EXCLUDED.cnae_secundarios,
            municipio_rfb = EXCLUDED.municipio_rfb,
            uf = EXCLUDED.uf,
            situacao_cadastral = EXCLUDED.situacao_cadastral,
            data_situacao_cadastral = COALESCE(EXCLUDED.data_situacao_cadastral, fornecedores.data_situacao_cadastral),
            tipo_estabelecimento = EXCLUDED.tipo_estabelecimento,
            fonte_dump_rfb = EXCLUDED.fonte_dump_rfb,
            atualizado_em = now()
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, batch)
    conn.commit()
    return len(batch)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--pasta", default=PASTA_DEFAULT)
    parser.add_argument("--apenas", type=int)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    cnaes = carregar_cnaes_interesse(conn)
    log.info(f"CNAEs de interesse: {len(cnaes)}")

    qtd_arquivos = 1 if args.test else (args.apenas or 10)
    inicio = datetime.now()
    total_geral = {"total_linhas": 0, "inseridas": 0, "pulou_situacao": 0, "pulou_cnae": 0}

    for i in range(qtd_arquivos):
        nome = f"Estabelecimentos{i}.zip"
        url = f"{MIRROR_BASE}/{args.pasta}/{nome}"
        destino = WORK_DIR / nome

        log.info("=" * 60)
        log.info(f"ARQUIVO {i+1}/{qtd_arquivos}: {nome}")
        log.info("=" * 60)

        try:
            baixar_arquivo(url, destino)
            max_linhas = TEST_MAX_LINHAS if args.test else None
            stats = processar_zip(destino, cnaes, conn, args.pasta, max_linhas=max_linhas)
            log.info(f"  RESULTADO: {stats}")
            for k, v in stats.items():
                total_geral[k] += v
        except Exception as e:
            log.exception(f"Erro processando {nome}: {e}")
        finally:
            if destino.exists():
                destino.unlink()
                log.info(f"  ZIP removido: {destino}")

    duracao = (datetime.now() - inicio).total_seconds()
    log.info("=" * 60)
    log.info(f"FIM: duracao {duracao:.0f}s")
    log.info(f"  Total de linhas processadas: {total_geral['total_linhas']:,}")
    log.info(f"  Inseridas em fornecedores: {total_geral['inseridas']:,}")
    log.info(f"  Puladas (nao ATIVA): {total_geral['pulou_situacao']:,}")
    log.info(f"  Puladas (CNAE fora do interesse): {total_geral['pulou_cnae']:,}")
    log.info("=" * 60)
    conn.close()


if __name__ == "__main__":
    main()
