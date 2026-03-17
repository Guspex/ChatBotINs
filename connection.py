# connection.py
# ============================================================
# ORACULO/UDESC — Gerenciador de conexão PostgreSQL
# Antes em: database/connection.py
# ============================================================

import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from config import cfg

logger = logging.getLogger(__name__)


def _get_connection():
    """
    Cria e retorna uma nova conexão com o banco.
    Usa parâmetros individuais em vez de string DSN para evitar
    UnicodeDecodeError no Windows com senhas que têm caracteres especiais.
    """
    conn = psycopg2.connect(
        host=cfg.db.host,
        port=cfg.db.port,
        dbname=cfg.db.name,
        user=cfg.db.user,
        password=cfg.db.password,
        client_encoding="utf8",
    )
    conn.autocommit = False
    return conn


@contextmanager
def get_cursor(commit: bool = False):
    """
    Context manager que entrega um cursor RealDictCursor e
    opcionalmente faz commit ao sair.

    Uso:
        with get_cursor(commit=True) as cur:
            cur.execute("INSERT ...")
    """
    conn = _get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()