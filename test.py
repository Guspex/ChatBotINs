#!/usr/bin/env python3
# test.py
# ============================================================
# ORACULO/UDESC — Testes do banco vetorial e integração com AYA
#
# Uso:
#   python test.py               → roda todos os testes
#   python test.py --banco       → só testa conexão e banco
#   python test.py --busca       → só testa busca semântica
#   python test.py --aya         → só testa geração com AYA 8B
#   python test.py --interativo  → modo chat para testar perguntas livres
# ============================================================

import json
import os
import sys
import time
import argparse
from pathlib import Path

# ── Configuração ───────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("PGHOST",     "localhost"),
    "port":     os.getenv("PGPORT",     "5432"),
    "dbname":   os.getenv("PGDATABASE", "oraculo_udesc"),
    "user":     os.getenv("PGUSER",     "oraculo"),
    "password": os.getenv("PGPASSWORD", "oraculo123"),
}

OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL  = "nomic-embed-text"
CHAT_MODEL   = "aya-expanse:8b"
EMBED_DIM    = 768
TOP_K        = 5
SIMILARIDADE_MINIMA = 0.3

# Perguntas de teste cobrindo diferentes áreas das INs
PERGUNTAS_TESTE = [
    "Como funciona o processo de compra por dispensa de licitação?",
    "Quais documentos são necessários para afastamento do país?",
    "Como solicitar diárias para viagem a serviço?",
    "Quais são os procedimentos para gestão patrimonial de bens móveis?",
    "Como funciona o estágio probatório do servidor técnico universitário?",
]

# Cores para o terminal
class Cor:
    VERDE   = "\033[92m"
    AMARELO = "\033[93m"
    VERMELHO= "\033[91m"
    AZUL    = "\033[94m"
    CIANO   = "\033[96m"
    NEGRITO = "\033[1m"
    RESET   = "\033[0m"

def ok(msg):    print(f"  {Cor.VERDE}✓{Cor.RESET} {msg}")
def erro(msg):  print(f"  {Cor.VERMELHO}✗{Cor.RESET} {msg}")
def info(msg):  print(f"  {Cor.AZUL}→{Cor.RESET} {msg}")
def aviso(msg): print(f"  {Cor.AMARELO}⚠{Cor.RESET} {msg}")
def titulo(msg):
    print(f"\n{Cor.NEGRITO}{Cor.CIANO}{'═'*60}{Cor.RESET}")
    print(f"{Cor.NEGRITO}{Cor.CIANO}  {msg}{Cor.RESET}")
    print(f"{Cor.NEGRITO}{Cor.CIANO}{'═'*60}{Cor.RESET}")
def secao(msg):
    print(f"\n{Cor.NEGRITO}── {msg} {'─'*(55-len(msg))}{Cor.RESET}")


# ══════════════════════════════════════════════════════════════
# 1. TESTES DE INFRAESTRUTURA
# ══════════════════════════════════════════════════════════════

def testar_postgres() -> bool:
    secao("PostgreSQL + pgvector")
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        erro("psycopg2 não instalado. Execute: pip install psycopg2-binary")
        return False

    try:
        conn = psycopg2.connect(
            host            = DB_CONFIG["host"],
            port            = DB_CONFIG["port"],
            dbname          = DB_CONFIG["dbname"],
            user            = DB_CONFIG["user"],
            password        = DB_CONFIG["password"],
            client_encoding = "utf8",
        )
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Testa pgvector
        cur.execute("SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()
        if not row:
            erro("Extensão pgvector NÃO está instalada.")
            conn.close()
            return False
        ok(f"pgvector {row['extversion']} ativo")

        # Estatísticas do banco
        cur.execute("SELECT COUNT(*) AS total FROM documentos WHERE revogado = FALSE")
        docs = cur.fetchone()["total"]

        cur.execute("SELECT COUNT(*) AS total FROM chunks")
        chunks = cur.fetchone()["total"]

        cur.execute("""
            SELECT tipo, orgao_emissor, COUNT(*) AS qtd
            FROM documentos
            WHERE revogado = FALSE
            GROUP BY tipo, orgao_emissor
            ORDER BY qtd DESC
            LIMIT 5
        """)
        grupos = cur.fetchall()

        ok(f"Documentos indexados : {docs}")
        ok(f"Chunks no pgvector   : {chunks}")

        if chunks == 0:
            aviso("Banco vazio! Execute pipeline_vetorizacao.py primeiro.")
            conn.close()
            return False

        print()
        info("Distribuição por Pró-Reitoria:")
        for g in grupos:
            print(f"       {g['orgao_emissor']:10} → {g['qtd']} documento(s)")

        conn.close()
        return True

    except Exception as e:
        erro(f"Falha na conexão: {e}")
        return False


def testar_ollama() -> bool:
    secao("Ollama — Modelos")
    try:
        import httpx
    except ImportError:
        erro("httpx não instalado. Execute: pip install httpx")
        return False

    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        resp.raise_for_status()
        modelos = [m["name"] for m in resp.json().get("models", [])]
        ok(f"Ollama online em {OLLAMA_URL}")
        info(f"Modelos disponíveis: {', '.join(modelos) or 'nenhum'}")
    except Exception as e:
        erro(f"Ollama não acessível: {e}")
        return False

    # Verifica embed
    embed_ok = any(EMBED_MODEL.split(":")[0] in m for m in modelos)
    if embed_ok:
        ok(f"Modelo de embedding  : {EMBED_MODEL}")
    else:
        erro(f"Modelo '{EMBED_MODEL}' não encontrado.")
        aviso(f"Execute: ollama pull {EMBED_MODEL}")

    # Verifica AYA
    aya_ok = any(CHAT_MODEL.split(":")[0] in m for m in modelos)
    if aya_ok:
        ok(f"Modelo de geração    : {CHAT_MODEL}")
    else:
        aviso(f"Modelo '{CHAT_MODEL}' não encontrado.")
        aviso(f"Execute: ollama pull {CHAT_MODEL}")

    return embed_ok


# ══════════════════════════════════════════════════════════════
# 2. TESTE DE EMBEDDING
# ══════════════════════════════════════════════════════════════

def gerar_embedding(texto: str) -> list:
    import httpx, unicodedata

    # Sanitiza
    texto = unicodedata.normalize("NFC", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Cc" or c in ("\n", "\t"))
    texto = texto[:2000].strip()

    # Tenta API moderna
    r = httpx.post(f"{OLLAMA_URL}/api/embed",
                   json={"model": EMBED_MODEL, "input": texto}, timeout=60)
    if r.status_code == 200:
        emb = r.json().get("embeddings", [[]])[0]
        if emb and len(emb) == EMBED_DIM:
            return emb

    # Fallback legado
    r = httpx.post(f"{OLLAMA_URL}/api/embeddings",
                   json={"model": EMBED_MODEL, "prompt": texto}, timeout=60)
    r.raise_for_status()
    return r.json()["embedding"]


def testar_embedding() -> bool:
    secao("Embedding — nomic-embed-text")
    try:
        texto = "licitação dispensa de compras diretas UDESC"
        t0 = time.time()
        emb = gerar_embedding(texto)
        ms = int((time.time() - t0) * 1000)

        ok(f"Embedding gerado em {ms}ms")
        ok(f"Dimensão: {len(emb)} (esperado: {EMBED_DIM})")
        info(f"Primeiros 5 valores: {[round(v,4) for v in emb[:5]]}")
        return len(emb) == EMBED_DIM

    except Exception as e:
        erro(f"Falha ao gerar embedding: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# 3. TESTE DE BUSCA SEMÂNTICA
# ══════════════════════════════════════════════════════════════

def buscar(query: str, top_k: int = TOP_K) -> list:
    import psycopg2, psycopg2.extras

    emb = gerar_embedding(query)
    emb_str = "[" + ",".join(str(v) for v in emb) + "]"

    conn = psycopg2.connect(
        host            = DB_CONFIG["host"],
        port            = DB_CONFIG["port"],
        dbname          = DB_CONFIG["dbname"],
        user            = DB_CONFIG["user"],
        password        = DB_CONFIG["password"],
        client_encoding = "utf8",
    )
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT
            d.numero        AS doc_numero,
            d.titulo        AS doc_titulo,
            d.orgao_emissor,
            d.data_publicacao,
            c.conteudo,
            c.secao,
            c.pagina_inicio,
            1 - (c.embedding <=> %s::vector) AS similaridade
        FROM chunks c
        JOIN documentos d ON d.id = c.documento_id
        WHERE d.revogado = FALSE
          AND 1 - (c.embedding <=> %s::vector) >= %s
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
    """, (emb_str, emb_str, SIMILARIDADE_MINIMA, emb_str, top_k))

    resultados = [dict(r) for r in cur.fetchall()]
    conn.close()
    return resultados


def testar_busca() -> bool:
    secao("Busca Semântica — pgvector")
    todos_ok = True

    for pergunta in PERGUNTAS_TESTE:
        print(f"\n  {Cor.AMARELO}Pergunta:{Cor.RESET} {pergunta}")
        try:
            t0 = time.time()
            resultados = buscar(pergunta)
            ms = int((time.time() - t0) * 1000)

            if not resultados:
                aviso(f"Nenhum resultado (similaridade >= {SIMILARIDADE_MINIMA})")
                todos_ok = False
                continue

            melhor = resultados[0]
            sim    = melhor["similaridade"]
            fonte  = f"IN {melhor['doc_numero']}".strip() or melhor["doc_titulo"]
            cor_sim = Cor.VERDE if sim >= 0.5 else Cor.AMARELO if sim >= 0.35 else Cor.VERMELHO

            ok(f"{len(resultados)} resultado(s) em {ms}ms")
            print(f"       Melhor: {fonte} — {melhor['orgao_emissor']} "
                  f"({cor_sim}sim={sim:.3f}{Cor.RESET})")
            if melhor["secao"]:
                print(f"       Seção : {melhor['secao']} | Pág. {melhor['pagina_inicio']}")
            print(f"       Trecho: {melhor['conteudo'][:120].strip()}...")

        except Exception as e:
            erro(f"Erro na busca: {e}")
            todos_ok = False

    return todos_ok


# ══════════════════════════════════════════════════════════════
# 4. TESTE DE GERAÇÃO COM AYA-EXPANSE 8B
# ══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """Você é o ORACULO, assistente especializado em legislação e processos administrativos da UDESC.

Regras:
1. Use APENAS as informações do contexto normativo fornecido.
2. Cite sempre a fonte (número da IN) entre colchetes. Ex: [IN 001/2024]
3. Se o contexto não tiver a informação, diga claramente que não encontrou.
4. Responda sempre em português brasileiro, de forma clara e objetiva.
5. Para procedimentos, use passos numerados."""


def montar_contexto(resultados: list) -> str:
    if not resultados:
        return ""
    partes = []
    for i, r in enumerate(resultados, 1):
        fonte = f"IN {r['doc_numero']}".strip() or r["doc_titulo"]
        cab   = f"[FONTE {i}: {fonte} — {r['orgao_emissor']}]"
        if r["secao"]:
            cab += f" {r['secao']}"
        partes.append(f"{cab}\n{r['conteudo']}")
    return "\n\n---\n\n".join(partes)


def _aquecer_modelo() -> bool:
    """
    Carrega o modelo na memória antes de gerar a resposta.
    O aya-expanse:8b (5.1 GB) pode demorar 30-60s no primeiro uso.
    """
    import httpx
    print(f"  → Carregando {CHAT_MODEL} na memória...", end="", flush=True)
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": CHAT_MODEL, "prompt": "", "keep_alive": "10m", "stream": False},
            timeout=120
        )
        if r.status_code in (200, 400):
            print(f" ✓ pronto!")
            return True
        print(f" status {r.status_code}")
        return False
    except Exception as e:
        print(f" ⚠ {e}")
        return False


def _detectar_endpoint_chat() -> str:
    import httpx
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/chat",
                       json={"model": CHAT_MODEL, "messages": [], "stream": False},
                       timeout=10)
        if r.status_code != 404:
            return "chat"
    except Exception:
        pass
    return "generate"


def gerar_resposta(pergunta: str, contexto: str, stream: bool = False) -> str:
    import httpx

    _aquecer_modelo()

    conteudo_usuario = (
        f"CONTEXTO NORMATIVO:\n{'='*50}\n{contexto}\n{'='*50}\n\n"
        f"PERGUNTA: {pergunta}"
        if contexto else
        f"ATENÇÃO: Nenhum documento normativo encontrado para esta pergunta.\n\nPERGUNTA: {pergunta}"
    )

    endpoint = _detectar_endpoint_chat()

    if endpoint == "chat":
        payload = {
            "model": CHAT_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": conteudo_usuario},
            ],
            "stream": stream,
            "options": {"temperature": 0.1, "num_ctx": 4096},
        }

        if stream:
            resposta = ""
            with httpx.stream("POST", f"{OLLAMA_URL}/api/chat",
                              json=payload, timeout=300) as resp:
                resp.raise_for_status()
                for linha in resp.iter_lines():
                    if not linha:
                        continue
                    data = json.loads(linha)
                    token = data.get("message", {}).get("content", "")
                    if token:
                        print(token, end="", flush=True)
                        resposta += token
                    if data.get("done"):
                        break
            print()
            return resposta
        else:
            resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300)
            resp.raise_for_status()
            return resp.json()["message"]["content"]

    else:
        prompt_completo = (
            f"{SYSTEM_PROMPT}\n\n"
            f"{conteudo_usuario}\n\n"
            f"Resposta:"
        )
        payload = {
            "model": CHAT_MODEL,
            "prompt": prompt_completo,
            "stream": stream,
            "options": {"temperature": 0.1, "num_ctx": 4096},
        }

        if stream:
            resposta = ""
            with httpx.stream("POST", f"{OLLAMA_URL}/api/generate",
                              json=payload, timeout=3000) as resp:
                resp.raise_for_status()
                for linha in resp.iter_lines():
                    if not linha:
                        continue
                    data = json.loads(linha)
                    token = data.get("response", "")
                    if token:
                        print(token, end="", flush=True)
                        resposta += token
                    if data.get("done"):
                        break
            print()
            return resposta
        else:
            resp = httpx.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=300)
            resp.raise_for_status()
            return resp.json()["response"]


def testar_aya() -> bool:
    secao("Geração de Resposta — AYA-Expanse 8B")

    pergunta = PERGUNTAS_TESTE[0]
    print(f"\n  {Cor.AMARELO}Pergunta de teste:{Cor.RESET} {pergunta}\n")

    try:
        t0 = time.time()
        resultados = buscar(pergunta)
        contexto   = montar_contexto(resultados)
        ms_busca   = int((time.time() - t0) * 10000)

        info(f"Contexto montado: {len(resultados)} chunks, {len(contexto)} chars ({ms_busca}ms)")

        if not resultados:
            aviso("Nenhum contexto encontrado — AYA responderá sem base normativa.")

        print(f"\n  {Cor.CIANO}Resposta do ORACULO:{Cor.RESET}")
        print(f"  {'─'*56}")

        t0 = time.time()
        resposta = gerar_resposta(pergunta, contexto, stream=False)
        ms_gen   = int((time.time() - t0) * 10000)

        for linha in resposta.split("\n"):
            print(f"  {linha}")

        print(f"  {'─'*56}")
        ok(f"Resposta gerada em {ms_gen}ms ({len(resposta)} chars)")

        if resultados:
            print()
            info("Fontes utilizadas:")
            for r in resultados:
                fonte = f"IN {r['doc_numero']}".strip() or r["doc_titulo"]
                print(f"       • {fonte} — {r['orgao_emissor']} (sim={r['similaridade']:.3f})")

        return True

    except Exception as e:
        erro(f"Falha na geração: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# 5. MODO INTERATIVO
# ══════════════════════════════════════════════════════════════

def modo_interativo():
    titulo("ORACULO/UDESC — Modo Interativo de Teste")
    print(f"  Digite sua pergunta e pressione Enter.")
    print(f"  Comandos: {Cor.AMARELO}sair{Cor.RESET} | "
          f"{Cor.AMARELO}fontes{Cor.RESET} (mostra chunks usados) | "
          f"{Cor.AMARELO}limpar{Cor.RESET}\n")

    ultimas_fontes = []

    while True:
        try:
            print()
            pergunta = input(f"{Cor.NEGRITO}Você:{Cor.RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nEncerrando. Até logo!")
            break

        if not pergunta:
            continue

        if pergunta.lower() in ("sair", "exit", "quit"):
            print("Encerrando. Até logo!")
            break

        if pergunta.lower() == "fontes":
            if ultimas_fontes:
                print("\nFontes da última resposta:")
                for r in ultimas_fontes:
                    fonte = f"IN {r['doc_numero']}".strip() or r["doc_titulo"]
                    print(f"  • {fonte} ({r['orgao_emissor']}) sim={r['similaridade']:.3f}")
                    print(f"    {r['conteudo'][:150]}...")
            else:
                print("Nenhuma resposta anterior.")
            continue

        if pergunta.lower() == "limpar":
            os.system("cls" if os.name == "nt" else "clear")
            continue

        try:
            t0 = time.time()
            resultados     = buscar(pergunta)
            contexto       = montar_contexto(resultados)
            ultimas_fontes = resultados
            ms_busca       = int((time.time() - t0) * 10000)

            sem_contexto = len(resultados) == 0

            print(f"\n{Cor.NEGRITO}{Cor.CIANO}ORACULO:{Cor.RESET} ", end="", flush=True)
            t0 = time.time()
            gerar_resposta(pergunta, contexto, stream=True)
            ms_gen = int((time.time() - t0) * 10000)

            print(f"\n{Cor.AZUL}{'─'*60}{Cor.RESET}")
            print(f"{Cor.AZUL}⏱ Busca: {ms_busca}ms | Geração: {ms_gen}ms | "
                  f"Chunks: {len(resultados)}{Cor.RESET}", end="")

            if resultados:
                fontes_str = " | ".join(
                    f"IN {r['doc_numero']}({r['similaridade']:.2f})"
                    for r in resultados[:3]
                )
                print(f"\n{Cor.AZUL}📄 {fontes_str}{Cor.RESET}", end="")

            if sem_contexto:
                print(f"\n{Cor.AMARELO}⚠ Resposta sem base normativa encontrada.{Cor.RESET}", end="")

            print()

        except KeyboardInterrupt:
            print("\n(interrompido)")
            continue
        except Exception as e:
            print(f"\n{Cor.VERMELHO}Erro: {e}{Cor.RESET}")


# ══════════════════════════════════════════════════════════════
# 6. RUNNER PRINCIPAL
# ══════════════════════════════════════════════════════════════

def rodar_todos_testes():
    titulo("ORACULO/UDESC — Suite de Testes")

    resultados = {}

    resultados["postgres"] = testar_postgres()
    if not resultados["postgres"]:
        erro("PostgreSQL falhou — abortando demais testes.")
        return resultados

    resultados["ollama"] = testar_ollama()
    if not resultados["ollama"]:
        erro("Ollama/embedding falhou — abortando demais testes.")
        return resultados

    resultados["embedding"] = testar_embedding()
    resultados["busca"]     = testar_busca()
    resultados["aya"]       = testar_aya()

    secao("Resumo dos Testes")
    icones = {True: f"{Cor.VERDE}✓ PASSOU{Cor.RESET}", False: f"{Cor.VERMELHO}✗ FALHOU{Cor.RESET}"}
    nomes  = {
        "postgres":  "PostgreSQL + pgvector",
        "ollama":    "Ollama + modelos",
        "embedding": "Geração de embeddings",
        "busca":     "Busca semântica",
        "aya":       "Geração com AYA-Expanse 8B",
    }
    for chave, nome in nomes.items():
        status = icones.get(resultados.get(chave, False))
        print(f"  {nome:35} {status}")

    passou = sum(resultados.values())
    total  = len(resultados)
    print(f"\n  {Cor.NEGRITO}Resultado: {passou}/{total} testes passaram{Cor.RESET}")

    return resultados


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ORACULO/UDESC — Testes")
    parser.add_argument("--banco",      action="store_true", help="Testa PostgreSQL + pgvector")
    parser.add_argument("--busca",      action="store_true", help="Testa busca semântica")
    parser.add_argument("--aya",        action="store_true", help="Testa geração com AYA-Expanse 8B")
    parser.add_argument("--interativo", action="store_true", help="Modo chat interativo")
    args = parser.parse_args()

    if args.interativo:
        modo_interativo()
    elif args.banco:
        testar_postgres()
        testar_ollama()
        testar_embedding()
    elif args.busca:
        testar_busca()
    elif args.aya:
        testar_aya()
    else:
        rodar_todos_testes()