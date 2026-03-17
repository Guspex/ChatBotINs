#!/usr/bin/env python3
# criar_banco.py
# ============================================================
# ORACULO/UDESC — Criação completa do banco do zero
#
# Conecta como superusuário (postgres) e:
#   1. Cria o usuário 'oraculo' (sem caracteres especiais)
#   2. Cria o banco 'oraculo_udesc'
#   3. Instala as extensões vector e unaccent
#   4. Cria todas as tabelas, índices e views
# ============================================================

import sys
import getpass

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Instale psycopg2: pip install psycopg2-binary")
    sys.exit(1)

# ── Configurações do novo banco ────────────────────────────────────────────────
NOVO_BANCO    = "oraculo_udesc"
NOVO_USUARIO  = "oraculo"
NOVA_SENHA    = "oraculo123"          # sem acentos ou caracteres especiais
HOST          = "localhost"
PORT          = 5432

# ── Schema completo ────────────────────────────────────────────────────────────
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Documentos normativos (INs, Resoluções, Portarias, etc.)
CREATE TABLE IF NOT EXISTS documentos (
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

-- Chunks vetorizados (dimensão 768 = nomic-embed-text)
CREATE TABLE IF NOT EXISTS chunks (
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
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks (documento_id);

-- Sessões de conversa
CREATE TABLE IF NOT EXISTS sessoes (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    servidor_id  TEXT NOT NULL,
    criada_em    TIMESTAMPTZ DEFAULT NOW(),
    encerrada_em TIMESTAMPTZ
);

-- Mensagens trocadas em cada sessão
CREATE TABLE IF NOT EXISTS mensagens (
    id                SERIAL PRIMARY KEY,
    sessao_id         UUID NOT NULL REFERENCES sessoes(id) ON DELETE CASCADE,
    role              TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    conteudo          TEXT NOT NULL,
    chunks_usados     INTEGER[],
    avaliacao         SMALLINT CHECK (avaliacao IN (-1, 1)),
    tempo_resposta_ms INTEGER,
    criado_em         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mensagens_sessao ON mensagens (sessao_id, criado_em);

-- Log de eventos (ingestão, revogações, erros)
CREATE TABLE IF NOT EXISTS log_atualizacoes (
    id           SERIAL PRIMARY KEY,
    documento_id INTEGER REFERENCES documentos(id),
    tipo_evento  TEXT NOT NULL,
    descricao    TEXT,
    criado_em    TIMESTAMPTZ DEFAULT NOW()
);

-- View: chunks com metadados do documento pai
CREATE OR REPLACE VIEW v_chunks AS
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

# ── Helpers ────────────────────────────────────────────────────────────────────

def conectar_postgres(usuario, senha, banco="postgres"):
    """Conecta ao PostgreSQL com os parâmetros dados (sem DSN string)."""
    return psycopg2.connect(
        host            = HOST,
        port            = PORT,
        dbname          = banco,
        user            = usuario,
        password        = senha,
        client_encoding = "utf8",
    )

def banco_existe(conn, nome):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (nome,))
    return cur.fetchone() is not None

def usuario_existe(conn, nome):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (nome,))
    return cur.fetchone() is not None

# ── Pipeline ────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("  ORACULO/UDESC — Criação do Banco de Dados")
    print("=" * 60)
    print(f"\n  Banco   : {NOVO_BANCO}")
    print(f"  Usuário : {NOVO_USUARIO}")
    print(f"  Senha   : {NOVA_SENHA}")
    print(f"  Host    : {HOST}:{PORT}\n")

    # Solicita credenciais do superusuário
    print("Informe as credenciais do superusuário do PostgreSQL")
    print("(usuário 'postgres' com a senha definida na instalação)\n")
    su_usuario = input("  Superusuário [postgres]: ").strip() or "postgres"
    su_senha   = getpass.getpass(f"  Senha do {su_usuario}: ")

    print()

    # ── 1. Conecta como superusuário ──────────────────────────────────────────
    try:
        conn_su = conectar_postgres(su_usuario, su_senha)
        conn_su.autocommit = True
        print("  ✓ Conectado ao PostgreSQL como superusuário")
    except Exception as e:
        print(f"  ✗ Falha ao conectar: {e}")
        print("\n  Verifique se o PostgreSQL está rodando e a senha está correta.")
        sys.exit(1)

    cur_su = conn_su.cursor()

    # ── 2. Cria usuário ───────────────────────────────────────────────────────
    if usuario_existe(conn_su, NOVO_USUARIO):
        print(f"  ✓ Usuário '{NOVO_USUARIO}' já existe — atualizando senha")
        cur_su.execute(
            f"ALTER USER {NOVO_USUARIO} WITH PASSWORD %s",
            (NOVA_SENHA,)
        )
    else:
        cur_su.execute(
            f"CREATE USER {NOVO_USUARIO} WITH PASSWORD %s",
            (NOVA_SENHA,)
        )
        print(f"  ✓ Usuário '{NOVO_USUARIO}' criado")

    # ── 3. Cria banco ─────────────────────────────────────────────────────────
    if banco_existe(conn_su, NOVO_BANCO):
        print(f"  ✓ Banco '{NOVO_BANCO}' já existe — usando existente")
    else:
        cur_su.execute(
            f'CREATE DATABASE {NOVO_BANCO} OWNER {NOVO_USUARIO} ENCODING \'UTF8\''
        )
        print(f"  ✓ Banco '{NOVO_BANCO}' criado")

    cur_su.execute(
        f"GRANT ALL PRIVILEGES ON DATABASE {NOVO_BANCO} TO {NOVO_USUARIO}"
    )
    print(f"  ✓ Privilégios concedidos")
    conn_su.close()

    # ── 4. Instala extensões e cria schema ────────────────────────────────────
    print(f"\n  Criando schema no banco '{NOVO_BANCO}'...")
    try:
        conn_db = conectar_postgres(su_usuario, su_senha, NOVO_BANCO)
        conn_db.autocommit = True
        cur_db = conn_db.cursor()

        # Garante que o usuário possa usar o schema public
        cur_db.execute(f"GRANT ALL ON SCHEMA public TO {NOVO_USUARIO}")

        cur_db.execute(SCHEMA)
        print("  ✓ Extensões instaladas: vector, unaccent")
        print("  ✓ Tabelas criadas: documentos, chunks, sessoes, mensagens, log_atualizacoes")
        print("  ✓ Índice HNSW criado: idx_chunks_embedding")
        print("  ✓ View criada: v_chunks")

        # Concede permissões nas tabelas ao usuário oraculo
        cur_db.execute(f"""
            GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {NOVO_USUARIO};
            GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {NOVO_USUARIO};
        """)
        print(f"  ✓ Permissões nas tabelas concedidas a '{NOVO_USUARIO}'")

        conn_db.close()

    except Exception as e:
        print(f"\n  ✗ Erro ao criar schema: {e}")
        print("  Dica: verifique se a extensão pgvector está instalada no PostgreSQL.")
        print("  Instalação: https://github.com/pgvector/pgvector")
        sys.exit(1)

    # ── 5. Testa com o novo usuário ───────────────────────────────────────────
    print(f"\n  Testando conexão com o usuário '{NOVO_USUARIO}'...")
    try:
        conn_test = conectar_postgres(NOVO_USUARIO, NOVA_SENHA, NOVO_BANCO)
        cur_test  = conn_test.cursor()
        cur_test.execute("SELECT COUNT(*) FROM documentos")
        cur_test.execute("SELECT extname FROM pg_extension WHERE extname='vector'")
        row = cur_test.fetchone()
        if not row:
            raise Exception("pgvector não encontrado após instalação")
        conn_test.close()
        print(f"  ✓ Conexão com '{NOVO_USUARIO}' funcionando")
        print(f"  ✓ pgvector ativo no banco\n")
    except Exception as e:
        print(f"  ✗ Teste falhou: {e}")
        sys.exit(1)

    # ── Resultado ─────────────────────────────────────────────────────────────
    print("=" * 60)
    print("  BANCO CRIADO COM SUCESSO!")
    print("=" * 60)
    print(f"""
  Atualize seu config.py com:

    name:     {NOVO_BANCO}
    user:     {NOVO_USUARIO}
    password: {NOVA_SENHA}

  Ou crie um arquivo .env na pasta do projeto:

    PGHOST=localhost
    PGPORT=5432
    PGDATABASE={NOVO_BANCO}
    PGUSER={NOVO_USUARIO}
    PGPASSWORD={NOVA_SENHA}

  Próximos passos:
    1. python scraper_udesc.py          # baixa os PDFs das INs
    2. python pipeline_vetorizacao.py   # vetoriza e indexa
    3. python test.py                   # testa tudo
    4. python chatbot.py                # inicia o chat
""")

if __name__ == "__main__":
    main()