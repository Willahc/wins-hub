import os, sys, csv, io
import psycopg2
from psycopg2.extras import execute_values
import requests

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

GH_MUN = "https://raw.githubusercontent.com/kelvins/municipios-brasileiros/main/csv/municipios.csv"
GH_EST = "https://raw.githubusercontent.com/kelvins/municipios-brasileiros/main/csv/estados.csv"

REGIOES = {
    "AC":"Norte","AM":"Norte","AP":"Norte","PA":"Norte","RO":"Norte","RR":"Norte","TO":"Norte",
    "AL":"Nordeste","BA":"Nordeste","CE":"Nordeste","MA":"Nordeste","PB":"Nordeste",
    "PE":"Nordeste","PI":"Nordeste","RN":"Nordeste","SE":"Nordeste",
    "DF":"Centro-Oeste","GO":"Centro-Oeste","MS":"Centro-Oeste","MT":"Centro-Oeste",
    "ES":"Sudeste","MG":"Sudeste","RJ":"Sudeste","SP":"Sudeste",
    "PR":"Sul","RS":"Sul","SC":"Sul",
}

UF_NOMES = {
    "AC":"Acre","AL":"Alagoas","AM":"Amazonas","AP":"Amapa","BA":"Bahia","CE":"Ceara",
    "DF":"Distrito Federal","ES":"Espirito Santo","GO":"Goias","MA":"Maranhao",
    "MG":"Minas Gerais","MS":"Mato Grosso do Sul","MT":"Mato Grosso","PA":"Para",
    "PB":"Paraiba","PE":"Pernambuco","PI":"Piaui","PR":"Parana","RJ":"Rio de Janeiro",
    "RN":"Rio Grande do Norte","RO":"Rondonia","RR":"Roraima","RS":"Rio Grande do Sul",
    "SC":"Santa Catarina","SE":"Sergipe","SP":"Sao Paulo","TO":"Tocantins",
}

SQL_UPSERT = (
    "INSERT INTO municipios_ibge "
    "(codigo_ibge,nome,uf,uf_nome,regiao,latitude,longitude) VALUES %s "
    "ON CONFLICT (codigo_ibge) DO UPDATE SET "
    "nome=EXCLUDED.nome, uf=EXCLUDED.uf, uf_nome=EXCLUDED.uf_nome, "
    "regiao=EXCLUDED.regiao, latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude"
)

def baixar(url):
    print(f"Baixando {url.split('/')[-1]}...", flush=True)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content  # bytes, vamos decodificar com utf-8-sig pra remover BOM

def parse_csv(raw_bytes):
    texto = raw_bytes.decode("utf-8-sig")  # <-- remove BOM se existir
    reader = csv.DictReader(io.StringIO(texto))
    # limpa tambem espacos em branco nos nomes de colunas por seguranca
    reader.fieldnames = [f.strip() for f in reader.fieldnames]
    return list(reader)

def main():
    try:
        estados_rows = parse_csv(baixar(GH_EST))
        municipios_rows = parse_csv(baixar(GH_MUN))
    except Exception as e:
        print(f"Falha no download: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Estados: {len(estados_rows)} | Municipios: {len(municipios_rows)}")
    uf_map = {r["codigo_uf"]: r["uf"] for r in estados_rows}

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    inseridos = 0

    try:
        with conn.cursor() as cur:
            batch = []
            for row in municipios_rows:
                uf = uf_map.get(row["codigo_uf"])
                if not uf:
                    continue
                batch.append((
                    int(row["codigo_ibge"]),
                    row["nome"],
                    uf,
                    UF_NOMES.get(uf),
                    REGIOES.get(uf),
                    float(row["latitude"]) if row["latitude"] else None,
                    float(row["longitude"]) if row["longitude"] else None,
                ))
                if len(batch) >= 500:
                    execute_values(cur, SQL_UPSERT, batch)
                    inseridos += len(batch)
                    print(f"   {inseridos}", flush=True)
                    batch = []
            if batch:
                execute_values(cur, SQL_UPSERT, batch)
                inseridos += len(batch)
        conn.commit()
        print(f"OK: {inseridos} municipios inseridos/atualizados")
    except Exception as e:
        conn.rollback()
        print(f"Erro: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
