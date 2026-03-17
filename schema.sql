-- ============================================================
-- ORION/UDESC — Schema do banco de dados
-- Requer: PostgreSQL >= 15 + extensão pgvector
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- ------------------------------------------------------------
-- Documentos normativos (INs, Resoluções, Portarias, etc.)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documentos (
    id              SERIAL PRIMARY KEY,
    titulo          TEXT NOT NULL,
    numero          TEXT,                          -- Ex: "IN 001/2023"
    tipo            TEXT NOT NULL,                 -- IN, RESOLUCAO, PORTARIA, ESTATUTO...
    orgao_emissor   TEXT,                          -- Ex: "UDESC", "CGE/SC", "SEGES"
    data_publicacao DATE,
    data_vigencia   DATE,
    revogado        BOOLEAN DEFAULT FALSE,
    revogado_por    INTEGER REFERENCES documentos(id),
    arquivo_origem  TEXT,                          -- nome do arquivo PDF original
    hash_arquivo    TEXT UNIQUE,                   -- SHA256 para evitar duplicatas
    metadados       JSONB DEFAULT '{}',
    criado_em       TIMESTAMPTZ DEFAULT NOW(),
    atualizado_em   TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Chunks vetorizados das INs
-- Dimensão 768 = nomic-embed-text
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    id              SERIAL PRIMARY KEY,
    documento_id    INTEGER NOT NULL REFERENCES documentos(id) ON DELETE CASCADE,
    sequencia       INTEGER NOT NULL,              -- ordem do chunk no documento
    conteudo        TEXT NOT NULL,                 -- texto do chunk
    embedding       VECTOR(768),                   -- vetor nomic-embed-text
    pagina_inicio   INTEGER,
    pagina_fim      INTEGER,
    secao           TEXT,                          -- Ex: "Art. 3º", "Capítulo II"
    tokens_aprox    INTEGER,
    criado_em       TIMESTAMPTZ DEFAULT NOW()
);

-- Índice HNSW para busca por similaridade cosine (melhor performance)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Índice para filtrar por documento
CREATE INDEX IF NOT EXISTS idx_chunks_documento
    ON chunks (documento_id);

-- ------------------------------------------------------------
-- Histórico de conversas
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessoes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    servidor_id     TEXT NOT NULL,                 -- matrícula ou login UDESC
    criada_em       TIMESTAMPTZ DEFAULT NOW(),
    encerrada_em    TIMESTAMPTZ,
    metadados       JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS mensagens (
    id              SERIAL PRIMARY KEY,
    sessao_id       UUID NOT NULL REFERENCES sessoes(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    conteudo        TEXT NOT NULL,
    chunks_usados   INTEGER[],                     -- IDs dos chunks que embasaram a resposta
    avaliacao       SMALLINT CHECK (avaliacao IN (-1, 1)),   -- 👍/👎
    tempo_resposta_ms INTEGER,
    criado_em       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mensagens_sessao
    ON mensagens (sessao_id, criado_em);

-- ------------------------------------------------------------
-- Log de atualizações normativas
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS log_atualizacoes (
    id              SERIAL PRIMARY KEY,
    documento_id    INTEGER REFERENCES documentos(id),
    tipo_evento     TEXT NOT NULL,   -- INGESTED, REVOGADO, ATUALIZADO, ERRO
    descricao       TEXT,
    criado_em       TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- View útil: chunks com metadados do documento pai
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_chunks_completos AS
SELECT
    c.id,
    c.documento_id,
    c.sequencia,
    c.conteudo,
    c.embedding,
    c.pagina_inicio,
    c.pagina_fim,
    c.secao,
    d.titulo        AS documento_titulo,
    d.numero        AS documento_numero,
    d.tipo          AS documento_tipo,
    d.orgao_emissor,
    d.data_publicacao,
    d.revogado
FROM chunks c
JOIN documentos d ON d.id = c.documento_id
WHERE d.revogado = FALSE;
