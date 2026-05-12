"""
Importa tabela Municipios.zip da Receita Federal.
Cria/atualiza tabela municipios_rfb (codigo_rfb -> nome).
"""
import os
import csv
import io
import zipfile
import logging
from pathlib import Path
import requests
import psycopg2
from psycopg2.extras import execute_values

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

URL = "https://dados-abertos-rf-cnpj.casadosdados.com.br/arquivos/2026-04-12/Municipios.zip"
DEST = Path("/tmp/municipios_rfb.zip")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger(__name__)

    log.info(f"Baixando {URL}")
    with requests.get(URL, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(DEST, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
    log.info(f"  {DEST.stat().st_size} bytes")

    municipios = []
    with zipfile.ZipFile(DEST) as zf:
        nome_csv = zf.namelist()[0]
        with zf.open(nome_csv) as raw:
            text = io.TextIOWrapper(raw, encoding="latin-1", newline="")
            reader = csv.reader(text, delimiter=";", quotechar='"')
            for row in reader:
                if len(row) >= 2:
                    codigo = row[0].strip().strip('"')
                    nome = row[1].strip().strip('"')
                    if codigo and nome:
                        municipios.append((codigo, nome))

    log.info(f"Lidos: {len(municipios)} municipios RFB")

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS municipios_rfb (
                    codigo_rfb TEXT PRIMARY KEY,
                    nome TEXT NOT NULL,
                    codigo_ibge INTEGER
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_municipios_rfb_ibge ON municipios_rfb (codigo_ibge)")

            execute_values(cur, """
                INSERT INTO municipios_rfb (codigo_rfb, nome)
                VALUES %s
                ON CONFLICT (codigo_rfb) DO UPDATE SET nome = EXCLUDED.nome
            """, municipios)
            log.info(f"municipios_rfb populada: {len(municipios)} linhas")

        conn.commit()
        log.info("OK")
    finally:
        conn.close()
        DEST.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
