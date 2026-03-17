# ingestor.py
# ============================================================
# ORACULO/UDESC — Pipeline de ingestão: PDF → texto → chunks → embedding → pgvector
# ============================================================

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional

import pdfplumber

from config import cfg
from connection import get_cursor
from embedder import gerar_embedding

logger = logging.getLogger(__name__)


# ── Estruturas de dados ────────────────────────────────────────────────────────

@dataclass
class MetadadosDocumento:
    titulo: str
    numero: str                         = ""
    tipo: str                           = "IN"
    orgao_emissor: str                  = "UDESC"
    data_publicacao: Optional[date]     = None
    data_vigencia: Optional[date]       = None


@dataclass
class Chunk:
    conteudo: str
    sequencia: int
    pagina_inicio: int
    pagina_fim: int
    secao: str = ""

    @property
    def tokens_aprox(self) -> int:
        return len(self.conteudo) // 4


# ── Extração de texto do PDF ───────────────────────────────────────────────────

def extrair_texto_pdf(caminho_pdf: Path) -> List[dict]:
    """
    Extrai texto página a página usando pdfplumber.
    Retorna lista de {'pagina': int, 'texto': str}.
    """
    paginas = []
    with pdfplumber.open(caminho_pdf) as pdf:
        for num, pagina in enumerate(pdf.pages, start=1):
            texto = pagina.extract_text(x_tolerance=3, y_tolerance=3) or ""
            texto = _limpar_texto(texto)
            if texto.strip():
                paginas.append({"pagina": num, "texto": texto})

    logger.info("PDF '%s': %d páginas com texto extraído.", caminho_pdf.name, len(paginas))
    return paginas


def _limpar_texto(texto: str) -> str:
    """Remove artefatos comuns de PDFs de órgãos públicos."""
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    texto = re.sub(r" +([.,;:])", r"\1", texto)
    texto = re.sub(r" {2,}", " ", texto)
    texto = re.sub(r"[Pp]ágina\s+\d+\s+de\s+\d+", "", texto)
    return texto.strip()


# ── Detecção de seções normativas ─────────────────────────────────────────────

_PADROES_SECAO = re.compile(
    r"^(Art(?:igo)?\s*\.?\s*\d+|"
    r"Capítulo\s+[IVXLC]+|"
    r"Seção\s+[IVXLC]+|"
    r"§\s*\d+|"
    r"CAPÍTULO|SEÇÃO|TÍTULO|ANEXO)",
    re.IGNORECASE | re.MULTILINE
)

def detectar_secao(texto: str) -> str:
    """Tenta identificar o identificador de seção no início de um chunk."""
    match = _PADROES_SECAO.search(texto[:200])
    return match.group(0).strip() if match else ""


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunkar_texto(paginas: List[dict]) -> List[Chunk]:
    """
    Estratégia de chunking em dois níveis:
    1. Preferencial: divide nos limites de artigos/seções detectados
    2. Fallback: divide por tamanho com overlap
    """
    chunks: List[Chunk] = []
    buffer = ""
    pagina_inicio = 1
    pagina_atual  = 1
    sequencia     = 0

    max_chars     = cfg.chunking.chunk_size * 4
    overlap_chars = cfg.chunking.chunk_overlap * 4

    for item in paginas:
        pagina_atual = item["pagina"]
        paragrafos   = item["texto"].split("\n\n")

        for paragrafo in paragrafos:
            paragrafo = paragrafo.strip()
            if not paragrafo:
                continue

            e_nova_secao = bool(_PADROES_SECAO.match(paragrafo))

            if e_nova_secao and len(buffer) >= cfg.chunking.min_chunk_size * 4:
                chunk = _criar_chunk(buffer, sequencia, pagina_inicio, pagina_atual)
                if chunk:
                    chunks.append(chunk)
                    sequencia += 1
                buffer        = buffer[-overlap_chars:] + "\n\n" + paragrafo
                pagina_inicio = pagina_atual
                continue

            buffer = (buffer + "\n\n" + paragrafo).strip()

            if len(buffer) >= max_chars:
                chunk = _criar_chunk(buffer, sequencia, pagina_inicio, pagina_atual)
                if chunk:
                    chunks.append(chunk)
                    sequencia += 1
                buffer        = buffer[-overlap_chars:]
                pagina_inicio = pagina_atual

    # Flush final
    if buffer.strip():
        chunk = _criar_chunk(buffer, sequencia, pagina_inicio, pagina_atual)
        if chunk:
            chunks.append(chunk)

    logger.info("Chunking gerou %d chunks.", len(chunks))
    return chunks


def _criar_chunk(
    conteudo: str,
    sequencia: int,
    pagina_inicio: int,
    pagina_fim: int
) -> Optional[Chunk]:
    conteudo  = conteudo.strip()
    min_chars = cfg.chunking.min_chunk_size * 4
    if len(conteudo) < min_chars:
        return None
    return Chunk(
        conteudo      = conteudo,
        sequencia     = sequencia,
        pagina_inicio = pagina_inicio,
        pagina_fim    = pagina_fim,
        secao         = detectar_secao(conteudo)
    )


# ── Persistência no PostgreSQL ────────────────────────────────────────────────

def _calcular_hash(caminho: Path) -> str:
    sha256 = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(65536), b""):
            sha256.update(bloco)
    return sha256.hexdigest()


def _documento_ja_existe(hash_arquivo: str) -> bool:
    with get_cursor() as cur:
        cur.execute(
            "SELECT id FROM documentos WHERE hash_arquivo = %s",
            (hash_arquivo,)
        )
        return cur.fetchone() is not None


def _salvar_documento(meta: MetadadosDocumento, arquivo: Path, hash_arquivo: str) -> int:
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO documentos
                (titulo, numero, tipo, orgao_emissor, data_publicacao,
                 data_vigencia, arquivo_origem, hash_arquivo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                meta.titulo, meta.numero, meta.tipo, meta.orgao_emissor,
                meta.data_publicacao, meta.data_vigencia,
                arquivo.name, hash_arquivo
            )
        )
        row = cur.fetchone()
        return row["id"]


def _salvar_chunks(documento_id: int, chunks: List[Chunk]) -> None:
    """Salva todos os chunks de um documento em batch."""
    import psycopg2.extras

    with get_cursor(commit=True) as cur:
        registros = []
        for chunk in chunks:
            embedding = gerar_embedding(chunk.conteudo)
            registros.append((
                documento_id,
                chunk.sequencia,
                chunk.conteudo,
                embedding,
                chunk.pagina_inicio,
                chunk.pagina_fim,
                chunk.secao,
                chunk.tokens_aprox
            ))

        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO chunks
                (documento_id, sequencia, conteudo, embedding,
                 pagina_inicio, pagina_fim, secao, tokens_aprox)
            VALUES %s
            """,
            registros,
            template="(%s, %s, %s, %s::vector, %s, %s, %s, %s)"
        )

    logger.info("  → %d chunks salvos no pgvector.", len(chunks))


def _registrar_log(documento_id: int, tipo_evento: str, descricao: str) -> None:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO log_atualizacoes (documento_id, tipo_evento, descricao) VALUES (%s, %s, %s)",
            (documento_id, tipo_evento, descricao)
        )


# ── Ponto de entrada principal ────────────────────────────────────────────────

def ingerir_pdf(caminho_pdf: Path, meta: MetadadosDocumento) -> Optional[int]:
    """
    Pipeline completo de ingestão de um PDF normativo.
    Retorna o ID do documento criado, ou None se já existia.
    """
    logger.info("=== Iniciando ingestão: %s ===", caminho_pdf.name)

    hash_arquivo = _calcular_hash(caminho_pdf)
    if _documento_ja_existe(hash_arquivo):
        logger.warning("Documento já ingerido (hash idêntico). Pulando.")
        return None

    paginas = extrair_texto_pdf(caminho_pdf)
    if not paginas:
        logger.error("Nenhum texto extraído do PDF. Verifique se não é imagem/escaneado.")
        return None

    chunks = chunkar_texto(paginas)
    if not chunks:
        logger.error("Nenhum chunk gerado. Verifique o conteúdo do PDF.")
        return None

    documento_id = _salvar_documento(meta, caminho_pdf, hash_arquivo)
    logger.info("Documento ID=%d criado. Gerando embeddings para %d chunks...", documento_id, len(chunks))

    _salvar_chunks(documento_id, chunks)
    _registrar_log(documento_id, "INGESTED", f"{len(chunks)} chunks indexados.")

    logger.info("=== Ingestão concluída: ID=%d ===", documento_id)
    return documento_id


def ingerir_pasta(pasta: Optional[Path] = None) -> None:
    """
    Ingere todos os PDFs de uma pasta.
    Formato esperado do nome: TIPO_NUMERO_TITULO.pdf
    Ex: IN_001-2023_Compras-Diretas.pdf
    """
    pasta = pasta or Path(cfg.pasta_ingestion)
    pdfs  = sorted(pasta.glob("*.pdf"))

    if not pdfs:
        logger.warning("Nenhum PDF encontrado em '%s'.", pasta)
        return

    logger.info("Encontrados %d PDFs para ingestão.", len(pdfs))
    sucesso = 0

    for pdf in pdfs:
        meta = _inferir_metadados(pdf)
        try:
            doc_id = ingerir_pdf(pdf, meta)
            if doc_id:
                sucesso += 1
        except Exception as exc:
            logger.error("Erro ao ingerir '%s': %s", pdf.name, exc)

    logger.info("Ingestão concluída: %d/%d documentos processados.", sucesso, len(pdfs))


def _inferir_metadados(pdf: Path) -> MetadadosDocumento:
    """
    Tenta inferir metadados a partir do nome do arquivo.
    Ex: IN_042-2021_Licitacoes-Servicos.pdf → tipo=IN, numero=042/2021
    """
    stem   = pdf.stem.replace("_", " ").replace("-", " ")
    partes = stem.split()

    tipo   = "IN"
    titulo = stem

    if partes:
        tipos_conhecidos = {"IN", "RESOLUCAO", "PORTARIA", "ESTATUTO", "DECRETO", "LEI"}
        if partes[0].upper() in tipos_conhecidos:
            tipo   = partes[0].upper()
            titulo = " ".join(partes[1:]) if len(partes) > 1 else stem

    return MetadadosDocumento(titulo=titulo, tipo=tipo, numero="")
