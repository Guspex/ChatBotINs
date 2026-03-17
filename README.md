# ORACULO/UDESC — Assistente Normativo

Chatbot RAG (Retrieval-Augmented Generation) para suporte a servidores da UDESC.  
Responde perguntas sobre processos administrativos com base nas **Instruções Normativas oficiais**, citando sempre a fonte.

**Stack:** AYA-Expanse 8B (Ollama) · nomic-embed-text · pgvector (PostgreSQL) · Python 3.11+

---

## Como funciona

```
Pergunta do servidor
        │
        ▼
[nomic-embed-text]  ←  embedding da pergunta (768 dims)
        │
        ▼
[pgvector / HNSW]   ←  busca cosine → top-5 chunks mais relevantes
        │
        ▼
[AYA-Expanse 8B]    ←  gera resposta fundamentada no contexto normativo
        │
        ▼
Resposta com citação das fontes [IN 042/2021, Art. 3º]
        +
Persistência da sessão no PostgreSQL
```

O sistema nunca inventa normas: se não encontrar embasamento nos documentos indexados, informa claramente ao servidor.

---

## Pré-requisitos

| Dependência | Versão mínima | Download |
|---|---|---|
| Python | 3.11+ | [python.org](https://python.org) |
| PostgreSQL | 15+ | [postgresql.org](https://postgresql.org) |
| pgvector | 0.7+ | [github.com/pgvector/pgvector](https://github.com/pgvector/pgvector) |
| Ollama | 0.3+ | [ollama.com](https://ollama.com) |

> **Windows:** instale o PostgreSQL pelo instalador oficial. O pgvector já vem incluído nas versões recentes do instalador da EDB (EnterpriseDB). Caso não esteja, siga as instruções em [pgvector/pgvector](https://github.com/pgvector/pgvector).

---

## Instalação passo a passo

### 1. Clone o repositório

```bash
git clone https://github.com/SEU_USUARIO/oraculo-udesc.git
cd oraculo-udesc
```

### 2. Crie o ambiente virtual e instale as dependências

```bash
# Linux / Mac
python -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
```

```bash
pip install -r requirements.txt
```

### 3. Inicie o Ollama e baixe os modelos

> Em um terminal separado, deixe o Ollama rodando durante todo o uso do projeto.

```bash
ollama serve
```

Em outro terminal, baixe os dois modelos necessários:

```bash
ollama pull nomic-embed-text   # ~274 MB — geração de embeddings
ollama pull aya-expanse:8b     # ~5.1 GB — geração de respostas
```

### 4. Crie o banco de dados

Execute o script de criação automática. Ele pedirá apenas a senha do superusuário `postgres`:

```bash
python criar_banco.py
```

O script cria automaticamente:
- Usuário `oraculo` com senha `oraculo123`
- Banco `oraculo_udesc` em UTF-8
- Extensões `vector` e `unaccent`
- Todas as tabelas, índices e views

> **Importante:** a senha foi definida sem caracteres especiais (`oraculo123`) para evitar erros de encoding no Windows.

### 5. Baixe os PDFs das Instruções Normativas

```bash
python scraper_udesc.py
```

O scraper acessa o site oficial da UDESC, lista todas as INs disponíveis e baixa os PDFs para a pasta `ingestion/pdfs/`. Execuções subsequentes pulam arquivos já baixados.

### 6. Vetorize e indexe os documentos

```bash
python pipeline_vetorizacao.py
```

Para cada PDF, o pipeline:
1. Extrai o texto com `pdfplumber`
2. Divide em chunks por artigo/seção (com overlap)
3. Gera embeddings via `nomic-embed-text`
4. Salva no `pgvector` com índice HNSW

Progresso e eventuais erros ficam em `ingestion/logs/vetorizacao.log`.

### 7. Teste o sistema

```bash
python test.py
```

Resultados esperados:
```
  PostgreSQL + pgvector          ✓ PASSOU
  Ollama + modelos               ✓ PASSOU
  Geração de embeddings          ✓ PASSOU
  Busca semântica                ✓ PASSOU
  Geração com AYA-Expanse 8B     ✓ PASSOU
```

### 8. Inicie o chatbot

```bash
python chatbot.py
```

---

## Uso

### Interface de linha de comando

```
============================================================
  ORACULO/UDESC — Assistente Normativo
  'sair' para encerrar | '+' avaliar bem | '-' avaliar mal
============================================================

Matrícula/login UDESC: 12345

Você: Como funciona o processo de compra por dispensa de licitação?

──────────────────────────────────────────────────────────
ORACULO: De acordo com a IN 042/2021, Art. 24...
──────────────────────────────────────────────────────────
⏱  1843 ms | 📄 4 trechos normativos usados

Fontes consultadas:
  • IN 042/2021 — PROAD (sim=0.87)
  • IN 015/2020 — PROAD (sim=0.74)
```

Comandos disponíveis no chat:

| Comando | Ação |
|---|---|
| `sair` | Encerra a sessão |
| `+` | Avalia a última resposta positivamente 👍 |
| `-` | Avalia a última resposta negativamente 👎 |

### Uso programático

```python
from chatbot import SessaoChat

# Inicia uma sessão para o servidor
sessao = SessaoChat(servidor_id="12345")

# Faz uma pergunta
resultado = sessao.perguntar("Como solicitar diárias para viagem a serviço?")
print(resultado["resposta"])
print(f"Fontes: {[c.documento_numero for c in resultado['chunks_usados']]}")
print(f"Tempo: {resultado['tempo_ms']}ms")

# Avalia a resposta
sessao.avaliar_ultima_resposta(positivo=True)

# Encerra
sessao.encerrar()
```

### Modo interativo de testes

```bash
python test.py --interativo
```

Permite conversar diretamente com o ORACULO no terminal, mostrando métricas de busca e fontes em tempo real.

---

## Comandos de manutenção

```bash
# Baixar novas INs publicadas no site da UDESC
python scraper_udesc.py

# Vetorizar apenas os PDFs ainda não indexados
python pipeline_vetorizacao.py

# Forçar reprocessamento de todos os PDFs
python pipeline_vetorizacao.py --forcar

# Recriar o banco do zero (apaga tudo)
python resetabanco.py

# Testes individuais
python test.py --banco       # PostgreSQL + pgvector
python test.py --busca       # busca semântica
python test.py --aya         # geração com AYA
```

---

## Estrutura do projeto

```
oraculo-udesc/
│
├── config.py                   # Configurações centrais (DB, Ollama, chunking)
├── connection.py               # Gerenciador de conexão PostgreSQL
├── embedder.py                 # Geração de embeddings via Ollama
├── retriever.py                # Busca semântica cosine no pgvector
├── generator.py                # Pipeline RAG + AYA-Expanse 8B
├── chatbot.py                  # Motor do chatbot, sessões e CLI
│
├── criar_banco.py              # Criação automatizada do banco (execute primeiro)
├── resetabanco.py              # Recria o schema do zero (apaga dados)
├── scraper_udesc.py            # Scraper das INs do site da UDESC
├── pipeline_vetorizacao.py     # PDF → chunks → embeddings → pgvector
├── ingestor.py                 # Ingestão manual de PDFs avulsos
│
├── test.py                     # Suite de testes + modo interativo
├── schema.sql                  # DDL de referência (aplicado pelo criar_banco.py)
│
├── requirements.txt
├── .env.example                # Template de variáveis de ambiente
├── .gitignore
│
└── ingestion/
    ├── pdfs/                   # PDFs baixados pelo scraper (não versionados)
    └── logs/                   # Logs e relatórios JSON (não versionados)
```

---

## Banco de dados

### Tabelas principais

| Tabela | Descrição |
|---|---|
| `documentos` | Metadados de cada IN: número, tipo, pró-reitoria, data, hash SHA256 |
| `chunks` | Trechos vetorizados com embedding `VECTOR(768)` |
| `sessoes` | Sessões de conversa por servidor |
| `mensagens` | Histórico completo com avaliações 👍/👎 e tempo de resposta |
| `log_atualizacoes` | Eventos de ingestão, revogações e erros |

### View útil

```sql
-- Chunks com todos os metadados do documento pai (exclui revogados)
SELECT * FROM v_chunks WHERE conteudo ILIKE '%dispensa%';
```

### Consultas úteis

```sql
-- Documentos indexados por pró-reitoria
SELECT pro_reitoria, COUNT(*) FROM documentos
WHERE revogado = FALSE GROUP BY pro_reitoria ORDER BY 2 DESC;

-- Perguntas mais frequentes (últimos 30 dias)
SELECT conteudo, criado_em FROM mensagens
WHERE role = 'user' AND criado_em > NOW() - INTERVAL '30 days'
ORDER BY criado_em DESC LIMIT 20;

-- Respostas com avaliação negativa
SELECT m.conteudo, m.avaliacao, s.servidor_id
FROM mensagens m JOIN sessoes s ON s.id = m.sessao_id
WHERE m.avaliacao = -1 ORDER BY m.criado_em DESC;
```

---

## Configuração avançada

Todas as configurações ficam em `config.py` e podem ser sobrescritas por variáveis de ambiente:

| Variável | Padrão | Descrição |
|---|---|---|
| `PGHOST` | `localhost` | Host do PostgreSQL |
| `PGPORT` | `5432` | Porta do PostgreSQL |
| `PGDATABASE` | `oraculo_udesc` | Nome do banco |
| `PGUSER` | `oraculo` | Usuário do banco |
| `PGPASSWORD` | `oraculo123` | Senha (sem caracteres especiais) |
| `OLLAMA_URL` | `http://localhost:11434` | Endpoint do Ollama |
| `ORACULO_INGESTION_DIR` | `./ingestion/pdfs` | Pasta dos PDFs |
| `ORACULO_LOG_LEVEL` | `INFO` | Nível de log (`DEBUG`, `INFO`, `WARNING`) |

Para usar variáveis de ambiente, crie um arquivo `.env` na raiz:

```env
PGHOST=localhost
PGPORT=5432
PGDATABASE=oraculo_udesc
PGUSER=oraculo
PGPASSWORD=oraculo123
OLLAMA_URL=http://localhost:11434
ORACULO_INGESTION_DIR=./ingestion/pdfs
ORACULO_LOG_LEVEL=INFO
```

Parâmetros de chunking e recuperação também são configuráveis em `config.py`:

```python
ChunkingConfig:
    chunk_size    = 512    # tokens por chunk
    chunk_overlap = 80     # overlap entre chunks
    min_chunk_size = 50    # tamanho mínimo (descarta ruído)

RetrievalConfig:
    top_k               = 5     # chunks retornados por busca
    similarity_threshold = 0.35  # similaridade cosine mínima (0–1)
```

---

## Solução de problemas

**`UnicodeDecodeError` ao conectar no Windows**  
A senha do banco não pode ter acentos ou caracteres especiais. Use apenas letras e números. O `criar_banco.py` já configura a senha como `oraculo123`.

**`ModuleNotFoundError: No module named 'retrieval'`**  
Todos os arquivos devem estar na mesma pasta (estrutura flat). Não use subpastas. Verifique se `connection.py` e `embedder.py` existem na raiz do projeto.

**`pgvector` não encontrado**  
No Windows, reinstale o PostgreSQL pelo instalador da EDB marcando a opção pgvector, ou siga o guia em [github.com/pgvector/pgvector](https://github.com/pgvector/pgvector#installation).

**Ollama não responde**  
Verifique se o processo está rodando com `ollama serve` em um terminal separado. Confirme os modelos instalados com `ollama list`.

**PDFs sem texto extraído**  
O `pdfplumber` não processa PDFs escaneados (imagens). Esses arquivos são pulados automaticamente com o log `PDF sem texto extraível`.

---

## Git — primeiro setup

```bash
git init
git branch -M main

# Adiciona todos os arquivos (o .gitignore já exclui .env e ingestion/pdfs/)
git add .
git status    # confirme que .env NÃO aparece

git commit -m "feat: ORACULO/UDESC — setup inicial

- Pipeline RAG: pgvector + nomic-embed-text + aya-expanse:8b
- Scraper das INs da UDESC
- Pipeline de vetorização com chunking por artigo/seção
- Chatbot com histórico de sessão persistido no PostgreSQL
- Suite de testes com modo interativo"

# Conectar ao repositório remoto
git remote add origin https://github.com/SEU_USUARIO/oraculo-udesc.git
git push -u origin main
```