"""
Service de consulta CNPJ via BrasilAPI com cache.

Fluxo:
1. Verifica cache em cache_brasilapi (TTL 30 dias)
2. Se cache vazio/expirado, consulta BrasilAPI
3. Salva resposta em cache + insere/atualiza fornecedores
4. Retorna dict normalizado pronto pra usar
"""
import os
import json
import logging
import re
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

BRASILAPI_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
TIMEOUT = 15


def _normalizar_cnpj(cnpj: str) -> str:
    """Remove tudo que nao for digito. '12.345.678/0001-99' -> '12345678000199'"""
    return re.sub(r'\D', '', cnpj or '')


def _buscar_no_cache(conn, cnpj: str):
    """Retorna payload do cache se valido (nao expirado), senao None."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT payload FROM cache_brasilapi
            WHERE cnpj = %s AND expira_em > now()
        """, (cnpj,))
        row = cur.fetchone()
        return row['payload'] if row else None


def _salvar_no_cache(conn, cnpj: str, payload: dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO cache_brasilapi (cnpj, payload, consultado_em, expira_em)
            VALUES (%s, %s, now(), now() + INTERVAL '30 days')
            ON CONFLICT (cnpj) DO UPDATE
            SET payload = EXCLUDED.payload,
                consultado_em = EXCLUDED.consultado_em,
                expira_em = EXCLUDED.expira_em
        """, (cnpj, json.dumps(payload)))


def _upsert_empresa_receita(conn, payload: dict):
    """Insere ou atualiza empresa em fornecedores a partir do payload BrasilAPI."""
    cnpj = _normalizar_cnpj(payload.get('cnpj', ''))
    if not cnpj:
        return

    cnaes_secundarios = [
        str(c.get('codigo', '')).replace('.', '').replace('-', '').replace('/', '')
        for c in (payload.get('cnaes_secundarios') or [])
        if c.get('codigo')
    ]
    cnae_principal = str(payload.get('cnae_fiscal', '') or '')

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fornecedores (
                cnpj, razao_social, nome_fantasia,
                cnae_principal, cnae_secundarios,
                logradouro, numero, complemento, bairro, cep,
                municipio_ibge, municipio_nome, uf,
                telefone_1, telefone_2, email,
                situacao, porte, capital_social, data_abertura,
                atualizado_em
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                now()
            )
            ON CONFLICT (cnpj) DO UPDATE SET
                razao_social     = EXCLUDED.razao_social,
                nome_fantasia    = EXCLUDED.nome_fantasia,
                cnae_principal   = EXCLUDED.cnae_principal,
                cnae_secundarios = EXCLUDED.cnae_secundarios,
                logradouro       = EXCLUDED.logradouro,
                numero           = EXCLUDED.numero,
                complemento      = EXCLUDED.complemento,
                bairro           = EXCLUDED.bairro,
                cep              = EXCLUDED.cep,
                municipio_ibge   = EXCLUDED.municipio_ibge,
                municipio_nome   = EXCLUDED.municipio_nome,
                uf               = EXCLUDED.uf,
                telefone_1       = EXCLUDED.telefone_1,
                telefone_2       = EXCLUDED.telefone_2,
                email            = EXCLUDED.email,
                situacao         = EXCLUDED.situacao,
                porte            = EXCLUDED.porte,
                capital_social   = EXCLUDED.capital_social,
                data_abertura    = EXCLUDED.data_abertura,
                atualizado_em    = now()
        """, (
            cnpj,
            payload.get('razao_social'),
            payload.get('nome_fantasia'),
            cnae_principal,
            cnaes_secundarios or None,
            payload.get('logradouro'),
            payload.get('numero'),
            payload.get('complemento'),
            payload.get('bairro'),
            (payload.get('cep') or '').replace('-', '') or None,
            payload.get('codigo_municipio_ibge'),
            payload.get('municipio'),
            payload.get('uf'),
            payload.get('ddd_telefone_1'),
            payload.get('ddd_telefone_2'),
            payload.get('email'),
            payload.get('descricao_situacao_cadastral'),
            payload.get('porte'),
            payload.get('capital_social'),
            payload.get('data_inicio_atividade'),
        ))


def consultar_cnpj(cnpj: str, force_refresh: bool = False) -> dict | None:
    """
    Consulta CNPJ via BrasilAPI (com cache).

    Args:
        cnpj: CNPJ com ou sem formatacao
        force_refresh: ignora cache e busca direto na API

    Returns:
        dict com dados da empresa ou None se nao encontrado
    """
    cnpj = _normalizar_cnpj(cnpj)
    if len(cnpj) != 14:
        log.warning(f"CNPJ invalido: {cnpj}")
        return None

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    try:
        # 1. tenta cache
        if not force_refresh:
            cached = _buscar_no_cache(conn, cnpj)
            if cached:
                log.info(f"CNPJ {cnpj} encontrado no cache")
                return cached

        # 2. consulta API
        log.info(f"Consultando BrasilAPI: {cnpj}")
        try:
            r = requests.get(BRASILAPI_URL.format(cnpj=cnpj), timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            log.error(f"Erro de rede consultando {cnpj}: {e}")
            return None

        if r.status_code == 404:
            log.warning(f"CNPJ {cnpj} nao encontrado na BrasilAPI")
            return None
        if r.status_code != 200:
            log.error(f"BrasilAPI retornou {r.status_code} pra {cnpj}: {r.text[:200]}")
            return None

        payload = r.json()

        # 3. salva cache + base estruturada (mesma transacao)
        _salvar_no_cache(conn, cnpj, payload)
        _upsert_empresa_receita(conn, payload)
        conn.commit()

        return payload

    except Exception as e:
        conn.rollback()
        log.exception(f"Erro inesperado consultando {cnpj}: {e}")
        return None
    finally:
        conn.close()


# Permite executar standalone: python -m app.services.brasilapi 12345678000199
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    if len(sys.argv) < 2:
        print("Uso: python brasilapi.py <CNPJ>")
        sys.exit(1)

    cnpj_teste = sys.argv[1]
    print(f"Consultando {cnpj_teste}...")
    resultado = consultar_cnpj(cnpj_teste)

    if resultado:
        print(f"OK")
        print(f"  Razao social: {resultado.get('razao_social')}")
        print(f"  Nome fantasia: {resultado.get('nome_fantasia')}")
        print(f"  Municipio: {resultado.get('municipio')}/{resultado.get('uf')}")
        print(f"  CNAE principal: {resultado.get('cnae_fiscal')} - {resultado.get('cnae_fiscal_descricao')}")
        print(f"  Email: {resultado.get('email')}")
        print(f"  Telefone: {resultado.get('ddd_telefone_1')}")
        print(f"  Situacao: {resultado.get('descricao_situacao_cadastral')}")
    else:
        print("Nao encontrado ou erro")
