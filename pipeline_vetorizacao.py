#!/usr/bin/env python3
# pipeline_vetorizacao.py  (v2 — alta precisão)
# ============================================================
# ORACULO/UDESC — Pipeline de Vetorização v2
#
# Melhorias v2:
#   • Número da IN extraído do CONTEÚDO do PDF (não do nome do arquivo)
#   • Anexos e fluxogramas completamente ignorados na entrada
#   • Cada chunk prefixado com cabeçalho da IN (número, pró-reitoria, título)
#   • Chunking por artigo/seção com overlap
#   • Deduplicação por hash SHA256
# ============================================================

import hashlib, json, logging, os, re, sys, time, unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
PASTA_PDFS  = _SCRIPT_DIR / "ingestion" / "pdfs"
PASTA_LOGS  = _SCRIPT_DIR / "ingestion" / "logs"
PASTA_LOGS.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(PASTA_LOGS / "vetorizacao.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("PGHOST",     "localhost"),
    "port":     os.getenv("PGPORT",     "5432"),
    "dbname":   os.getenv("PGDATABASE", "oraculo_udesc"),
    "user":     os.getenv("PGUSER",     "oraculo"),
    "password": os.getenv("PGPASSWORD", "oraculo123"),
}

OLLAMA_URL    = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL   = "nomic-embed-text"
EMBED_DIM     = 768
EMBED_TIMEOUT = 60
CHUNK_SIZE    = 1500
CHUNK_OVERLAP = 200
CHUNK_MIN     = 150

# Palavras que indicam anexo/fluxograma — esses PDFs são ignorados
_SKIP = [
    "anexo", "fluxograma", "declaracao", "declaração", "checklist",
    "formulario", "formulário", "modelo", "guia", "orientac", "orientaç",
    "tabela", "planilha", "requerimento", "termo", "relatorio", "relatório",
    "instrumento", "minuta", "manual", "prestacao", "prestação", "apendice"
]

def _deve_pular(nome: str) -> bool:
    n = nome.lower()
    return any(p in n for p in _SKIP)

# ── Extração de metadados do PDF ──────────────────────────────────────────────

_RE_NUMERO = [
    re.compile(r"INSTRU[CÇ][ÃA]O\s+NORMATIVA\s+N[º°]?\s*(\d{1,3})[,/\s].*?(\d{4})", re.IGNORECASE),
    re.compile(r"\bN[º°]\s*(\d{1,3})[/\-](\d{4})\b"),
    re.compile(r"\b(\d{3})[/\-](20\d{2})\b"),
]

_RE_PR = re.compile(r"\b(PROAD|PROPLAN|PROEN|PROEX|PROPPG|GABINETE|REITOR)\b", re.IGNORECASE)

_RE_DATA = [
    re.compile(r"Florian[oó]polis.*?(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", re.IGNORECASE),
    re.compile(r"[Pp]ublicada\s+em\s+(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})"),
]

_MESES = {
    "janeiro":1,"fevereiro":2,"marco":3,"março":3,"abril":4,"maio":5,
    "junho":6,"julho":7,"agosto":8,"setembro":9,"outubro":10,
    "novembro":11,"dezembro":12
}

_RE_REVOGA = re.compile(
    r"[Rr]evog[ao]\w*\s+(?:a\s+)?IN\s+(?:n[°º]?\s*)?(\d{1,3}[/\-]\d{4})",
    re.IGNORECASE
)


def extrair_meta(texto_p1: str, nome_arquivo: str, cat: dict) -> dict:
    meta = {"numero": "", "pro_reitoria": "UDESC", "data": None, "revoga": "", "titulo": ""}

    # Número — tenta no texto do PDF
    for p in _RE_NUMERO:
        m = p.search(texto_p1[:800])
        if m:
            num = m.group(1).zfill(3)
            ano = m.group(2)
            if "2000" <= ano <= "2030" and num != "000":
                meta["numero"] = f"{num}/{ano}"
                break

    # Fallback: catálogo JSON
    if not meta["numero"] or meta["numero"].startswith("000"):
        n_cat = cat.get("numero", "")
        if n_cat and not n_cat.startswith("000"):
            meta["numero"] = n_cat

    # Fallback: nome do arquivo
    if not meta["numero"] or meta["numero"].startswith("000"):
        m = re.search(r"IN[_\-](\d{3})[_\-](\d{4})", nome_arquivo, re.IGNORECASE)
        if m and m.group(1) != "000":
            meta["numero"] = f"{m.group(1)}/{m.group(2)}"

    if not meta["numero"]:
        meta["numero"] = "S/N"

    # Pró-reitoria
    m = _RE_PR.search(texto_p1[:600])
    if m:
        meta["pro_reitoria"] = m.group(1).upper()
    elif cat.get("pro_reitoria"):
        meta["pro_reitoria"] = cat["pro_reitoria"]
    else:
        m = _RE_PR.search(nome_arquivo)
        if m:
            meta["pro_reitoria"] = m.group(1).upper()

    # Data
    for p in _RE_DATA:
        m = p.search(texto_p1)
        if m:
            try:
                d, mes_s, a = m.group(1), m.group(2), m.group(3)
                mes = int(mes_s) if mes_s.isdigit() else _MESES.get(mes_s.lower().replace("ç","c"), 0)
                if mes:
                    meta["data"] = date(int(a), mes, int(d)).isoformat()
                    break
            except Exception:
                pass
    if not meta["data"]:
        meta["data"] = cat.get("data_publicacao")

    # Revogações
    revs = _RE_REVOGA.findall(texto_p1)
    meta["revoga"] = ", ".join(revs) if revs else cat.get("revoga_in", "")

    # Título
    meta["titulo"] = cat.get("titulo", "") or _titulo_fallback(texto_p1, nome_arquivo)

    return meta


def _titulo_fallback(texto: str, nome: str) -> str:
    for linha in texto.split("\n"):
        l = linha.strip()
        if 15 <= len(l) <= 200 and not re.match(r"^[\d/\-]+$", l):
            return l
    return Path(nome).stem.replace("_", " ")


def _cabecalho(meta: dict) -> str:
    partes = [f"[IN {meta['numero']} — {meta['pro_reitoria']} — UDESC]"]
    if meta["titulo"]:
        partes.append(f"Assunto: {meta['titulo'][:120]}")
    if meta["data"]:
        partes.append(f"Publicada em: {meta['data']}")
    if meta["revoga"]:
        partes.append(f"Revoga: IN {meta['revoga']}")
    return "\n".join(partes)


# ── Extração de texto PDF ─────────────────────────────────────────────────────

def extrair_pdf(caminho: Path) -> List[dict]:
    import pdfplumber
    paginas = []
    try:
        with pdfplumber.open(caminho) as pdf:
            for i, pag in enumerate(pdf.pages, 1):
                txt = pag.extract_text(x_tolerance=2, y_tolerance=2) or ""
                txt = _limpar(txt)
                if len(txt.strip()) >= CHUNK_MIN:
                    paginas.append({"pagina": i, "texto": txt})
    except Exception as e:
        logger.warning("Erro ao ler PDF: %s", e)
    return paginas


def _limpar(t: str) -> str:
    t = unicodedata.normalize("NFC", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[Pp][aá]gina\s+\d+\s+de\s+\d+", "", t)
    t = re.sub(r"^\s*\d{1,3}\s*$", "", t, flags=re.MULTILINE)
    t = re.sub(r"-\n([a-záàâãéêíóôõúüç])", r"\1", t, flags=re.IGNORECASE)
    t = "".join(c for c in t if unicodedata.category(c) not in ("Cc","Cs") or c in ("\n","\t"))
    return t.strip()


# ── Chunking ──────────────────────────────────────────────────────────────────

_RE_SEC = re.compile(
    r"^(Art(?:igo)?\.?\s*\d+[º°]?[oa]?\b|§\s*\d+[º°]?|Parágrafo\s+[Úú]nico|"
    r"Cap[íi]tulo\s+[IVXLCDM]+|Se[çc][ãa]o\s+[IVXLCDM]+|"
    r"CAP[ÍI]TULO\s+[IVXLCDM]+|SE[ÇC][ÃA]O\s+[IVXLCDM]+|Anexo\s+[IVXLCDM\d]+)",
    re.IGNORECASE | re.MULTILINE
)


def chunkar(paginas: List[dict], cabecalho: str) -> List[dict]:
    chunks, buffer, pag_ini, seq = [], "", 1, 0
    for item in paginas:
        pag = item["pagina"]
        for par in item["texto"].split("\n\n"):
            par = par.strip()
            if not par:
                continue
            nova_sec = bool(_RE_SEC.match(par))
            if nova_sec and len(buffer) >= CHUNK_MIN:
                c = _chunk(buffer, cabecalho, seq, pag_ini, pag)
                if c:
                    chunks.append(c); seq += 1
                buffer = buffer[-CHUNK_OVERLAP:].strip()
                pag_ini = pag
            buffer = (buffer + "\n\n" + par).strip()
            if len(buffer) >= CHUNK_SIZE:
                c = _chunk(buffer, cabecalho, seq, pag_ini, pag)
                if c:
                    chunks.append(c); seq += 1
                buffer = buffer[-CHUNK_OVERLAP:].strip()
                pag_ini = pag
    if len(buffer.strip()) >= CHUNK_MIN:
        c = _chunk(buffer, cabecalho, seq, pag_ini, pag if paginas else 1)
        if c:
            chunks.append(c)
    return chunks


def _chunk(conteudo, cabecalho, seq, pag_ini, pag_fim):
    conteudo = conteudo.strip()
    if len(conteudo) < CHUNK_MIN:
        return None
    m = _RE_SEC.search(conteudo[:300])
    return {
        "conteudo":      f"{cabecalho}\n\n{conteudo}",
        "sequencia":     seq,
        "pagina_inicio": pag_ini,
        "pagina_fim":    pag_fim,
        "secao":         m.group(0).strip() if m else "",
    }


# ── Embeddings ────────────────────────────────────────────────────────────────

def _san(t: str) -> str:
    t = unicodedata.normalize("NFC", t)
    t = "".join(c for c in t if unicodedata.category(c) not in ("Cc","Cs") or c in ("\n","\t"))
    return t[:2000].strip()


def embedding(texto: str, tentativas: int = 3) -> List[float]:
    import httpx
    texto = _san(texto)
    if not texto:
        return [0.0] * EMBED_DIM
    for t in range(1, tentativas + 1):
        try:
            r = httpx.post(f"{OLLAMA_URL}/api/embed",
                           json={"model": EMBED_MODEL, "input": texto}, timeout=EMBED_TIMEOUT)
            if r.status_code == 200:
                emb = r.json().get("embeddings", [[]])[0]
                if emb and len(emb) == EMBED_DIM:
                    return emb
            r = httpx.post(f"{OLLAMA_URL}/api/embeddings",
                           json={"model": EMBED_MODEL, "prompt": texto}, timeout=EMBED_TIMEOUT)
            r.raise_for_status()
            emb = r.json().get("embedding", [])
            if emb and len(emb) == EMBED_DIM:
                return emb
            raise ValueError("Embedding vazio")
        except Exception as e:
            logger.warning("Embedding t%d/%d: %s", t, tentativas, e)
            if t < tentativas:
                time.sleep(2 ** t)
    raise RuntimeError("Embedding falhou após todas as tentativas.")


# ── Banco de dados ────────────────────────────────────────────────────────────

def _conn():
    import psycopg2
    c = DB_CONFIG
    conn = psycopg2.connect(
        host            = c["host"],
        port            = c["port"],
        dbname          = c["dbname"],
        user            = c["user"],
        password        = c["password"],
        client_encoding = "utf8",
    )
    conn.autocommit = False
    return conn

def _hash(p: Path) -> str:
    s = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""): s.update(b)
    return s.hexdigest()

def _ja_existe(h: str) -> bool:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM documentos WHERE hash_arquivo=%s", (h,))
        return cur.fetchone() is not None
    finally:
        conn.close()

def _salvar(meta: dict, chunks: List[dict]) -> int:
    import psycopg2, psycopg2.extras
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO documentos
                (titulo,numero,tipo,pro_reitoria,orgao_emissor,
                 data_publicacao,revogado,revoga_in,arquivo_origem,hash_arquivo,metadados)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            meta["titulo"][:500], meta["numero"], "IN",
            meta["pro_reitoria"], meta["pro_reitoria"],
            meta.get("data"), False, meta.get("revoga",""),
            meta["arquivo"], meta["hash"],
            json.dumps(meta.get("extras",{}), ensure_ascii=False)
        ))
        doc_id = cur.fetchone()["id"]
        psycopg2.extras.execute_values(cur, """
            INSERT INTO chunks
                (documento_id,sequencia,conteudo,embedding,
                 pagina_inicio,pagina_fim,secao,tokens_aprox)
            VALUES %s
        """, [
            (doc_id, c["sequencia"], c["conteudo"],
             "[" + ",".join(str(v) for v in c["embedding"]) + "]",
             c["pagina_inicio"], c["pagina_fim"],
             c["secao"], len(c["conteudo"]) // 4)
            for c in chunks
        ], template="(%s,%s,%s,%s::vector,%s,%s,%s,%s)")
        cur.execute(
            "INSERT INTO log_atualizacoes (documento_id,tipo_evento,descricao) VALUES (%s,'INDEXADO',%s)",
            (doc_id, f"{len(chunks)} chunks — {datetime.now().isoformat()}")
        )
        conn.commit()
        return doc_id
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()

def _remover(h: str):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM documentos WHERE hash_arquivo=%s", (h,))
        row = cur.fetchone()
        if row:
            did = row[0]
            for t in ["chunks","log_atualizacoes","documentos"]:
                col = "documento_id" if t != "documentos" else "id"
                cur.execute(f"DELETE FROM {t} WHERE {col}=%s", (did,))
        conn.commit()
    finally:
        conn.close()


# ── Catálogo JSON ─────────────────────────────────────────────────────────────

def _catalogo() -> dict:
    jsons = sorted(PASTA_LOGS.glob("scraper_*.json"), reverse=True)
    if not jsons:
        return {}
    dados = json.loads(jsons[0].read_text(encoding="utf-8"))
    idx = {}
    for item in dados.get("instrucoes", []):
        if item.get("caminho_local"):
            idx[Path(item["caminho_local"]).name] = item
    logger.info("Catálogo: %d entradas (%s)", len(idx), jsons[0].name)
    return idx


# ── Pipeline ──────────────────────────────────────────────────────────────────

def processar(caminho: Path, cat: dict) -> dict:
    nome  = caminho.name
    stats = {"pdf": nome, "status": "ok", "numero": "", "chunks": 0, "erro": ""}

    if _deve_pular(nome):
        logger.info("  ⊘ PULADO (anexo): %s", nome)
        stats["status"] = "pulado"; return stats

    h = _hash(caminho)
    if _ja_existe(h):
        logger.info("  ✓ Já indexado: %s", nome)
        stats["status"] = "existia"; return stats

    try:
        paginas = extrair_pdf(caminho)
        if not paginas:
            raise ValueError("PDF sem texto extraível (escaneado/imagem).")

        c = cat.get(nome, {})
        meta = extrair_meta(paginas[0]["texto"], nome, c)
        cab  = _cabecalho(meta)

        logger.info("  IN %-12s | %-10s | %s", meta["numero"], meta["pro_reitoria"], meta["titulo"][:50])

        chunks = chunkar(paginas, cab)
        if not chunks:
            raise ValueError("Nenhum chunk gerado.")

        validos = []
        for i, chunk in enumerate(chunks, 1):
            try:
                chunk["embedding"] = embedding(chunk["conteudo"])
                validos.append(chunk)
                if i % 10 == 0 or i == len(chunks):
                    logger.info("    Embeddings: %d/%d", i, len(chunks))
            except Exception as e:
                logger.warning("    Chunk %d ignorado: %s", i, e)

        if not validos:
            raise ValueError("Nenhum chunk vetorizado.")

        doc_id = _salvar({
            "titulo":       meta["titulo"],
            "numero":       meta["numero"],
            "pro_reitoria": meta["pro_reitoria"],
            "data":         meta["data"],
            "revoga":       meta["revoga"],
            "arquivo":      nome,
            "hash":         h,
            "extras":       {"url": c.get("url_pdf",""), "paginas": len(paginas)},
        }, validos)

        logger.info("  ✅ ID=%-4d | %d chunks", doc_id, len(validos))
        stats["numero"] = meta["numero"]
        stats["chunks"] = len(validos)

    except Exception as e:
        logger.error("  ❌ %s: %s", nome, e)
        stats["status"] = "erro"; stats["erro"] = str(e)

    return stats


def executar(limite=None, forcar=False):
    inicio = time.time()
    logger.info("=" * 62)
    logger.info("  ORACULO/UDESC — Pipeline Vetorização v2")
    logger.info("=" * 62)

    import httpx, psycopg2
    try:
        conn = _conn(); cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
        assert cur.fetchone(); conn.close()
        logger.info("[1/4] PostgreSQL + pgvector ✓")
    except Exception as e:
        logger.error("PostgreSQL: %s", e); sys.exit(1)

    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        mods = [m["name"] for m in r.json().get("models",[])]
        assert any(EMBED_MODEL.split(":")[0] in m for m in mods), f"Execute: ollama pull {EMBED_MODEL}"
        logger.info("[2/4] Ollama + %s ✓", EMBED_MODEL)
    except Exception as e:
        logger.error("Ollama: %s", e); sys.exit(1)

    cat = _catalogo()
    logger.info("[3/4] Catálogo: %d entradas", len(cat))

    todos = sorted(PASTA_PDFS.glob("*.pdf"))
    pdfs  = [p for p in todos if not _deve_pular(p.name)]
    pulados_ini = len(todos) - len(pdfs)
    if limite: pdfs = pdfs[:limite]
    logger.info("[4/4] PDFs a processar: %d (pulados como anexo: %d)\n", len(pdfs), pulados_ini)

    if not pdfs:
        logger.warning("Nenhum PDF em %s", PASTA_PDFS); sys.exit(0)

    resultados = []
    for i, pdf in enumerate(pdfs, 1):
        logger.info("─── [%d/%d] %s", i, len(pdfs), pdf.name)
        if forcar: _remover(_hash(pdf))
        resultados.append(processar(pdf, cat))

    dur      = time.time() - inicio
    idx      = sum(1 for r in resultados if r["status"]=="ok")
    ex       = sum(1 for r in resultados if r["status"]=="existia")
    pul      = sum(1 for r in resultados if r["status"]=="pulado") + pulados_ini
    errs     = [r for r in resultados if r["status"]=="erro"]
    t_chunks = sum(r["chunks"] for r in resultados)

    logger.info("\n" + "="*62)
    logger.info("  RELATÓRIO FINAL")
    logger.info("="*62)
    logger.info("  PDFs processados    : %d", len(pdfs))
    logger.info("  ✅ Indexados agora  : %d", idx)
    logger.info("  ✓  Já existiam      : %d", ex)
    logger.info("  ⊘  Pulados (anexos) : %d", pul)
    logger.info("  ❌ Com erro         : %d", len(errs))
    logger.info("  Chunks no banco     : %d", t_chunks)
    logger.info("  Tempo total         : %.1f min", dur/60)
    if errs:
        logger.info("\n  PDFs com erro:")
        for r in errs: logger.info("    • %s\n      %s", r["pdf"], r["erro"])

    rel = PASTA_LOGS / f"vetorizacao_{date.today().isoformat()}.json"
    rel.write_text(json.dumps({
        "data": date.today().isoformat(), "duracao_s": round(dur),
        "resumo": {"total":len(pdfs),"indexados":idx,"existiam":ex,
                   "pulados":pul,"erros":len(errs),"chunks":t_chunks},
        "detalhes": resultados
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("  Relatório salvo     : %s\n" + "="*62, rel.name)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="ORACULO/UDESC — Vetorização v2")
    p.add_argument("--limite", type=int, default=None)
    p.add_argument("--forcar", action="store_true")
    a = p.parse_args()
    executar(limite=a.limite, forcar=a.forcar)