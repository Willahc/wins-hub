"""
Reconcilia municipios_rfb com municipios_ibge.
Atualiza empresas_receita.municipio_nome e .municipio_ibge.

Estrategia:
1. Atualiza empresas_receita.municipio_nome via JOIN com municipios_rfb
2. Reconcilia municipios_rfb -> municipios_ibge (por uf+nome normalizado sem acentos)
3. Atualiza empresas_receita.municipio_ibge via JOIN com municipios_rfb
"""
import os
import logging
import psycopg2

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Mapa de remocao de acentos (substitui unaccent extension)
ACENTOS_DE = "ÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖÙÚÛÜÝàáâãäåçèéêëìíîïñòóôõöùúûüý"
ACENTOS_PARA = "AAAAAACEEEEIIIINOOOOOUUUUYaaaaaaceeeeiiiinooooouuuuy"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger(__name__)

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # Etapa 1: atualiza municipio_nome em empresas_receita
            log.info("Etapa 1: atualizando empresas_receita.municipio_nome...")
            cur.execute("""
                UPDATE empresas_receita e
                SET municipio_nome = m.nome
                FROM municipios_rfb m
                WHERE e.municipio_rfb = m.codigo_rfb
                  AND e.municipio_nome IS NULL
            """)
            log.info(f"  {cur.rowcount:,} empresas atualizadas")
            conn.commit()

            # Etapa 2: reconcilia municipios_rfb com municipios_ibge
            # Usa a UF da empresa pra desambiguar municipios homonimos
            log.info("Etapa 2: reconciliando municipios_rfb -> municipios_ibge...")
            cur.execute(f"""
                WITH rfb_com_uf AS (
                    SELECT DISTINCT
                        e.municipio_rfb AS codigo_rfb,
                        upper(translate(e.municipio_nome, %s, %s)) AS nome_norm,
                        e.uf
                    FROM empresas_receita e
                    WHERE e.municipio_rfb IS NOT NULL
                      AND e.municipio_nome IS NOT NULL
                      AND e.uf IS NOT NULL
                )
                UPDATE municipios_rfb mrfb
                SET codigo_ibge = mibge.codigo_ibge
                FROM rfb_com_uf r
                JOIN municipios_ibge mibge
                  ON upper(translate(mibge.nome, %s, %s)) = r.nome_norm
                  AND mibge.uf = r.uf
                WHERE mrfb.codigo_rfb = r.codigo_rfb
                  AND mrfb.codigo_ibge IS NULL
            """, (ACENTOS_DE, ACENTOS_PARA, ACENTOS_DE, ACENTOS_PARA))
            log.info(f"  {cur.rowcount:,} municipios_rfb reconciliados")
            conn.commit()

            # Etapa 3: atualiza municipio_ibge em empresas_receita
            log.info("Etapa 3: atualizando empresas_receita.municipio_ibge...")
            cur.execute("""
                UPDATE empresas_receita e
                SET municipio_ibge = m.codigo_ibge
                FROM municipios_rfb m
                WHERE e.municipio_rfb = m.codigo_rfb
                  AND m.codigo_ibge IS NOT NULL
                  AND e.municipio_ibge IS NULL
            """)
            log.info(f"  {cur.rowcount:,} empresas atualizadas")
            conn.commit()

            # Estatisticas finais
            cur.execute("SELECT count(*) FROM empresas_receita")
            total = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM empresas_receita WHERE municipio_nome IS NOT NULL")
            com_nome = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM empresas_receita WHERE municipio_ibge IS NOT NULL")
            com_ibge = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM municipios_rfb")
            total_rfb = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM municipios_rfb WHERE codigo_ibge IS NOT NULL")
            rfb_com_ibge = cur.fetchone()[0]

            log.info(f"\nResumo final:")
            log.info(f"  Total empresas:               {total:,}")
            log.info(f"  Com municipio_nome:           {com_nome:,} ({100*com_nome/total:.1f}%)")
            log.info(f"  Com municipio_ibge:           {com_ibge:,} ({100*com_ibge/total:.1f}%)")
            log.info(f"  Total municipios_rfb:         {total_rfb:,}")
            log.info(f"  Municipios_rfb com IBGE:      {rfb_com_ibge:,} ({100*rfb_com_ibge/total_rfb:.1f}%)")

    except Exception as e:
        conn.rollback()
        log.exception(f"Erro: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
