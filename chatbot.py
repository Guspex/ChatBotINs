# chatbot.py
# ============================================================
# ORACULO/UDESC — Motor principal do chatbot
# Gerencia sessões, histórico e salva interações no banco.
# ============================================================

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from config import cfg
from connection import get_cursor
from generator import responder
from retriever import ResultadoBusca

logger = logging.getLogger(__name__)


class SessaoChat:
    """
    Representa uma sessão de conversa de um servidor com o ORACULO.
    Gerencia histórico, persiste mensagens e avaliações.
    """

    def __init__(self, servidor_id: str, sessao_id: Optional[str] = None):
        self.servidor_id = servidor_id
        self.sessao_id   = sessao_id or str(uuid.uuid4())
        self.historico: List[dict] = []
        self._criada_em  = datetime.now()

        self._iniciar_sessao()

    # ── Ciclo de vida da sessão ────────────────────────────────────────────

    def _iniciar_sessao(self) -> None:
        with get_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO sessoes (id, servidor_id)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (self.sessao_id, self.servidor_id)
            )
        logger.info("Sessão iniciada: %s (servidor: %s)", self.sessao_id, self.servidor_id)

    def encerrar(self) -> None:
        with get_cursor(commit=True) as cur:
            cur.execute(
                "UPDATE sessoes SET encerrada_em = NOW() WHERE id = %s",
                (self.sessao_id,)
            )
        logger.info("Sessão encerrada: %s", self.sessao_id)

    # ── Persistência de mensagens ──────────────────────────────────────────

    def _salvar_mensagem(
        self,
        role: str,
        conteudo: str,
        chunks_usados: Optional[List[ResultadoBusca]] = None,
        tempo_ms: Optional[int] = None
    ) -> int:
        chunk_ids = [c.chunk_id for c in chunks_usados] if chunks_usados else []

        with get_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO mensagens
                    (sessao_id, role, conteudo, chunks_usados, tempo_resposta_ms)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (self.sessao_id, role, conteudo, chunk_ids or None, tempo_ms)
            )
            row = cur.fetchone()
            return row["id"]

    def avaliar_ultima_resposta(self, positivo: bool) -> None:
        """Registra avaliação 👍 (+1) ou 👎 (-1) da última resposta do assistente."""
        with get_cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE mensagens
                SET avaliacao = %s
                WHERE sessao_id = %s
                  AND role = 'assistant'
                ORDER BY criado_em DESC
                LIMIT 1
                """,
                (1 if positivo else -1, self.sessao_id)
            )
        logger.debug("Avaliação registrada: %s", "👍" if positivo else "👎")

    # ── Interação principal ────────────────────────────────────────────────

    def perguntar(
        self,
        pergunta: str,
        stream: bool = False,
        filtro_tipo: Optional[str] = None,
        filtro_orgao: Optional[str] = None,
    ) -> dict:
        """
        Processa uma pergunta do servidor:
        1. Salva mensagem do usuário
        2. Executa pipeline RAG
        3. Salva resposta do assistente
        4. Atualiza histórico local
        5. Retorna resultado completo
        """
        logger.info("[%s] Pergunta: %s", self.servidor_id, pergunta[:80])

        self._salvar_mensagem("user", pergunta)
        self.historico.append({"role": "user", "content": pergunta})

        resultado = responder(
            pergunta=pergunta,
            historico=self.historico[:-1],
            stream=stream,
            filtro_tipo=filtro_tipo,
            filtro_orgao=filtro_orgao,
        )

        if stream:
            resultado["_sessao"]   = self
            resultado["_pergunta"] = pergunta
            return resultado

        resposta_texto = resultado["resposta"]
        msg_id = self._salvar_mensagem(
            role="assistant",
            conteudo=resposta_texto,
            chunks_usados=resultado["chunks_usados"],
            tempo_ms=resultado["tempo_ms"]
        )

        self.historico.append({"role": "assistant", "content": resposta_texto})
        resultado["mensagem_id"] = msg_id
        return resultado

    def finalizar_streaming(
        self,
        resposta_completa: str,
        chunks_usados: List[ResultadoBusca],
        tempo_ms: int
    ) -> int:
        """Persiste a resposta após consumir o stream completo."""
        msg_id = self._salvar_mensagem(
            role="assistant",
            conteudo=resposta_completa,
            chunks_usados=chunks_usados,
            tempo_ms=tempo_ms
        )
        self.historico.append({"role": "assistant", "content": resposta_completa})
        return msg_id

    # ── Consultas ao histórico ─────────────────────────────────────────────

    def obter_historico_banco(self, limite: int = 20) -> List[dict]:
        """Recupera histórico persistido da sessão atual."""
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT role, conteudo, criado_em, avaliacao, tempo_resposta_ms
                FROM mensagens
                WHERE sessao_id = %s
                ORDER BY criado_em
                LIMIT %s
                """,
                (self.sessao_id, limite)
            )
            return [dict(r) for r in cur.fetchall()]


# ── Carregamento de sessão existente ──────────────────────────────────────────

def carregar_sessao(servidor_id: str, sessao_id: str) -> Optional[SessaoChat]:
    """
    Carrega uma sessão existente e reconstrói o histórico em memória.
    Útil para retomar uma conversa anterior.
    """
    with get_cursor() as cur:
        cur.execute(
            "SELECT id FROM sessoes WHERE id = %s AND servidor_id = %s AND encerrada_em IS NULL",
            (sessao_id, servidor_id)
        )
        if not cur.fetchone():
            logger.warning("Sessão %s não encontrada ou encerrada.", sessao_id)
            return None

    sessao = SessaoChat(servidor_id=servidor_id, sessao_id=sessao_id)

    mensagens = sessao.obter_historico_banco(limite=20)
    sessao.historico = [
        {"role": m["role"], "content": m["conteudo"]}
        for m in mensagens
    ]

    logger.info("Sessão %s recarregada com %d mensagens.", sessao_id, len(sessao.historico))
    return sessao


# ── CLI de desenvolvimento ────────────────────────────────────────────────────

def _cli_loop():
    """Interface de linha de comando para testes durante desenvolvimento."""
    import sys

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    print("\n" + "=" * 60)
    print("  ORACULO/UDESC — Assistente Normativo")
    print("  'sair' para encerrar | '+' avaliar bem | '-' avaliar mal")
    print("=" * 60 + "\n")

    servidor_id = input("Matrícula/login UDESC: ").strip() or "dev_user"
    sessao = SessaoChat(servidor_id=servidor_id)

    try:
        while True:
            print()
            pergunta = input("Você: ").strip()

            if not pergunta:
                continue

            if pergunta.lower() in ("sair", "exit", "quit"):
                sessao.encerrar()
                print("\nSessão encerrada. Até logo!")
                break

            if pergunta in ("+", "-"):
                sessao.avaliar_ultima_resposta(pergunta == "+")
                print("Avaliação registrada. Obrigado!")
                continue

            resultado = sessao.perguntar(pergunta)

            print(f"\n{'─' * 60}")
            print(f"ORACULO: {resultado['resposta']}")
            print(f"{'─' * 60}")
            print(f"⏱  {resultado['tempo_ms']} ms | "
                  f"📄 {len(resultado['chunks_usados'])} trechos normativos usados")

            if resultado["chunks_usados"]:
                print("\nFontes consultadas:")
                for c in resultado["chunks_usados"]:
                    fonte = f"{c.documento_tipo} {c.documento_numero}".strip() or c.documento_titulo
                    print(f"  • {fonte} — {c.orgao_emissor} (sim={c.similaridade:.2f})")

            if resultado["sem_contexto"]:
                print("\n⚠️  Atenção: resposta sem embasamento normativo encontrado.")

    except KeyboardInterrupt:
        sessao.encerrar()
        print("\n\nSessão interrompida.")
        sys.exit(0)


if __name__ == "__main__":
    _cli_loop()
