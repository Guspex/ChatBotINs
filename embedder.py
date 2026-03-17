# embedder.py
# ============================================================
# ORACULO/UDESC — Geração de embeddings via Ollama
# Antes em: ingestion/embedder.py
# ============================================================

import logging
import time
import unicodedata
from typing import List

import httpx

from config import cfg

logger = logging.getLogger(__name__)

_EMBED_TIMEOUT = 60


def _sanitizar(texto: str) -> str:
    """Remove caracteres de controle e normaliza unicode."""
    texto = unicodedata.normalize("NFC", texto)
    texto = "".join(
        c for c in texto
        if unicodedata.category(c) not in ("Cc", "Cs") or c in ("\n", "\t")
    )
    return texto[:2000].strip()


def gerar_embedding(texto: str, tentativas: int = 3) -> List[float]:
    """
    Gera embedding de um texto usando nomic-embed-text via Ollama.
    Tenta a API moderna (/api/embed) e cai no legado (/api/embeddings).
    Retorna lista de floats com dimensão cfg.ollama.embed_dim (768).
    """
    texto = _sanitizar(texto)
    if not texto:
        logger.warning("Texto vazio passado para gerar_embedding — retornando zeros.")
        return [0.0] * cfg.ollama.embed_dim

    url_base = cfg.ollama.base_url
    model    = cfg.ollama.model_embed
    dim      = cfg.ollama.embed_dim

    for tentativa in range(1, tentativas + 1):
        try:
            # API moderna (Ollama >= 0.1.26)
            r = httpx.post(
                f"{url_base}/api/embed",
                json={"model": model, "input": texto},
                timeout=_EMBED_TIMEOUT
            )
            if r.status_code == 200:
                emb = r.json().get("embeddings", [[]])[0]
                if emb and len(emb) == dim:
                    return emb

            # Fallback: API legada
            r = httpx.post(
                f"{url_base}/api/embeddings",
                json={"model": model, "prompt": texto},
                timeout=_EMBED_TIMEOUT
            )
            r.raise_for_status()
            emb = r.json().get("embedding", [])
            if emb and len(emb) == dim:
                return emb

            raise ValueError(f"Embedding retornado com dimensão inesperada: {len(emb)} (esperado {dim})")

        except Exception as exc:
            logger.warning("Embedding tentativa %d/%d falhou: %s", tentativa, tentativas, exc)
            if tentativa < tentativas:
                time.sleep(2 ** tentativa)

    raise RuntimeError(f"Não foi possível gerar embedding após {tentativas} tentativas.")
