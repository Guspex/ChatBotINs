#!/usr/bin/env python3
# resetabanco.py
# ============================================================
# ORACULO/UDESC — Apaga e recria o banco do zero
# ============================================================

import os
import sys
import psycopg2

DB_CONFIG = {
    "host":     os.getenv("PGHOST",     "localhost"),
    "port":     os.getenv("PGPORT",     "5432"),
    "dbname":   os.getenv("PGDATABASE", "oraculo_udesc"),
    "user":     os.getenv("PGUSER",     "oraculo"),
    "password": os.getenv("PGPASSWORD", "oraculo123"),
}

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS log_atualizacoes CASCADE;
DROP TABLE IF EXISTS mensagens       CASCADE;
DROP TABLE IF EXISTS sessoes         CASCADE;
DROP TABLE IF EXISTS chunks          CASCADE;
DROP TABLE IF EXISTS documentos      CASCADE;

-- ── Documentos normativos ────────────────────────────────────
CREATE TABLE documentos (
    id              SERIAL PRIMARY KEY,
    titulo          TEXT NOT NULL,
    numero          TEXT NOT NULL DEFAULT '',
    tipo            TEXT NOT NULL DEFAULT 'IN',
    pro_reitoria    TEXT NOT NULL DEFAULT 'UDESC',
    orgao_emissor   TEXT NOT NULL DEFAULT 'UDESC',
    data_publicacao DATE,
    revogado        BOOLEAN DEFAULT FALSE,
    revoga_in       TEXT DEFAULT '',
    arquivo_origem  TEXT,
    hash_arquivo    TEXT UNIQUE,
    metadados       JSONB DEFAULT '{}',
    criado_em       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Chunks vetorizados ───────────────────────────────────────
CREATE TABLE chunks (
    id              SERIAL PRIMARY KEY,
    documento_id    INTEGER NOT NULL REFERENCES documentos(id) ON DELETE CASCADE,
    sequencia       INTEGER NOT NULL,
    conteudo        TEXT NOT NULL,
    embedding       VECTOR(768),
    pagina_inicio   INTEGER DEFAULT 0,
    pagina_fim      INTEGER DEFAULT 0,
    secao           TEXT DEFAULT '',
    tokens_aprox    INTEGER DEFAULT 0,
    criado_em       TIMESTAMPTZ DEFAULT NOW()
);

-- Índice HNSW para busca cosine
CREATE INDEX idx_chunks_embedding
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_chunks_doc ON chunks (documento_id);

-- ── Histórico de conversas ───────────────────────────────────
CREATE TABLE sessoes (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    servidor_id  TEXT NOT NULL,
    criada_em    TIMESTAMPTZ DEFAULT NOW(),
    encerrada_em TIMESTAMPTZ
);

CREATE TABLE mensagens (
    id                SERIAL PRIMARY KEY,
    sessao_id         UUID NOT NULL REFERENCES sessoes(id) ON DELETE CASCADE,
    role              TEXT NOT NULL CHECK (role IN ('user','assistant')),
    conteudo          TEXT NOT NULL,
    chunks_usados     INTEGER[],
    avaliacao         SMALLINT CHECK (avaliacao IN (-1, 1)),
    tempo_resposta_ms INTEGER,                 -- FIX: era "tempo_ms" — alinhado com chatbot.py
    criado_em         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_mensagens_sessao ON mensagens (sessao_id, criado_em);

-- ── Log ──────────────────────────────────────────────────────
CREATE TABLE log_atualizacoes (
    id           SERIAL PRIMARY KEY,
    documento_id INTEGER REFERENCES documentos(id),
    tipo_evento  TEXT NOT NULL,
    descricao    TEXT,
    criado_em    TIMESTAMPTZ DEFAULT NOW()
);

-- ── View de consulta ─────────────────────────────────────────
CREATE VIEW v_chunks AS
SELECT
    c.id, c.documento_id, c.sequencia,
    c.conteudo, c.embedding,
    c.pagina_inicio, c.pagina_fim, c.secao,
    d.titulo, d.numero, d.tipo,
    d.pro_reitoria, d.orgao_emissor,
    d.data_publicacao, d.revogado, d.revoga_in
FROM chunks c
JOIN documentos d ON d.id = c.documento_id
WHERE d.revogado = FALSE;
"""

def main():
    print("\n" + "=" * 60)
    print("  ORACULO/UDESC — Reset do Banco de Dados")
    print("=" * 60)
    print("\n  ⚠  ATENÇÃO: todos os dados serão apagados!")
    resp = input("  Confirma? (digite SIM para continuar): ").strip()
    if resp != "SIM":
        print("  Cancelado.")
        sys.exit(0)

    try:
        conn = psycopg2.connect(
            host            = DB_CONFIG["host"],
            port            = DB_CONFIG["port"],
            dbname          = DB_CONFIG["dbname"],
            user            = DB_CONFIG["user"],
            password        = DB_CONFIG["password"],
            client_encoding = "utf8",
        )
        conn.autocommit = True
        cur = conn.cursor()
        print("\n  Recriando schema...")
        cur.execute(SCHEMA)
        print("  ✓ Schema criado com sucesso!")
        print(f"\n  Tabelas criadas: documentos, chunks, sessoes, mensagens, log_atualizacoes")
        print(f"  View criada    : v_chunks")
        print(f"  Índice HNSW    : idx_chunks_embedding\n")
        conn.close()
    except Exception as e:
        print(f"\n  ✗ Erro: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()