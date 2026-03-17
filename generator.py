# generator.py
# ============================================================
# ORACULO/UDESC — Geração de respostas com AYA-Expanse 8B via Ollama
# Estratégia RAG: contexto normativo + pergunta → resposta fundamentada
# ============================================================

import logging
import time
from typing import Generator, List, Optional

import httpx

from config import cfg
from retriever import ResultadoBusca, buscar, montar_contexto

logger = logging.getLogger(__name__)

_ENDPOINT_CHAT = f"{cfg.ollama.base_url}/api/chat"

# ── Prompt do sistema ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é o ORACULO, assistente especializado em legislação e processos administrativos da Universidade do Estado de Santa Catarina (UDESC).

Suas responsabilidades:
- Orientar servidores públicos sobre processos administrativos com base exclusivamente na legislação fornecida.
- Citar sempre a fonte normativa (número da IN, Resolução, Portaria, etc.) em cada orientação.
- Responder sempre em português brasileiro, de forma clara, objetiva e acessível.
- Organizar respostas longas em passos numerados quando se tratar de procedimentos.

Regras obrigatórias:
1. Use APENAS as informações presentes no contexto normativo fornecido. Nunca invente ou suponha normas.
2. Se o contexto não contiver informação suficiente para responder, diga claramente que não encontrou a norma aplicável e sugira que o servidor consulte o setor responsável.
3. Não forneça opiniões ou interpretações jurídicas — apenas reproduza o que a norma diz.
4. Ao citar um artigo ou seção, indique o documento de origem entre colchetes. Ex: [IN 042/2021, Art. 3º]
5. Se a norma estiver desatualizada ou revogada, alerte o servidor imediatamente.
"""


# ── Histórico de conversa ─────────────────────────────────────────────────────

def _construir_mensagens(
    pergunta: str,
    contexto: str,
    historico: Optional[List[dict]] = None
) -> List[dict]:
    """
    Monta a lista de mensagens para a API do Ollama no formato chat.
    Inclui histórico da sessão (últimas N trocas) para continuidade.
    """
    mensagens = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Adiciona histórico recente (máx. 6 trocas = 12 mensagens)
    if historico:
        mensagens.extend(historico[-12:])

    # Injeta contexto normativo na pergunta do usuário
    if contexto:
        conteudo_usuario = (
            f"CONTEXTO NORMATIVO RECUPERADO:\n"
            f"{'=' * 50}\n"
            f"{contexto}\n"
            f"{'=' * 50}\n\n"
            f"PERGUNTA DO SERVIDOR:\n{pergunta}"
        )
    else:
        conteudo_usuario = (
            f"ATENÇÃO: Não foram encontrados documentos normativos relevantes para esta pergunta.\n\n"
            f"PERGUNTA DO SERVIDOR:\n{pergunta}"
        )

    mensagens.append({"role": "user", "content": conteudo_usuario})
    return mensagens


# ── Chamada ao Ollama ─────────────────────────────────────────────────────────

def _chamar_ollama(mensagens: List[dict]) -> dict:
    payload = {
        "model":   cfg.ollama.model_chat,
        "messages": mensagens,
        "stream":  False,
        "options": {
            "temperature": cfg.ollama.temperature,
            "num_ctx":     cfg.ollama.num_ctx,
        }
    }
    resp = httpx.post(_ENDPOINT_CHAT, json=payload, timeout=cfg.ollama.timeout)
    resp.raise_for_status()
    return resp.json()


def _chamar_ollama_stream(mensagens: List[dict]) -> Generator[str, None, None]:
    """Versão streaming: entrega tokens conforme são gerados."""
    import json as _json

    payload = {
        "model":   cfg.ollama.model_chat,
        "messages": mensagens,
        "stream":  True,
        "options": {
            "temperature": cfg.ollama.temperature,
            "num_ctx":     cfg.ollama.num_ctx,
        }
    }
    with httpx.stream("POST", _ENDPOINT_CHAT, json=payload, timeout=cfg.ollama.timeout) as resp:
        resp.raise_for_status()
        for linha in resp.iter_lines():
            if not linha:
                continue
            data  = _json.loads(linha)
            token = data.get("message", {}).get("content", "")
            if token:
                yield token
            if data.get("done"):
                break


# ── Interface principal ───────────────────────────────────────────────────────

def responder(
    pergunta: str,
    historico: Optional[List[dict]] = None,
    stream: bool = False,
    filtro_tipo: Optional[str] = None,
    filtro_orgao: Optional[str] = None,
) -> dict:
    """
    Pipeline RAG completo:
    1. Busca chunks relevantes no pgvector
    2. Monta contexto normativo
    3. Chama AYA-Expanse 8B com o contexto
    4. Retorna resposta + metadados de rastreabilidade

    Retorna:
        {
            "resposta": str,
            "chunks_usados": List[ResultadoBusca],
            "tempo_ms": int,
            "sem_contexto": bool
        }
    """
    inicio = time.time()

    # 1. Recuperação
    chunks    = buscar(pergunta, filtro_tipo=filtro_tipo, filtro_orgao=filtro_orgao)
    contexto  = montar_contexto(chunks)
    sem_contexto = len(chunks) == 0

    if sem_contexto:
        logger.warning("Nenhum chunk relevante encontrado para: '%s'", pergunta)

    # 2. Construção do prompt
    mensagens = _construir_mensagens(pergunta, contexto, historico)

    # 3. Geração
    if stream:
        return {
            "stream":       _chamar_ollama_stream(mensagens),
            "chunks_usados": chunks,
            "sem_contexto": sem_contexto,
        }

    resposta_data  = _chamar_ollama(mensagens)
    resposta_texto = resposta_data["message"]["content"]
    tempo_ms       = int((time.time() - inicio) * 1000)

    logger.info("Resposta gerada em %d ms. Chunks usados: %d.", tempo_ms, len(chunks))

    return {
        "resposta":      resposta_texto,
        "chunks_usados": chunks,
        "tempo_ms":      tempo_ms,
        "sem_contexto":  sem_contexto,
    }


def gerar_resumo_documento(documento_id: int) -> str:
    """
    Gera um resumo executivo de um documento normativo
    a partir dos seus primeiros chunks.
    """
    from connection import get_cursor

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT c.conteudo, d.titulo, d.numero, d.tipo
            FROM chunks c
            JOIN documentos d ON d.id = c.documento_id
            WHERE c.documento_id = %s
            ORDER BY c.sequencia
            LIMIT 5
            """,
            (documento_id,)
        )
        rows = cur.fetchall()

    if not rows:
        return "Documento não encontrado."

    doc_info        = rows[0]
    contexto_inicial = "\n\n".join(r["conteudo"] for r in rows)

    mensagens = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Faça um resumo executivo em até 5 parágrafos do seguinte documento:\n"
                f"Documento: {doc_info['tipo']} {doc_info['numero']} — {doc_info['titulo']}\n\n"
                f"Conteúdo inicial:\n{contexto_inicial}"
            )
        }
    ]

    data = _chamar_ollama(mensagens)
    return data["message"]["content"]
