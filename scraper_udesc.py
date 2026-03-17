#!/usr/bin/env python3
# orion_udesc/ingestion/scraper_udesc.py
# ============================================================
# Scraper das Instruções Normativas da UDESC
# Fonte: https://www.udesc.br/proreitoria/proplan/normativos/instrucoesnormativas
#
# Fluxo:
#   1. Faz o fetch da página e parseia o HTML
#   2. Extrai metadados de cada IN (número, ano, título, pró-reitoria, URL do PDF)
#   3. Baixa os PDFs que ainda não existem localmente
#   4. Salva metadados no PostgreSQL (tabela documentos)
#   5. Registra resultado em log JSON
# ============================================================

import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

# ── Configuração ───────────────────────────────────────────────────────────────

URL_BASE         = "https://www.udesc.br"
URL_PAGINA_INS   = "https://www.udesc.br/proreitoria/proplan/normativos/instrucoesnormativas"

# Caminhos relativos ao diretório do script — funciona independente de onde o script é executado
_SCRIPT_DIR = Path(__file__).resolve().parent          # .../ingestion/
_BASE_DIR   = _SCRIPT_DIR.parent                       # raiz do projeto (BOT UDESC/ ou orion_udesc/)
PASTA_PDFS  = _BASE_DIR / "ingestion" / "pdfs"
PASTA_LOGS  = _BASE_DIR / "ingestion" / "logs"

TIMEOUT          = 30          # segundos por requisição
DELAY_ENTRE_REQS = 1.5         # segundos entre downloads (respeitoso ao servidor)
MAX_TENTATIVAS   = 3

# Garante que as pastas existem ANTES de configurar o logging
PASTA_PDFS.mkdir(parents=True, exist_ok=True)
PASTA_LOGS.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(PASTA_LOGS / "scraper.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ORION-UDESC-Bot/1.0; "
        "+https://www.udesc.br - coleta automatizada de INs para uso institucional)"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}


# ── Estrutura de dados ─────────────────────────────────────────────────────────

@dataclass
class InstrucaoNormativa:
    numero: str                    # ex: "001"
    ano: int
    titulo: str
    pro_reitoria: str              # ex: "PROAD", "PROPLAN", "GABINETE"
    url_pdf: str
    revogada: bool = False
    revoga_in: str = ""            # ex: "007/2023" — quando esta IN revoga outra
    caminho_local: Optional[str] = None
    hash_arquivo: Optional[str]   = None
    data_publicacao: Optional[date] = None
    anexos: List[str] = field(default_factory=list)  # URLs dos anexos

    @property
    def numero_completo(self) -> str:
        return f"{self.numero}/{self.ano}"

    @property
    def nome_arquivo(self) -> str:
        safe = re.sub(r"[^\w\-]", "_", self.titulo[:50])
        return f"IN_{self.numero}_{self.ano}_{safe}.pdf"


# ── Extração de metadados da página ───────────────────────────────────────────

def _extrair_pro_reitoria(texto: str) -> str:
    """Detecta a pró-reitoria/gabinete no texto da IN."""
    mapa = {
        "PROAD":   ["PROAD", "PRÓ-REITORIA DE ADMINISTRAÇÃO"],
        "PROPLAN": ["PROPLAN", "PRÓ-REITORIA DE PLANEJAMENTO"],
        "PROEN":   ["PROEN", "PRÓ-REITORIA DE ENSINO"],
        "PROEX":   ["PROEX", "PRÓ-REITORIA DE EXTENSÃO"],
        "PROPPG":  ["PROPPG", "PRÓ-REITORIA DE PESQUISA"],
        "GABINETE":["GABINETE", "REITOR"],
    }
    texto_upper = texto.upper()
    for sigla, termos in mapa.items():
        if any(t in texto_upper for t in termos):
            return sigla
    return "UDESC"


def _extrair_numero_in(texto: str) -> tuple[str, int]:
    """
    Extrai número e ano de texto como '001/2024', '002/2023', etc.
    Retorna (numero, ano).
    """
    # Padrão: NNN/AAAA no início do texto
    m = re.search(r"(\d{1,3})[\/\-](\d{4})", texto)
    if m:
        return m.group(1).zfill(3), int(m.group(2))
    return "000", date.today().year


def _detectar_revogacao(texto: str) -> tuple[bool, str]:
    """
    Detecta se a IN está revogada e qual IN a revogou.
    Retorna (revogada, numero_que_revogou).
    """
    # Ex: "Revogada pela IN 003/2024"
    m = re.search(r"[Rr]evog(?:ada|ado)\s+pela?\s+IN\s+([\d\/\-]+)", texto)
    if m:
        return True, m.group(1)
    return False, ""


def _extrair_revoga_outras(texto: str) -> str:
    """
    Extrai qual IN anterior esta IN está revogando.
    Ex: "(Revoga IN 007/2023)" → "007/2023"
    """
    m = re.search(r"[Rr]evog(?:a|ou)\s+(?:a\s+)?IN\s+(?:n[°º]?\s*)?([\d\/\-]+)", texto)
    if m:
        return m.group(1)
    return ""


def _extrair_data_publicacao(texto: str) -> Optional[date]:
    """Tenta extrair data de publicação do texto da IN."""
    meses = {
        "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
        "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
        "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12
    }
    # "Publicada em 15/07/2025"
    m = re.search(r"[Pp]ublicada\s+em\s+(\d{1,2})/(\d{1,2})/(\d{4})", texto)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    # "Publicada em 13 de dezembro de 2023"
    m = re.search(
        r"[Pp]ublicada\s+em\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})",
        texto, re.IGNORECASE
    )
    if m:
        mes = meses.get(m.group(2).lower())
        if mes:
            try:
                return date(int(m.group(3)), mes, int(m.group(1)))
            except ValueError:
                pass
    return None


def _texto_direto(tag) -> str:
    """
    Retorna apenas o texto direto de uma tag, excluindo o conteúdo
    de sub-listas (<ul>/<ol>) aninhadas (que contém os anexos).
    """
    partes = []
    for node in tag.children:
        # Pula sub-listas inteiras (são os anexos)
        if hasattr(node, "name") and node.name in ("ul", "ol"):
            continue
        if hasattr(node, "get_text"):
            partes.append(node.get_text(separator=" ", strip=True))
        else:
            partes.append(str(node).strip())
    return " ".join(p for p in partes if p)


def parsear_pagina(html: str) -> List[InstrucaoNormativa]:
    """
    Parseia o HTML da página de INs da UDESC.
    Retorna lista de InstrucaoNormativa com metadados extraídos.

    Estrutura esperada do HTML:
    <ul>
      <li><strong>2026</strong>
        <ul>
          <li>
            <strong><a href="...pdf">001/2026</a></strong> - Título da IN...
            <ul><li>Anexo I...</li></ul>   ← sub-lista de anexos
          </li>
        </ul>
      </li>
    </ul>
    """
    soup = BeautifulSoup(html, "html.parser")
    instrucoes: List[InstrucaoNormativa] = []

    for item in soup.find_all("li"):
        # Ignora itens que são apenas cabeçalhos de ano (não contêm PDFs diretos)
        # Detecta por: o texto principal do <li> é só um número de 4 dígitos
        texto_direto_item = _texto_direto(item)
        if re.match(r"^\d{4}$", texto_direto_item.strip()):
            continue

        # Procura links PDF DIRETAMENTE neste <li> (não em sub-listas de anexos)
        links_pdf_diretos = []
        for a in item.find_all("a", href=True):
            href = a["href"]
            if not (href.endswith(".pdf") and "arquivos/udesc" in href):
                continue
            # Verifica se o link está em uma sub-lista de anexos
            em_sublista = any(
                p.name in ("ul", "ol")
                for p in a.parents
                if p == item
            )
            # Mais simples: verifica se o link é filho direto do item (não de sub-ul)
            pai_ul = a.find_parent("ul")
            if pai_ul and pai_ul != item.find_parent("ul"):
                # está em uma sub-lista dentro deste item → é anexo
                links_pdf_diretos  # deixa como está, será capturado abaixo
            links_pdf_diretos.append(a)

        if not links_pdf_diretos:
            continue

        # O primeiro link PDF é sempre a IN principal
        link_principal = links_pdf_diretos[0]
        url_pdf = link_principal["href"]
        if not url_pdf.startswith("http"):
            url_pdf = urljoin(URL_BASE, url_pdf)

        # Texto limpo do item (sem sub-listas de anexos)
        texto_item = texto_direto_item

        # Número e ano — extraído do texto do link principal
        texto_link = link_principal.get_text(strip=True)
        numero, ano = _extrair_numero_in(texto_link)
        if numero == "000":
            numero, ano = _extrair_numero_in(texto_item)

        # Pró-reitoria — detectada no texto do link ou do item
        pro_reitoria = _extrair_pro_reitoria(texto_link + " " + texto_item)

        # Título: texto após o padrão "NNN/AAAA - [PROREITORIA] - "
        titulo = texto_item
        # Remove ano isolado no início (artefato de captura do <li> pai)
        titulo = re.sub(r"^\d{4}\s+", "", titulo)
        # Remove o número da IN do início
        titulo = re.sub(r"^\d{1,3}[\/\-]\d{4}\s*[-–—]\s*", "", titulo)
        # Remove sigla de pró-reitoria do início
        titulo = re.sub(
            r"^(?:PROAD|PROPLAN|PROEN|PROEX|PROPPG|GABINETE|REITOR)\s*[-–—]?\s*",
            "", titulo, flags=re.IGNORECASE
        )
        titulo = titulo.strip()[:300]

        # Revogação
        revogada, _ = _detectar_revogacao(texto_item)
        revoga_in   = _extrair_revoga_outras(texto_item)

        # Data de publicação
        data_pub = _extrair_data_publicacao(texto_item)
        if not data_pub and ano:
            data_pub = date(ano, 1, 1)

        # Anexos: demais links PDF dentro deste item
        anexos = [
            (urljoin(URL_BASE, a["href"]) if not a["href"].startswith("http") else a["href"])
            for a in links_pdf_diretos[1:]
        ]

        in_obj = InstrucaoNormativa(
            numero=numero,
            ano=ano,
            titulo=titulo,
            pro_reitoria=pro_reitoria,
            url_pdf=url_pdf,
            revogada=revogada,
            revoga_in=revoga_in,
            data_publicacao=data_pub,
            anexos=anexos
        )
        instrucoes.append(in_obj)
        logger.debug("  Encontrada: IN %s — %s", in_obj.numero_completo, in_obj.titulo[:60])

    # Remove duplicatas por URL
    vistos: set = set()
    unicos: List[InstrucaoNormativa] = []
    for in_obj in instrucoes:
        if in_obj.url_pdf not in vistos:
            vistos.add(in_obj.url_pdf)
            unicos.append(in_obj)

    logger.info("Total de INs encontradas na página: %d", len(unicos))
    return unicos


# ── Download de PDFs ───────────────────────────────────────────────────────────

def _sha256_arquivo(caminho: Path) -> str:
    sha = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(65536), b""):
            sha.update(bloco)
    return sha.hexdigest()


def baixar_pdf(in_obj: InstrucaoNormativa, pasta: Path, client: httpx.Client) -> bool:
    """
    Baixa o PDF da IN para a pasta local.
    Pula se o arquivo já existir (por nome).
    Retorna True se baixado com sucesso.
    """
    caminho = pasta / in_obj.nome_arquivo

    if caminho.exists():
        logger.info("  ✓ Já existe: %s", caminho.name)
        in_obj.caminho_local = str(caminho)
        in_obj.hash_arquivo  = _sha256_arquivo(caminho)
        return True

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            logger.info("  ↓ Baixando [%s]: %s", in_obj.numero_completo, in_obj.url_pdf)
            resp = client.get(in_obj.url_pdf, follow_redirects=True, timeout=TIMEOUT)
            resp.raise_for_status()

            # Verifica se é realmente um PDF
            content_type = resp.headers.get("content-type", "")
            if "pdf" not in content_type and not resp.content[:4] == b"%PDF":
                logger.warning(
                    "  ⚠ Resposta não é PDF (content-type: %s) — IN %s",
                    content_type, in_obj.numero_completo
                )
                return False

            caminho.write_bytes(resp.content)
            in_obj.caminho_local = str(caminho)
            in_obj.hash_arquivo  = _sha256_arquivo(caminho)
            logger.info(
                "  ✓ Salvo: %s (%.1f KB)",
                caminho.name, len(resp.content) / 1024
            )
            return True

        except httpx.HTTPStatusError as e:
            logger.warning(
                "  ✗ HTTP %d ao baixar IN %s (tentativa %d/%d)",
                e.response.status_code, in_obj.numero_completo, tentativa, MAX_TENTATIVAS
            )
        except Exception as e:
            logger.warning(
                "  ✗ Erro ao baixar IN %s: %s (tentativa %d/%d)",
                in_obj.numero_completo, e, tentativa, MAX_TENTATIVAS
            )

        if tentativa < MAX_TENTATIVAS:
            time.sleep(2 ** tentativa)

    return False


# ── Persistência no banco de dados ─────────────────────────────────────────────

def _salvar_no_banco(instrucoes: List[InstrucaoNormativa]) -> dict:
    """
    Salva metadados das INs baixadas no PostgreSQL.
    Retorna estatísticas de inserção.
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        logger.warning("psycopg2 não instalado — pulando gravação no banco.")
        return {"inseridos": 0, "existentes": 0, "erros": 0}

    # Importa config dinamicamente (pode não estar no path)
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from config import cfg
        dsn = cfg.db.dsn
    except Exception:
        # Fallback com variáveis de ambiente
        import os
        dsn = (
            f"host={os.getenv('PGHOST','localhost')} "
            f"port={os.getenv('PGPORT','5432')} "
            f"dbname={os.getenv('PGDATABASE','orion_udesc')} "
            f"user={os.getenv('PGUSER','orion')} "
            f"password={os.getenv('PGPASSWORD','orion_pass')}"
        )

    stats = {"inseridos": 0, "existentes": 0, "erros": 0}

    try:
        conn = psycopg2.connect(dsn)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:
        logger.error("Não foi possível conectar ao banco: %s", e)
        return stats

    for in_obj in instrucoes:
        if not in_obj.caminho_local:
            continue  # não foi baixada

        try:
            # Verifica duplicata por hash
            if in_obj.hash_arquivo:
                cur.execute(
                    "SELECT id FROM documentos WHERE hash_arquivo = %s",
                    (in_obj.hash_arquivo,)
                )
                if cur.fetchone():
                    stats["existentes"] += 1
                    continue

            cur.execute(
                """
                INSERT INTO documentos
                    (titulo, numero, tipo, orgao_emissor, data_publicacao,
                     revogado, arquivo_origem, hash_arquivo, metadados)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (hash_arquivo) DO NOTHING
                RETURNING id
                """,
                (
                    in_obj.titulo[:500],
                    in_obj.numero_completo,
                    "IN",
                    in_obj.pro_reitoria,
                    in_obj.data_publicacao,
                    in_obj.revogada,
                    Path(in_obj.caminho_local).name if in_obj.caminho_local else None,
                    in_obj.hash_arquivo,
                    json.dumps({
                        "url_original": in_obj.url_pdf,
                        "revoga_in": in_obj.revoga_in,
                        "anexos": in_obj.anexos,
                        "caminho_local": in_obj.caminho_local,
                    }, ensure_ascii=False)
                )
            )
            row = cur.fetchone()
            if row:
                doc_id = row["id"]
                cur.execute(
                    "INSERT INTO log_atualizacoes (documento_id, tipo_evento, descricao) VALUES (%s, %s, %s)",
                    (doc_id, "SCRAPED", f"Obtido via scraper UDESC — IN {in_obj.numero_completo}")
                )
                stats["inseridos"] += 1
                logger.info("  💾 Banco: IN %s salva (ID=%d)", in_obj.numero_completo, doc_id)
            else:
                stats["existentes"] += 1

            conn.commit()

        except Exception as e:
            conn.rollback()
            logger.error("  ✗ Erro ao salvar IN %s no banco: %s", in_obj.numero_completo, e)
            stats["erros"] += 1

    cur.close()
    conn.close()
    return stats


# ── Relatório JSON ─────────────────────────────────────────────────────────────

def _salvar_relatorio(instrucoes: List[InstrucaoNormativa], stats_banco: dict) -> Path:
    """Salva relatório completo da execução em JSON."""
    relatorio = {
        "execucao": {
            "data": date.today().isoformat(),
            "total_encontradas": len(instrucoes),
            "total_baixadas": sum(1 for i in instrucoes if i.caminho_local),
            "total_com_erro": sum(1 for i in instrucoes if not i.caminho_local),
            "banco": stats_banco,
        },
        "instrucoes": [
            {
                "numero": i.numero_completo,
                "titulo": i.titulo,
                "pro_reitoria": i.pro_reitoria,
                "ano": i.ano,
                "data_publicacao": i.data_publicacao.isoformat() if i.data_publicacao else None,
                "revogada": i.revogada,
                "revoga_in": i.revoga_in,
                "url_pdf": i.url_pdf,
                "caminho_local": i.caminho_local,
                "hash_sha256": i.hash_arquivo,
                "n_anexos": len(i.anexos),
                "baixada": bool(i.caminho_local),
            }
            for i in instrucoes
        ]
    }

    caminho = PASTA_LOGS / f"scraper_{date.today().isoformat()}.json"
    caminho.write_text(json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8")
    return caminho


# ── Ponto de entrada ───────────────────────────────────────────────────────────

def executar_scraping(
    salvar_banco: bool = True,
    apenas_novas: bool = True,
) -> List[InstrucaoNormativa]:
    """
    Executa o pipeline completo de scraping das INs da UDESC.

    Args:
        salvar_banco:  Se True, persiste metadados no PostgreSQL.
        apenas_novas:  Se True, pula PDFs já existentes na pasta local.

    Returns:
        Lista de InstrucaoNormativa processadas.
    """
    # Pastas já criadas na inicialização do módulo
    logger.info("=" * 60)
    logger.info("ORION/UDESC — Scraper de Instruções Normativas")
    logger.info("Fonte: %s", URL_PAGINA_INS)
    logger.info("=" * 60)

    # 1. Fetch da página
    logger.info("Buscando página de INs...")
    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        try:
            resp = client.get(URL_PAGINA_INS, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error("Falha ao acessar a página: %s", e)
            sys.exit(1)

        html = resp.text
        logger.info("Página obtida. Tamanho: %.1f KB", len(html) / 1024)

        # 2. Parsing
        instrucoes = parsear_pagina(html)

        if not instrucoes:
            logger.error("Nenhuma IN encontrada. Verifique o parsing.")
            sys.exit(1)

        # Ordena por ano desc, número desc
        instrucoes.sort(key=lambda x: (x.ano, x.numero), reverse=True)

        # 3. Download dos PDFs
        logger.info("\nIniciando download de %d PDFs...\n", len(instrucoes))
        baixadas = 0
        erros = 0

        for i, in_obj in enumerate(instrucoes, start=1):
            logger.info("[%d/%d] IN %s — %s", i, len(instrucoes), in_obj.numero_completo, in_obj.pro_reitoria)

            if apenas_novas and (PASTA_PDFS / in_obj.nome_arquivo).exists():
                logger.info("  ✓ Já existe localmente. Pulando download.")
                in_obj.caminho_local = str(PASTA_PDFS / in_obj.nome_arquivo)
                in_obj.hash_arquivo  = _sha256_arquivo(PASTA_PDFS / in_obj.nome_arquivo)
                baixadas += 1
                continue

            sucesso = baixar_pdf(in_obj, PASTA_PDFS, client)
            if sucesso:
                baixadas += 1
            else:
                erros += 1

            # Delay respeitoso entre requisições
            if i < len(instrucoes):
                time.sleep(DELAY_ENTRE_REQS)

    # 4. Salva no banco
    stats_banco = {"inseridos": 0, "existentes": 0, "erros": 0}
    if salvar_banco:
        logger.info("\nSalvando metadados no banco de dados...")
        stats_banco = _salvar_no_banco(instrucoes)

    # 5. Relatório
    caminho_relatorio = _salvar_relatorio(instrucoes, stats_banco)

    # Resumo final
    logger.info("\n" + "=" * 60)
    logger.info("RESUMO DO SCRAPING")
    logger.info("=" * 60)
    logger.info("  INs encontradas na página : %d", len(instrucoes))
    logger.info("  PDFs baixados/disponíveis : %d", baixadas)
    logger.info("  Erros de download         : %d", erros)
    logger.info("  Inseridos no banco        : %d", stats_banco["inseridos"])
    logger.info("  Já existiam no banco      : %d", stats_banco["existentes"])
    logger.info("  Relatório salvo em        : %s", caminho_relatorio)
    logger.info("=" * 60)

    return instrucoes


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scraper de INs da UDESC")
    parser.add_argument(
        "--sem-banco", action="store_true",
        help="Não salva no banco de dados (apenas baixa os PDFs)"
    )
    parser.add_argument(
        "--forcar", action="store_true",
        help="Rebaixa PDFs mesmo que já existam localmente"
    )
    args = parser.parse_args()

    executar_scraping(
        salvar_banco=not args.sem_banco,
        apenas_novas=not args.forcar,
    )