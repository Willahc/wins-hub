"""Helper de conexao com Postgres. Auto-detect: dentro do container Docker
usa 'db' (Docker DNS); no host tenta 127.0.0.1; fallback DB_HOST env var."""
import os
import socket
from typing import Optional

_RESOLVED_HOST: Optional[str] = None


def _can_connect(host: str, port: int = 5432, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (socket.gaierror, ConnectionError, OSError):
        return False


def get_db_host() -> str:
    """Retorna primeiro host com Postgres respondendo. Cache por processo."""
    global _RESOLVED_HOST
    if _RESOLVED_HOST:
        return _RESOLVED_HOST
    port = int(os.getenv("DB_PORT", "5432"))
    explicit = os.getenv("DB_HOST")
    candidatos = []
    if explicit:
        candidatos.append(explicit)
    candidatos += ["db", "127.0.0.1"]
    for h in candidatos:
        if _can_connect(h, port):
            _RESOLVED_HOST = h
            return h
    _RESOLVED_HOST = "localhost"
    return _RESOLVED_HOST


def get_conn():
    import psycopg2
    return psycopg2.connect(
        host=get_db_host(),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
