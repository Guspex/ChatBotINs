# ORACULO_udesc/config.py
# ============================================================
# Configurações centrais do ORACULO/UDESC
# Ajuste as variáveis de ambiente ou edite diretamente aqui.
# ============================================================

import os
from dataclasses import dataclass, field


@dataclass
class DatabaseConfig:
    host: str     = os.getenv("PGHOST",     "localhost")
    port: int     = int(os.getenv("PGPORT", "5432"))
    name: str     = os.getenv("PGDATABASE", "oraculo_udesc")
    user: str     = os.getenv("PGUSER",     "oraculo")
    password: str = os.getenv("PGPASSWORD", "oraculo123")

    @property
    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} "
            f"dbname={self.name} user={self.user} "
            f"password={self.password}"
        )


@dataclass
class OllamaConfig:
    base_url: str        = os.getenv("OLLAMA_URL", "http://localhost:11434")
    model_chat: str      = "aya-expanse:8b"   # SLM de geração de respostas
    model_embed: str     = "nomic-embed-text"  # modelo de embeddings
    embed_dim: int       = 768                 # dimensão do nomic-embed-text
    temperature: float   = 0.1                # baixo: respostas determinísticas/factuais
    num_ctx: int         = 4096               # contexto do AYA
    timeout: int         = 120                # segundos


@dataclass
class ChunkingConfig:
    chunk_size: int      = 512      # tokens aproximados por chunk
    chunk_overlap: int   = 80       # overlap entre chunks consecutivos
    min_chunk_size: int  = 50       # descarta chunks muito pequenos (ruído)


@dataclass
class RetrievalConfig:
    top_k: int           = 5        # número de chunks recuperados por query
    similarity_threshold: float = 0.35  # cosine similarity mínima (0-1)


@dataclass
class AppConfig:
    db: DatabaseConfig       = field(default_factory=DatabaseConfig)
    ollama: OllamaConfig     = field(default_factory=OllamaConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)

    # Diretório onde os PDFs das INs são depositados para ingestão
    pasta_ingestion: str = os.getenv("ORACULO_INGESTION_DIR", "./ingestion/pdfs")
    log_level: str       = os.getenv("ORACULO_LOG_LEVEL", "INFO")


# Instância global — importe de qualquer módulo
cfg = AppConfig()