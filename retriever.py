# retriever.py
# ============================================================
# ORACULO/UDESC — Busca semântica por similaridade cosine no pgvector
# ============================================================

import logging
from dataclasses import dataclass
from typing import List, Optional

from config import cfg
from connection import get_cursor
from embedder import gerar_embedding

logger = logging.getLogger(__name__)


@dataclass
class ResultadoBusca:
    chunk_id: int
    documento_id: int
    documento_titulo: str
    documento_numero: str
    documento_tipo: str
    orgao_emissor: str
    data_publicacao: Optional[str]
    conteudo: str
    secao: str
    pagina_inicio: int
    pagina_fim: int
    similaridade: float  # 0.0 – 1.0 (cosine)

    def __str__(self) -> str:
        fonte = f"{self.documento_tipo} {self.documento_numero}".strip()
        if not self.documento_numero:
            fonte = self.documento_titulo
        return (
            f"[{fonte} | {self.orgao_emissor} | "
            f"p.{self.pagina_inicio}–{self.pagina_fim} | "
            f"sim={self.similaridade:.2f}]\n{self.conteudo}"
        )


def buscar(
    query: str,
    top_k: Optional[int] = None,
    threshold: Optional[float] = None,
    filtro_tipo: Optional[str] = None,
    filtro_orgao: Optional[str] = None,
) -> List[ResultadoBusca]:
    """
    Busca semântica principal.

    1. Gera embedding da query.
    2. Consulta pgvector com similaridade cosine.
    3. Filtra por threshold e metadados opcionais.
    4. Retorna os top-k resultados mais relevantes.
    """
    top_k     = top_k     or cfg.retrieval.top_k
    threshold = threshold or cfg.retrieval.similarity_threshold

    logger.debug("Buscando: '%s' (top_k=%d, threshold=%.2f)", query, top_k, threshold)

    # 1. Embedding da query
    query_embedding = gerar_embedding(query)
    embedding_str   = "[" + ",".join(str(x) for x in query_embedding) + "]"

    # 2. Monta filtros opcionais
    filtros_sql = ["d.revogado = FALSE"]
    params: list = [embedding_str, embedding_str, top_k * 3]

    if filtro_tipo:
        filtros_sql.append("d.tipo = %s")
        params.append(filtro_tipo.upper())

    if filtro_orgao:
        filtros_sql.append("d.orgao_emissor ILIKE %s")
        params.append(f"%{filtro_orgao}%")

    where_clause = " AND ".join(filtros_sql)

    # 3. Query com operador <=> (cosine distance; menor = mais similar)
    sql = f"""
        SELECT
            c.id                                AS chunk_id,
            c.documento_id,
            c.conteudo,
            c.secao,
            c.pagina_inicio,
            c.pagina_fim,
            d.titulo                            AS documento_titulo,
            d.numero                            AS documento_numero,
            d.tipo                              AS documento_tipo,
            d.orgao_emissor,
            d.data_publicacao::TEXT             AS data_publicacao,
            1 - (c.embedding <=> %s::vector)    AS similaridade
        FROM chunks c
        JOIN documentos d ON d.id = c.documento_id
        WHERE {where_clause}
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
    """

    with get_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    # 4. Filtra por threshold e limita ao top_k final
    resultados = []
    for row in rows:
        sim = float(row["similaridade"])
        if sim >= threshold:
            resultados.append(ResultadoBusca(
                chunk_id         = row["chunk_id"],
                documento_id     = row["documento_id"],
                documento_titulo = row["documento_titulo"],
                documento_numero = row["documento_numero"] or "",
                documento_tipo   = row["documento_tipo"],
                orgao_emissor    = row["orgao_emissor"] or "",
                data_publicacao  = row["data_publicacao"],
                conteudo         = row["conteudo"],
                secao            = row["secao"] or "",
                pagina_inicio    = row["pagina_inicio"] or 0,
                pagina_fim       = row["pagina_fim"] or 0,
                similaridade     = sim,
            ))
        if len(resultados) >= top_k:
            break

    logger.info(
        "Busca retornou %d resultado(s) com similaridade >= %.2f.",
        len(resultados), threshold
    )
    return resultados


def montar_contexto(resultados: List[ResultadoBusca]) -> str:
    """
    Formata os chunks recuperados em bloco de contexto para o prompt do AYA.
    Inclui a referência normativa de cada trecho.
    """
    if not resultados:
        return ""

    partes = []
    for i, r in enumerate(resultados, start=1):
        fonte    = f"{r.documento_tipo} {r.documento_numero}".strip() or r.documento_titulo
        cabecalho = f"[FONTE {i}: {fonte} — {r.orgao_emissor}]"
        if r.secao:
            cabecalho += f" — {r.secao}"
        partes.append(f"{cabecalho}\n{r.conteudo}")

    return "\n\n---\n\n".join(partes)
